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

import threading
import time
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
        assert self.service._iteration_number == 1  # iteration number incremented

    def test_loop(self):
        self.service = TestFiniteService()

        self.service.start()

        assert self.service._iteration_number == 3
        assert not self.service._enabled

    def test_iter_pause(self):
        self.service = TestFiniteService(iter_pause=1)
        mock_wait = mock.MagicMock(return_value=False)
        self.service._wake_event.wait = mock_wait

        self.service.start()

        # Two wait() calls — after iterations 1 and 2. Iteration 3
        # calls stop() from inside _iteration(), and the loop exits
        # immediately via the _enabled check, without an extra sleep.
        assert mock_wait.call_count == 2
        wait_values = [c.kwargs["timeout"] for c in mock_wait.call_args_list]
        assert all(0.5 < value < 1.1 for value in wait_values)

    def test_iter_pause_zero_wo_slept(self):
        self.service = TestFiniteService(iter_pause=0)
        mock_wait = mock.MagicMock(return_value=False)
        self.service._wake_event.wait = mock_wait

        self.service.start()

        mock_wait.assert_not_called()

    def test_iter_min_period(self):
        self.service = TestFiniteService(iter_min_period=0.001, iter_pause=0)
        mock_wait = mock.MagicMock(return_value=False)
        self.service._wake_event.wait = mock_wait

        self.service.start()

        # We mock Event.wait, so it won't wait and we'll have many of requests
        mock_wait.assert_called()
        assert 0 < mock_wait.call_args.kwargs["timeout"] < 0.001

    def test_stop_interrupts_sleep(self):
        # Service with a long sleep period — stop() must wake it up
        service = TestService(iter_min_period=0, iter_pause=60)
        loop_thread = threading.Thread(target=service._loop)
        loop_thread.start()

        # Give the loop time to enter the sleep phase
        loop_thread.join(timeout=0.1)

        service.stop()
        loop_thread.join(timeout=1)

        assert not service._enabled
        assert not loop_thread.is_alive(), (
            "stop() did not interrupt sleep — loop thread still running"
        )

    def test_stop_from_iteration_exits_fast(self):
        # stop() called from inside _iteration() must not delay the
        # exit by a full iter_min_period. The loop checks _enabled
        # right after _loop_iteration() and breaks immediately.
        class StopOnFirstIteration(basic.BasicService):
            __test__ = False

            def _iteration(self):
                self.stop()

        service = StopOnFirstIteration(iter_min_period=5, iter_pause=0)
        start_time = time.monotonic()
        loop_thread = threading.Thread(target=service._loop)
        loop_thread.start()
        loop_thread.join(timeout=2)
        elapsed = time.monotonic() - start_time

        assert not service._enabled
        assert not loop_thread.is_alive(), (
            "stop() from _iteration() delayed exit by a full "
            "iter_min_period — loop thread still running after 2s"
        )
        assert elapsed < 1.0, (
            "stop() from _iteration() took %.2fs to exit — "
            "expected under 1s" % elapsed
        )
