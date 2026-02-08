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

import pytest
from unittest import mock

from gcl_looper.services import basic


class TestService(basic.BasicService):
    __test__ = False

    def _iteration(self):
        pass


class TestFiniteService(basic.BasicService):
    __test__ = False
    _countdown = 3

    def __init__(self, iter_min_period=0, iter_pause=0):
        super(TestFiniteService, self).__init__(
            iter_min_period=iter_min_period,
            iter_pause=iter_pause,
        )

    def _iteration(self):
        if self._countdown > 1:
            self._countdown -= 1
        else:
            self.stop()


class TestBasicService:
    def setup_method(self):
        self.service = TestService(iter_min_period=0, iter_pause=0)

    @mock.patch("gcl_looper.services.basic.LOG")
    def test_loop_iteration_success(self, mock_log):
        self.service._iteration = mock.MagicMock()

        self.service._loop_iteration()

        self.service._iteration.assert_called_once()
        mock_log.exception.assert_not_called()
        assert self.service._iteration_number == 1

    @mock.patch("gcl_looper.services.basic.LOG")
    def test_loop_iteration_failure(self, mock_log):
        self.service._iteration = mock.MagicMock(
            side_effect=Exception("Test Exception")
        )

        self.service._loop_iteration()

        mock_log.debug.assert_called_once()
        self.service._iteration.assert_called_once()
        mock_log.exception.assert_called_once_with(
            "Unexpected error during iteration #%d for %s",
            0,
            self.service.__class__.__name__,
        )
        assert (
            self.service._iteration_number == 1
        )  # iteration number incremented

    def test_loop(self):
        self.service = TestFiniteService()

        self.service.start()

        assert self.service._iteration_number == 3
        assert self.service._enabled == False

    @mock.patch("time.sleep", return_value=None)
    def test_iter_pause(self, time_sleep):
        self.service = TestFiniteService(iter_pause=1)

        self.service.start()

        calls = [mock.call(1)] * 3
        time_sleep.assert_has_calls(calls)
        assert time_sleep.call_count == 3
        sleep_values = [call[0][0] for call in time_sleep.call_args_list]
        assert all(0.5 < value < 1.1 for value in sleep_values)

    @mock.patch("time.sleep", return_value=None)
    def test_iter_pause_zero_wo_slept(self, time_sleep):
        self.service = TestFiniteService(iter_pause=0)

        self.service.start()

        time_sleep.assert_not_called()

    @mock.patch("time.sleep", return_value=None)
    def test_iter_min_period(self, time_sleep):
        self.service = TestFiniteService(iter_min_period=0.001, iter_pause=0)

        self.service.start()

        # We mock time.sleep, so it won't wait and we'll have many of requests
        time_sleep.assert_called()
        assert 0 < time_sleep.call_args[0][0] < 0.001
