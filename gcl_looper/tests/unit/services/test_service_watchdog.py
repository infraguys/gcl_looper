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

from unittest import mock


from gcl_looper.services import basic
from gcl_looper.watchdogs import exceptions as wd_exc


class FakeWatchDog:
    """Minimal watchdog stub: grants or refuses every enter."""

    def __init__(self, results=None):
        self.results = list(results or [True])
        self.is_master = False
        self.teardown_calls = 0

    def __enter__(self):
        result = self.results.pop(0) if self.results else False
        if isinstance(result, Exception):
            self.is_master = False
            raise result
        self.is_master = bool(result)
        if not self.is_master:
            raise wd_exc.LockAcquireFailed(lock_key="fake")
        return self

    def __exit__(self, *exc_info):
        return None

    def teardown(self):
        self.teardown_calls += 1
        self.is_master = False


class WatchedService(basic.BasicService):
    __test__ = False

    def __init__(self, **kwargs):
        super(WatchedService, self).__init__(iter_min_period=0, iter_pause=0, **kwargs)
        self.iterations = 0
        self.become_master_calls = 0
        self.lose_master_calls = 0

    def _iteration(self):
        self.iterations += 1

    def _on_become_master(self):
        self.become_master_calls += 1

    def _on_lose_master(self):
        self.lose_master_calls += 1


class TestBasicServiceWithoutWatchdog:
    def test_always_master_and_iterates(self):
        service = WatchedService()
        service._loop_iteration()
        assert service.iterations == 1
        assert service.is_master is True
        assert service.become_master_calls == 0
        assert service.lose_master_calls == 0


class TestBasicServiceWithWatchdog:
    def test_master_iterates(self):
        service = WatchedService(watchdog=FakeWatchDog([True]))
        service._loop_iteration()
        assert service.iterations == 1
        assert service.is_master is True
        assert service.become_master_calls == 1
        # No repeated hook while the state does not change.
        service._watchdog.results = [True]
        service._loop_iteration()
        assert service.become_master_calls == 1
        assert service.iterations == 2

    def test_standby_skips_iterations_but_counts_them(self):
        service = WatchedService(watchdog=FakeWatchDog([False, False]))
        service._loop_iteration()
        service._loop_iteration()
        assert service.iterations == 0
        assert service.is_master is False
        assert service._iteration_number == 2
        assert service.lose_master_calls == 0  # was never the master

    def test_becomes_master_later(self):
        service = WatchedService(watchdog=FakeWatchDog([False, True]))
        service._loop_iteration()
        assert service.iterations == 0
        service._loop_iteration()
        assert service.iterations == 1
        assert service.is_master is True
        assert service.become_master_calls == 1

    def test_loses_master(self):
        service = WatchedService(watchdog=FakeWatchDog([True, False]))
        service._loop_iteration()
        assert service.become_master_calls == 1
        service._loop_iteration()
        assert service.is_master is False
        assert service.lose_master_calls == 1
        assert service.iterations == 1

    def test_critical_watchdog_error_is_logged_not_fatal(self):
        watchdog = FakeWatchDog([True, wd_exc.LockConnectionError(reason="db down")])
        service = WatchedService(watchdog=watchdog)
        service._loop_iteration()
        assert service.is_master is True

        with mock.patch("gcl_looper.services.basic.LOG") as mock_log:
            service._loop_iteration()
        mock_log.exception.assert_called_once()
        # Demoted on connection loss, hook fired once.
        assert service.is_master is False
        assert service.lose_master_calls == 1
        # The loop survived: next attempt is possible.
        watchdog.results = [True]
        service._loop_iteration()
        assert service.is_master is True

    def test_iteration_error_keeps_master_state(self):
        class BoomService(WatchedService):
            def _iteration(self):
                raise RuntimeError("business error")

        service = BoomService(watchdog=FakeWatchDog([True]))
        service._loop_iteration()  # error is logged inside, not raised
        assert service.is_master is True

    def test_hooks_errors_do_not_break_the_loop(self):
        service = WatchedService(watchdog=FakeWatchDog([True]))
        with mock.patch.object(
            service, "_on_become_master", side_effect=RuntimeError("hook error")
        ) as mock_become, mock.patch("gcl_looper.services.basic.LOG") as mock_log:
            service._loop_iteration()
        mock_log.exception.assert_called()
        mock_become.assert_called_once()
        # The transition is rolled back so the hook is retried on the
        # next iteration instead of running work without the resources
        # the hook was supposed to set up.
        assert service.is_master is False
        assert service.iterations == 0

        # The hook is retried on the next iteration (now it succeeds).
        service._watchdog.results = [True]
        with mock.patch.object(service, "_on_become_master") as mock_become2:
            service._loop_iteration()
        mock_become2.assert_called_once()
        assert service.is_master is True
        assert service.iterations == 1

    def test_finish_calls_lose_master_before_teardown(self):
        watchdog = FakeWatchDog([True])
        service = WatchedService(watchdog=watchdog)
        service._loop_iteration()
        assert service.is_master is True
        assert service.become_master_calls == 1
        assert service.lose_master_calls == 0
        service._finish()
        assert watchdog.teardown_calls == 1
        assert service.is_master is False
        assert service.lose_master_calls == 1

    def test_finish_teardowns_the_watchdog(self):
        watchdog = FakeWatchDog([True])
        service = WatchedService(watchdog=watchdog)
        service._loop_iteration()
        service._finish()
        assert watchdog.teardown_calls == 1

    def test_no_watchdog_finish_is_safe(self):
        service = WatchedService()
        service._finish()  # must not raise


