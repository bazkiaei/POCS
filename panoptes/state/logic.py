import os

import asyncio
from functools import partial

from astropy import units as u
from astropy.time import Time

from ..utils import error, listify
from ..utils import images

from collections import OrderedDict


class PanStateLogic(object):

    """ The enter and exit logic for each state. """

    def __init__(self, **kwargs):
        self.logger.debug("Setting up state logic")

        self._state_delay = kwargs.get('state_delay', 1.0)  # Small delay between State transitions
        self._sleep_delay = kwargs.get('sleep_delay', 7.0)  # When looping, use this for delay
        self._safe_delay = kwargs.get('safe_delay', 60 * 5)    # When checking safety, use this for delay

##################################################################################################
# State Conditions
##################################################################################################

    def check_safety(self, event_data=None):
        """ Checks the safety flag of the system to determine if safe.

        This will check the weather station as well as various other environmental
        aspects of the system in order to determine if conditions are safe for operation.

        Note:
            This condition is called by the state machine during each transition

        Args:
            event_data(transitions.EventData): carries information about the event if
            called from the state machine.

        Returns:
            bool:   Latest safety flag
        """

        self.logger.debug("Checking safety...")

        # It's always safe to park
        if event_data and event_data.event.name == 'park':
            self.logger.debug("Always safe to park")
            is_safe = True
        else:
            is_safe = self.is_safe()

        return is_safe

    def is_dark(self):
        """ Is it dark

        Checks whether it is dark at the location provided. This checks for the config
        entry `location.horizon` or 18 degrees (astronomical twilight).

        Returns:
            bool:   Is night at location

        """
        horizon = self.observatory.location.get('twilight_horizon', -18 * u.degree)

        is_dark = self.observatory.scheduler.is_night(self.now(), horizon=horizon)

        self.logger.debug("Is dark ({}): {}".format(horizon, is_dark))
        return is_dark

    def is_safe(self):
        """ Checks the safety flag of the system to determine if safe.

        This will check the weather station as well as various other environmental
        aspects of the system in order to determine if conditions are safe for operation.

        Note:
            This condition is called by the state machine during each transition

        Args:
            event_data(transitions.EventData): carries information about the event if
            called from the state machine.

        Returns:
            bool:   Latest safety flag
        """
        is_safe = dict()

        # Check if night time
        is_safe['is_dark'] = self.is_dark()

        # Check weather
        is_safe['good_weather'] = self.weather_station.is_safe()

        safe = all(is_safe.values())

        if 'weather' in self.config['simulator']:
            self.logger.debug("Weather simluator always safe")
            safe = True

        if not safe:
            self.logger.warning('System is not safe')
            self.logger.warning('{}'.format(is_safe))

            # Not safe so park unless we are sleeping
            if self.state not in ['sleeping', 'parked', 'parking']:
                self.park()

        return safe

    def mount_is_tracking(self, event_data):
        """ Transitional check for mount """
        return self.observatory.mount.is_tracking

    def initialize(self, event_data):
        """ """

        self.say("Initializing the system! Woohoo!")

        try:
            # Initialize the mount
            self.observatory.mount.initialize()

            # If successful, unpark and slew to home.
            if self.observatory.mount.is_initialized:
                self.observatory.mount.unpark()

                # Slew to home
                self.observatory.mount.slew_to_home()

                # Initialize each of the cameras while slewing
                for cam in self.observatory.cameras.values():
                    cam.connect()

            else:
                raise error.InvalidMountCommand("Mount not initialized")

        except Exception as e:
            self.say("Oh wait. There was a problem initializing: {}".format(e))
            self.say("Since we didn't initialize, I'm going to exit.")
            self.power_down()
        else:
            self._initialized = True

        return self._initialized


##################################################################################################
# State Logic
##################################################################################################

    def on_enter_ready(self, event_data):
        """
        Once in the `ready` state our unit has been initialized successfully. The next step is to
        schedule something for the night.
        """
        self.say("Up and ready to go!")

        self.wait_until_mount('is_home', 'schedule')

##################################################################################################

    def on_enter_scheduling(self, event_data):
        """
        In the `scheduling` state we attempt to find a target using our scheduler. If target is found,
        make sure that the target is up right now (the scheduler should have taken care of this). If
        observable, set the mount to the target and calls `slew_to_target` to begin slew.

        If no observable targets are available, `park` the unit.
        """
        self.say("Ok, I'm finding something good to look at...")

        # Get the next target
        try:
            target = self.observatory.get_target()
            self.logger.info(target)
        except Exception as e:
            self.logger.error("Error in scheduling: {}".format(e))

        # Assign the _method_
        next_state = 'park'

        if target is not None:

            self.say("Got it! I'm going to check out: {}".format(target.name))

            # Check if target is up
            if self.observatory.scheduler.target_is_up(Time.now(), target):
                self.logger.debug("Setting Target coords: {}".format(target))

                has_target = self.observatory.mount.set_target_coordinates(target)

                if has_target:
                    self.logger.debug("Mount set to target.".format(target))
                    next_state = 'slew_to_target'
                else:
                    self.logger.warning("Target not properly set. Parking.")
            else:
                self.say("That's weird, I have a target that is not up. Parking.")
        else:
            self.say("No valid targets found. Can't schedule. Going to park.")

        self.goto(next_state)

