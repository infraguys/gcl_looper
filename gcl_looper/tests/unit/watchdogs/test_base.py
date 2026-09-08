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

import time

import pytest

from gcl_looper.watchdogs import base as wd_base
from gcl_looper.watchdogs import exceptions as exc


class TestWatchDogBase:
    def test_is_always_master(self):
        watchdog = wd_base.WatchDogBase()
        assert watchdog.is_master is True

    def test_context_manager(self):
        watchdog = wd_base.WatchDogBase()
        with watchdog:
            assert watchdog._in_context is True
        assert watchdog._in_context is False

    def test_is_alive(self):
        watchdog = wd_base.WatchDogBase()
        assert watchdog.is_alive() is True

    def test_mark_failed_makes_not_alive(self):
        watchdog = wd_base.WatchDogBase()
        watchdog.mark_failed()
        assert watchdog.is_alive() is False
        with pytest.raises(exc.ServiceIsMarkedFailed):
            with watchdog:
                pass


class TestTimedWatchDog:
    def test_alive_within_timeout(self):
        watchdog = wd_base.TimedWatchDog(heartbeat_timeout=5)
        assert watchdog.is_alive() is True

    def test_heartbeat_timeout_raises_critical(self):
        watchdog = wd_base.TimedWatchDog(heartbeat_timeout=0)
        # Force a stale heartbeat.
        watchdog._last_heartbeat = time.time() - 100
        with pytest.raises(exc.ServiceHeartbeatTimeout):
            watchdog._check_health()

    def test_generate_heartbeat_resets_timeout(self):
        watchdog = wd_base.TimedWatchDog(heartbeat_timeout=1)
        watchdog._last_heartbeat = time.time() - 100
        watchdog.generate_heartbeat()
        assert watchdog.is_alive() is True

    def test_heartbeat_timeout_detected_on_enter(self):
        # __enter__ checks health *before* refreshing the heartbeat, so a
        # stale heartbeat is detected instead of being masked by the
        # refresh that happens in the same call.
        watchdog = wd_base.TimedWatchDog(heartbeat_timeout=0)
        watchdog._last_heartbeat = time.time() - 100
        with pytest.raises(exc.ServiceHeartbeatTimeout):
            with watchdog:
                pass

    def test_is_master_true(self):
        watchdog = wd_base.TimedWatchDog(heartbeat_timeout=1)
        assert watchdog.is_master is True
