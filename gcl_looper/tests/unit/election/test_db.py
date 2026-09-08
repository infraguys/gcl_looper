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

from gcl_looper.election import db as db_elector
from gcl_looper.election import exceptions as exc


class FakeDriver:
    lock_key = "fake_lock"
    lock_timeout = None

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


class TestDbLeaderElector:
    def test_acquire_grants_leadership(self):
        driver = FakeDriver([True])
        elector = db_elector.DbLeaderElector(driver)
        assert elector.is_leader is False
        assert elector.try_lead() is True
        assert elector.is_leader is True
        assert driver.acquire_calls == 1
        elector.close()

    def test_acquire_denied_is_not_leader(self):
        driver = FakeDriver([False])
        elector = db_elector.DbLeaderElector(driver)
        assert elector.try_lead() is False
        assert elector.is_leader is False

    def test_master_refresh_is_throttled(self, monkeypatch):
        current_time = [100.0]
        monkeypatch.setattr(db_elector.time, "monotonic", lambda: current_time[0])
        driver = FakeDriver([True, True])
        elector = db_elector.DbLeaderElector(driver, refresh_interval=2)

        for _ in range(100):
            assert elector.try_lead() is True
        assert driver.acquire_calls == 1

        current_time[0] += 2
        assert elector.try_lead() is True
        assert driver.acquire_calls == 2
        elector.close()

    def test_losing_the_lock_demotes(self, monkeypatch):
        current_time = [100.0]
        monkeypatch.setattr(db_elector.time, "monotonic", lambda: current_time[0])
        driver = FakeDriver([True, False])
        elector = db_elector.DbLeaderElector(driver, refresh_interval=2)
        assert elector.try_lead() is True

        current_time[0] += 2
        assert elector.try_lead() is False
        assert elector.is_leader is False

    def test_connection_error_marks_not_leader_and_propagates(self, monkeypatch):
        current_time = [100.0]
        monkeypatch.setattr(db_elector.time, "monotonic", lambda: current_time[0])
        err = exc.BackendError(reason="boom")
        driver = FakeDriver([True, err])
        elector = db_elector.DbLeaderElector(driver, refresh_interval=2)
        assert elector.try_lead() is True

        current_time[0] += 2
        with pytest.raises(exc.BackendError):
            elector.try_lead()
        assert elector.is_leader is False

    def test_lock_is_not_refreshed_during_iteration(self, monkeypatch):
        current_time = [100.0]
        monkeypatch.setattr(db_elector.time, "monotonic", lambda: current_time[0])
        driver = FakeDriver([True, True])
        driver.lock_timeout = 20
        elector = db_elector.DbLeaderElector(driver, refresh_interval=2)

        assert elector.try_lead() is True
        current_time[0] += 10
        assert driver.acquire_calls == 1
        assert elector.is_leader is True

        assert elector.try_lead() is True
        assert driver.acquire_calls == 2
        elector.close()

    def test_ensure_leadership_raises_before_lock_is_acquired(self):
        elector = db_elector.DbLeaderElector(FakeDriver([False]))
        with pytest.raises(exc.LeadershipLostError):
            elector.ensure_leadership()

    def test_ensure_leadership_ok_when_leader(self):
        driver = FakeDriver([True, True])
        elector = db_elector.DbLeaderElector(driver)
        assert elector.try_lead() is True
        elector.ensure_leadership()
        assert driver.acquire_calls == 2
        elector.close()

    def test_ensure_leadership_detects_lost_lock(self):
        elector = db_elector.DbLeaderElector(FakeDriver([True, False]))
        assert elector.try_lead() is True
        with pytest.raises(exc.LeadershipLostError):
            elector.ensure_leadership()
        assert elector.is_leader is False

    def test_close_releases_and_closes(self):
        driver = FakeDriver([True])
        elector = db_elector.DbLeaderElector(driver)
        elector.try_lead()
        elector.close()
        assert driver.release_calls == 1
        assert driver.close_calls == 1
        assert elector.is_leader is False

    def test_close_swallows_driver_errors(self):
        class BrokenDriver(FakeDriver):
            def release(self):
                raise RuntimeError("release failed")

            def close(self):
                raise RuntimeError("close failed")

        elector = db_elector.DbLeaderElector(BrokenDriver())
        elector.close()  # must not raise

    def test_lock_key_delegates_to_driver(self):
        elector = db_elector.DbLeaderElector(FakeDriver())
        assert elector.lock_key == "fake_lock"

    def test_refresh_interval_must_be_positive(self):
        with pytest.raises(ValueError):
            db_elector.DbLeaderElector(FakeDriver(), refresh_interval=0)

    def test_refresh_interval_must_be_less_than_lock_timeout(self):
        driver = FakeDriver()
        driver.lock_timeout = 2
        with pytest.raises(ValueError, match="less than"):
            db_elector.DbLeaderElector(driver, refresh_interval=2)

    def test_make_db_elector_helper(self):
        elector = db_elector.make_db_elector(FakeDriver(), refresh_interval=1.5)
        assert isinstance(elector, db_elector.DbLeaderElector)
        assert elector._refresh_interval == 1.5