##################################################################################################

    def on_enter_slewing(self, event_data):
        """ Once inside the slewing state, set the mount slewing. """
        try:

            # Start the mount slewing
            self.observatory.mount.slew_to_target()

            # Wait until mount is_tracking, then transition to track state
            self.wait_until_mount('is_tracking', 'track')

            self.say("I'm slewing over to the coordinates to track the target.")
        except Exception as e:
            self.say("Wait a minute, there was a problem slewing. Sending to parking. {}".format(e))
            self.goto('park')

##################################################################################################

    def on_enter_tracking(self, event_data):
        """ The unit is tracking the target. Proceed to observations. """
        self.say("I'm now tracking the target.")

        ms_offset = self.observatory.offset_info.get('ms_offset', 0)  # RA North
        if ms_offset > 0:
            self.say("I'm adjusting the tracking by just a bit.")
            # Add some offset to the offset
            ms_offset = ms_offset + (250 * u.ms)
            direction = 'east'
            self.logger.debug("Adjusting tracking by {} to direction".format(ms_offset, direction))
            self.observatory.mount.serial_query('move_ms_{}'.format(direction), "{:05.0f}".format(ms_offset.value))

            # Reset offset_info
            self.observatory.offset_info = {}

        self.goto('observe')

##################################################################################################

    def on_enter_observing(self, event_data):
        """ """
        self.say("I'm finding exoplanets!")

        try:
            img_files = self.observatory.observe()
        except Exception as e:
            self.logger.warning("Problem with imaging: {}".format(e))
            self.say("Hmm, I'm not sure what happened with that exposure.")
        else:
            # Wait for files to exist to finish to set up processing
            try:
                self.wait_until_files_exist(img_files, 'analyze')
            except Exception as e:
                self.logger.error("Problem waiting for images: {}".format(e))
                self.goto('park')

