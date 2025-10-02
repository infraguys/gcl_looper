#    Copyright 2025 George Melikov <mail@gmelikov.ru>
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import abc
import logging
import time

from gcl_looper.services import base


LOG = logging.getLogger(__name__)


class BasicService(base.AbstractService):
    __log_iteration__ = True

    def __init__(self, iter_min_period=1, iter_pause=0.1):
        super(BasicService, self).__init__()
        self._enabled = False
        self._iter_min_period = iter_min_period
        self._iter_pause = iter_pause
        self._iteration_number = 0

    def _loop_iteration(self):
        iteration = self._iteration_number
        if self.__log_iteration__:
            LOG.debug(
                "Iteration #%d started for %s",
                iteration,
                self.__class__.__name__,
            )
        try:
            self._iteration()
            if self.__log_iteration__:
                LOG.debug(
                    "Iteration #%d finished for %s",
                    iteration,
                    self.__class__.__name__,
                )
        except Exception:
            LOG.exception(
                "Unexpected error during iteration #%d for %s",
                iteration,
                self.__class__.__name__,
            )
        finally:
            self._iteration_number += 1

    def _loop(self):
        self._enabled = True
        next_iteration_time = 0
        while self._enabled:
            current_time = time.monotonic()

            if current_time >= next_iteration_time:
                next_iteration_time = current_time + self._iter_min_period
                self._loop_iteration()

            time_to_sleep = next_iteration_time - time.monotonic()
            if time_to_sleep > 0 or self._iter_pause > 0:
                time.sleep(max(time_to_sleep, self._iter_pause))

    @abc.abstractmethod
    def _iteration(self):
        """Implement your logic per one iteration here"""
        raise NotImplementedError()

    def stop(self):
        LOG.info("Stop service")
        self._enabled = False
