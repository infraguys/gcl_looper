#    Copyright 2025 Genesis Corporation.
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

from gcl_looper.watchdogs import database as wd_db
from gcl_looper.watchdogs import exceptions as exc


class FakeDriver:
    lock_key = "fake_lock"

    def __init__(self, results=None):
        self.results = list(results or [True])
        self.acquire_calls = 0
        self.release_calls = 0
        self.close_calls = 0
        self.is_held = False

    def acquire(self):
        self.acquire_calls += 1
        result = self.results.pop(0) if self.results else False
        if isinstance(result, Exception):
            raise result
        self.is_held = result
        return result

    def release(self):
        self.release_calls += 1
        self.is_held = False

    def close(self):
        self.close_calls += 1


class TestDbWatchDog:
    def test_acquire_success_enters_context_as_master(self):
        driver = FakeDriver([True])
        watchdog = wd_db.DbWatchDog(driver)
        assert watchdog.is_master is False
        with watchdog:
            assert watchdog.is_master is True
        assert driver.acquire_calls == 1

    def test_acquire_failure_raises_minor_and_skips(self):
        driver = FakeDriver([False])
        watchdog = wd_db.DbWatchDog(driver)
        with pytest.raises(exc.LockAcquireFailed):
            with watchdog:
                raise AssertionError("the body must not run")
        assert watchdog.is_master is False

    def test_refresh_every_enter(self):
        driver = FakeDriver([True, True, True])
        watchdog = wd_db.DbWatchDog(driver)
        for _ in range(3):
            with watchdog:
                pass
        assert driver.acquire_calls == 3
        assert watchdog.is_master is True

    def test_losing_the_lock_demotes(self):
        driver = FakeDriver([True, False])
        watchdog = wd_db.DbWatchDog(driver)
        with watchdog:
            assert watchdog.is_master is True
        with pytest.raises(exc.LockAcquireFailed):
            with watchdog:
                raise AssertionError("the body must not run")
        assert watchdog.is_master is False

    def test_connection_error_marks_not_master_and_propagates(self):
        err = exc.LockConnectionError(reason="boom")
        driver = FakeDriver([True, err])
        watchdog = wd_db.DbWatchDog(driver)
        with watchdog:
            assert watchdog.is_master is True
        with pytest.raises(exc.LockConnectionError):
            with watchdog:
                pass
        assert watchdog.is_master is False

    def test_is_alive_reflects_lock_state(self):
        driver = FakeDriver([True, False])
        watchdog = wd_db.DbWatchDog(driver)
        assert watchdog.is_alive() is True
        assert watchdog.is_alive() is False

    def test_teardown_releases_and_closes(self):
        driver = FakeDriver([True])
        watchdog = wd_db.DbWatchDog(driver)
        with watchdog:
            pass
        watchdog.teardown()
        assert driver.release_calls == 1
        assert driver.close_calls == 1
        assert watchdog.is_master is False

    def test_teardown_swallows_driver_errors(self):
        class BrokenDriver(FakeDriver):
            def release(self):
                raise RuntimeError("release failed")

            def close(self):
                raise RuntimeError("close failed")

        watchdog = wd_db.DbWatchDog(BrokenDriver())
        # Must not raise.
        watchdog.teardown()

    def test_mark_failed_resets_master(self):
        driver = FakeDriver([True])
        watchdog = wd_db.DbWatchDog(driver)
        with watchdog:
            assert watchdog.is_master is True
        watchdog.mark_failed()
        with pytest.raises(exc.ServiceIsMarkedFailed):
            with watchdog:
                pass
        assert watchdog.is_master is False

    def test_refresh_thread_keeps_lock_during_long_iteration(self):
        import threading
        import time

        # The refresh thread calls acquire() periodically; simulate a long
        # iteration that outlasts the refresh interval.
        refresh_calls = []
        lock = threading.Lock()

        class SlowDriver(FakeDriver):
            def acquire(self):
                with lock:
                    refresh_calls.append(time.monotonic())
                return True

        driver = SlowDriver([True])
        watchdog = wd_db.DbWatchDog(driver, refresh_interval=0.05)
        with watchdog:
            assert watchdog.is_master is True
            # Wait long enough for at least 2 refresh cycles.
            time.sleep(0.2)
            assert watchdog.is_master is True
        # At least the initial acquire + 2 refreshes.
        assert len(refresh_calls) >= 3

    def test_refresh_thread_stops_on_lock_loss(self):
        import time

        class LosingDriver(FakeDriver):
            def __init__(self):
                super().__init__([True])
                self._call = 0

            def acquire(self):
                self._call += 1
                if self._call > 1:
                    return False
                return True

        driver = LosingDriver()
        watchdog = wd_db.DbWatchDog(driver, refresh_interval=0.05)
        with watchdog:
            assert watchdog.is_master is True
            time.sleep(0.2)
            # The refresh thread lost the lock and demoted us.
            assert watchdog.is_master is False