##################################################################################################

    def on_enter_analyzing(self, event_data):
        """ """
        self.say("Analyzing image...")

        next_state = 'park'
        try:
            target = self.observatory.current_target
            self.logger.debug("For analyzing: Target: {}".format(target))

            observation = target.current_visit
            self.logger.debug("For analyzing: Observation: {}".format(observation))

            exposure = observation.current_exposure
            self.logger.debug("For analyzing: Exposure: {}".format(exposure))

            reference_image = target.reference_image
            self.logger.debug("Reference exposure: {}".format(reference_image))

            fits_headers = {
                'alt-obs': self.observatory.location.get('elevation'),
                'author': self.name,
                'date-end': Time.now().isot,
                'dec': target.coord.dec.value,
                'dec_nom': target.coord.dec.value,
                'epoch': float(target.coord.epoch),
                'equinox': target.coord.equinox,
                'instrument': self.name,
                'lat-obs': self.observatory.location.get('latitude').value,
                'latitude': self.observatory.location.get('latitude').value,
                'long-obs': self.observatory.location.get('longitude').value,
                'longitude': self.observatory.location.get('longitude').value,
                'object': target.name,
                'observer': self.name,
                'organization': 'Project PANOPTES',
                'ra': target.coord.ra.value,
                'ra_nom': target.coord.ra.value,
                'ra_obj': target.coord.ra.value,
                'telescope': self.name,
                'title': target.name,
            }

            try:
                # Process the raw images (converts to fits and plate solves)
                exposure.process_images(fits_headers=fits_headers, make_pretty=True)
            except Exception as e:
                self.logger.warning("Problem analyzing: {}".format(e))

            # Analyze image for tracking error
            if reference_image:
                last_image = exposure.images[list(exposure.images)[-1]]

                self.logger.debug(
                    "Comparing recent image to reference image: {}\t{}".format(reference_image, last_image))
                if not reference_image.get('fits_file') == last_image.get('fits_file'):
                    pass

                offset_info = {}

                # First try a simple correlation as it is much faster than plate solving
                try:
                    info = last_image.get('solved', {})
                    self.logger.debug("Info to use: {}".format(info))

                    d1 = images.read_image_data(
                        reference_image.get('fits_file', reference_image.get('img_file', None)))
                    d2 = images.read_image_data(last_image.get('fits_file', last_image.get('img_file', None)))

                    if d1 is None and d2 is None:
                        raise error.PanError("Can't get image data")

                    shift, error, diffphase = images.measure_offset(d1, d2)

                    pixel_scale = info.get('pixel_scale', 10.2859 * (u.arcsec / u.pixel))
                    self.logger.debug("Pixel scale: {}".format(pixel_scale))

                    sidereal_rate = (24 * u.hour).to(u.minute) / (360 * u.deg).to(u.arcsec)
                    self.logger.debug("Sidereal rate: {}".format(sidereal_rate))

                    self.logger.debug("Offset measured: {} {}".format(shift[0], shift[1]))
                    delta_ra, delta_dec = images.get_ra_dec_deltas(
                        shift[0] * u.pixel, shift[1] * u.pixel,
                        theta=info.get('rotation', 0 * u.deg),
                        rate=sidereal_rate,
                        pixel_scale=pixel_scale
                    )
                    offset_info['delta_ra'] = delta_ra
                    offset_info['delta_dec'] = delta_dec
                    self.logger.debug("Δ RA/Dec [pixel]: {} {}".format(delta_ra, delta_dec))

                    # Number of arcseconds we moved
                    delta_ra_as = pixel_scale * delta_ra
                    offset_info['delta_ra_as'] = delta_ra_as
                    self.logger.debug("Δ RA [arcsec]: {}".format(delta_ra_as))

                    # How many milliseconds at sidereal we are off
                    # (NOTE: This should be current rate, not necessarily sidearal)
                    ms_offset = (delta_ra_as * sidereal_rate).to(u.ms)
                    offset_info['ms_offset'] = ms_offset
                    self.logger.debug("MS Offset: {}".format(ms_offset))

                    # Number of arcseconds we moved
                    delta_dec_as = pixel_scale * delta_dec
                    offset_info['delta_dec_as'] = delta_dec_as
                    self.logger.debug("Δ Dec [arcsec]: {}".format(delta_dec_as))

                    # How many milliseconds at sidereal we are off
                    # (NOTE: This should be current rate, not necessarily sidearal)
                    ms_offset = (delta_dec_as * sidereal_rate).to(u.ms)
                    offset_info['ms_offset'] = ms_offset

                except Exception as e:
                    self.logger.warning("Can't get phase translation between images: {}".format(e))
                    self.logger.debug("Attempting plate solve")

                    try:
                        offset_info = images.solve_offset(
                            reference_image.get('solved', {}), last_image.get('solved', {}))
                        self.logger.debug("Offset info: {}".format(offset_info))
                    except AssertionError as e:
                        self.logger.warning("Can't solve offset: {}".format(e))

                self.logger.debug("Offset information: {}".format(offset_info))
                self.observatory.offset_info = offset_info

            # try:
            #     self.db.observations.insert({self.current_target: self.targets})
            # except:
            #     self.logger.warning("Problem inserting observation information")

        except Exception as e:
            self.logger.error("Problem in analyzing: {}".format(e))

        # If target has visits left, go back to observe
        if not observation.complete:
            next_state = 'adjust_tracking'

            # We have successfully analyzed this visit, so we go to next
        else:
            next_state = 'schedule'

        self.goto(next_state)

##################################################################################################

    def on_enter_parking(self, event_data):
        """ """
        try:
            self.say("I'm takin' it on home and then parking.")
            self.observatory.mount.home_and_park()

            self.say("Saving any observations")
            # if len(self.targets) > 0:
            #     for target, info in self.observatory.observed_targets.items():
            #         raw = info['observations'].get('raw', [])
            #         analyzed = info['observations'].get('analyzed', [])

            #         if len(raw) > 0:
            #             self.logger.debug("Saving {} with raw observations: {}".format(target, raw))
            #             self.db.observations.insert({target: observations})

            #         if len(analyzed) > 0:
            #             self.logger.debug("Saving {} with analyed observations: {}".format(target, observations))
            #             self.db.observations.insert({target: observations})

            self.wait_until_mount('is_parked', 'set_park')

        except Exception as e:
            self.say("Yikes. Problem in parking: {}".format(e))

##################################################################################################

    def on_enter_parked(self, event_data):
        """ """
        self.say("I'm parked now. Phew.")

        next_state = 'sleep'

        # Assume dark (we still check weather)
        if self.is_dark():
            # Assume bad weather so wait
            if not self.weather_station.is_safe():
                next_state = 'wait'
            else:
                self.say("Weather is good and it is dark. Something must have gone wrong. Sleeping")

        # Either wait until safe or goto next state (sleeping)
        if next_state == 'wait':
            self.wait_until_safe()
        else:
            self.goto(next_state)