class TestLaunchpadWatchdog:
    def test_inner_watchdogs_torn_down_on_finish(self):
        from gcl_looper.services.oslo import launchpad

        wd1 = FakeWatchDog([True])
        wd2 = FakeWatchDog([True])
        s1 = WatchedService(watchdog=wd1)
        s2 = WatchedService(watchdog=wd2)

        # Make the inner services masters so demotion is exercised.
        s1._loop_iteration()
        s2._loop_iteration()
        assert s1.is_master is True
        assert s2.is_master is True

        service = launchpad.LaunchpadService([s1, s2])
        service._finish()
        assert wd1.teardown_calls == 1
        assert wd2.teardown_calls == 1
        # Inner services are demoted before their watchdogs are torn down.
        assert s1.is_master is False
        assert s2.is_master is False
        assert s1.lose_master_calls == 1
        assert s2.lose_master_calls == 1

    def test_launchpad_watchdog_guards_inner_services(self):
        from gcl_looper.services.oslo import launchpad

        watchdog = FakeWatchDog([False, True])
        inner = WatchedService()
        outer = WatchedService()
        service = launchpad.LaunchpadService([inner, outer], watchdog=watchdog)

        service._loop_iteration()
        assert inner.iterations == 0
        assert outer.iterations == 0

        service._loop_iteration()
        assert inner.iterations == 1
        assert outer.iterations == 1


class TestLaunchpadFromCmdLine:
    """End-to-end check of the launchpad watchdog oslo options."""

    INI = """
        [launchpad]
        services = mock.module:OpsSvc
        iter_min_period = 0
        iter_pause = 0
        {watchdog}

        [mock.module:OpsSvc]
        param = abc
        """

    def _from_ini(self, monkeypatch, tmp_path, watchdog):
        import textwrap

        from oslo_config import cfg as oslo_cfg

        from gcl_looper import utils
        from gcl_looper.services.oslo import launchpad

        class OpsSvc:
            @classmethod
            def svc_get_config_opts(cls):
                return [oslo_cfg.StrOpt("param", default="x")]

            def __init__(self, param="x"):
                self.param = param

        monkeypatch.setattr(oslo_cfg, "CONF", oslo_cfg.ConfigOpts())

        # Mock only the service-class loader, letting the real driver
        # registry use the genuine loader for its own paths.
        real_loader = utils.cfg_load_module_attr

        def loader(path):
            if path.endswith("OpsSvc"):
                return OpsSvc
            return real_loader(path)

        monkeypatch.setattr("gcl_looper.utils.cfg_load_module_attr", loader)
        ini_file = tmp_path / "l.ini"
        ini_file.write_text(textwrap.dedent(self.INI).format(watchdog=watchdog))
        return launchpad.LaunchpadService.from_cmd_line(
            ["--config-file", str(ini_file)]
        )

    def test_no_watchdog_by_default(self, monkeypatch, tmp_path):
        service = self._from_ini(monkeypatch, tmp_path, "")
        assert service._watchdog is None

    def test_timed_watchdog_from_config(self, monkeypatch, tmp_path):
        service = self._from_ini(
            monkeypatch,
            tmp_path,
            "watchdog_lock_type = timed\nwatchdog_heartbeat_timeout = 7",
        )
        from gcl_looper.watchdogs.base import TimedWatchDog

        assert isinstance(service._watchdog, TimedWatchDog)
        assert service._watchdog._heartbeat_timeout == 7

    def test_pg_watchdog_from_config(self, monkeypatch, tmp_path):
        service = self._from_ini(
            monkeypatch,
            tmp_path,
            "watchdog_lock_type = postgres_table\n"
            "watchdog_connection_url = postgresql://u:p@h:5432/d\n"
            "watchdog_lock_key = cmd_line_lock\n"
            "watchdog_lock_timeout = 11",
        )
        from gcl_looper.watchdogs.database import DbWatchDog
        from gcl_looper.watchdogs.locks.pg_table import (
            PostgresTableLockDriver,
        )

        assert isinstance(service._watchdog, DbWatchDog)
        driver = service._watchdog.driver
        assert isinstance(driver, PostgresTableLockDriver)
        assert driver.lock_key == "cmd_line_lock"
        assert driver._lock_timeout == 11