##################################################################################################

    def on_enter_sleeping(self, event_data):
        """ """
        self.say("ZZzzzz...")

##################################################################################################
# Convenience Methods
##################################################################################################

    def goto(self, method, args=None):
        """ Calls the next state after a delay

        Args:
            method(str):    The `transition` method to call, required.
        """
        if self._loop.is_running():
            # If a string was passed, look for method matching name
            if isinstance(method, str) and hasattr(self, method):
                call_method = partial(getattr(self, method))
            else:
                call_method = partial(method, args)

            self.logger.debug("Method: {} Args: {}".format(method, args))
            self._loop.call_later(self._state_delay, call_method)

    def wait_until(self, method, transition):
        """ Waits until `method` is done, then calls `transition`

        This is a convenience method to wait for a method and then transition
        """
        if self._loop.is_running():
            self.logger.debug("Creating future for {} {}".format(transition, method))

            future = asyncio.Future()
            asyncio.ensure_future(method(future))
            future.add_done_callback(partial(self._goto_state, transition))

    def wait_until_mount(self, position, transition):
        """ Small convenience method for the mount. See `wait_until` """
        if self._loop.is_running():
            self.logger.debug("Waiting until {} to call {}".format(position, transition))

            position_method = partial(self._at_position, position)
            self.wait_until(position_method, transition)

    def wait_until_files_exist(self, filenames, transition):
        """ Given a file, wait until file exists then transition """
        if self._loop.is_running():
            self.logger.debug("Waiting until {} exist to call {}".format(filenames, transition))

            try:
                future = asyncio.Future()
                asyncio.ensure_future(self._file_exists(filenames, future))
                future.add_done_callback(partial(self._goto_state, transition))
            except Exception as e:
                self.logger.error("Can't wait on file: {}".format(e))

    def wait_until_safe(self, safe_delay=None):
        """ """
        if self._loop.is_running():
            self.logger.debug("Waiting until safe to call get_ready")

            if safe_delay is None:
                safe_delay = self._safe_delay

            wait_method = partial(self._is_safe, safe_delay=safe_delay)
            self.wait_until(wait_method, 'get_ready')


##################################################################################################
# Private Methods
##################################################################################################

    @asyncio.coroutine
    def _at_position(self, position, future):
        """ Loop until the mount is at a given `position`.

        Non-blocking loop that finishes when mount `position` is True

        Note:
            This is to be used along with `_goto_state` in the `wait_until` method.
            See `wait_until` for details.

        Args:
            position(str):  Any one of the mount's `is_*` properties
        """
        assert position, self.logger.error("Position required for loop")

        self.logger.debug("_at_position {} {}".format(position, future))

        while not getattr(self.observatory.mount, position):
            self.logger.debug("position: {} {}".format(position, getattr(self.observatory.mount, position)))
            yield from asyncio.sleep(self._sleep_delay)
        future.set_result(getattr(self.observatory.mount, position))

    @asyncio.coroutine
    def _file_exists(self, filenames, future):
        """ Loop until file exists

        Non-blocking loop that finishes when file exists. Sets the future
        to the filename.

        Args:
            filename(str or list):  File(s) to test for existence.
        """
        assert filenames, self.logger.error("Filename required for loop")

        filenames = listify(filenames)

        self.logger.debug("_file_exists {} {}".format(filenames, future))

        # Check if all files exist
        exist = [os.path.exists(f) for f in filenames]

        # Sleep (non-blocking) until all files exist
        while not all(exist):
            self.logger.debug("{} {}".format(filenames, all(exist)))
            yield from asyncio.sleep(self._sleep_delay)
            exist = [os.path.exists(f) for f in filenames]

        self.logger.debug("All files exist, now exiting loop")
        # Now that all files exist, set result
        future.set_result(filenames)

    @asyncio.coroutine
    def _is_safe(self, future, safe_delay=None):
        if safe_delay is None:
            safe_delay = self._safe_delay

        while not self.is_safe():
            self.logger.debug("System not safe, sleeping for {}".format(safe_delay))
            yield from asyncio.sleep(self._safe_delay)

        # Now that safe, return True
        future.set_result(True)

    def _goto_state(self, state, future):
        """  Create callback function for when slew is done

        Note:
            This is to be used along with `_at_position` in the `wait_until` method.
            See `wait_until` for details.

        Args:
            future(asyncio.future): Here be dragons. See `asyncio`
            state(str):         The name of a transition method to be called.
        """
        self.logger.debug("Inside _goto_state: {}".format(state))
        if not future.cancelled():
            goto = getattr(self, state)
            goto()
        else:
            self.logger.debug("Next state cancelled. Result from callback: {}".format(future.result()))
