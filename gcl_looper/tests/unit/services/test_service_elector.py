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

import pytest

from gcl_looper.election import exceptions as election_exc
from gcl_looper.services import basic


class FakeElector:
    """Minimal elector stub: grants or refuses every try_lead()."""

    def __init__(self, results=None):
        self.results = list(results or [True])
        self.is_leader = False
        self.close_calls = 0
        self.ensure_calls = 0

    def try_lead(self):
        result = self.results.pop(0) if self.results else False
        if isinstance(result, Exception):
            self.is_leader = False
            raise result
        self.is_leader = bool(result)
        return self.is_leader

    def ensure_leadership(self):
        self.ensure_calls += 1
        if not self.is_leader:
            raise election_exc.LeadershipLostError(lock_key="fake")

    def close(self):
        self.close_calls += 1
        self.is_leader = False


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


class TestBasicServiceWithoutElector:
    def test_always_master_and_iterates(self):
        service = WatchedService()
        service._loop_iteration()
        assert service.iterations == 1
        assert service.is_master is True
        assert service.become_master_calls == 0
        assert service.lose_master_calls == 0


class TestBasicServiceWithElector:
    def test_master_iterates(self):
        service = WatchedService(elector=FakeElector([True]))
        service._loop_iteration()
        assert service.iterations == 1
        assert service.is_master is True
        assert service.become_master_calls == 1
        # No repeated hook while the state does not change.
        service._elector.results = [True]
        service._loop_iteration()
        assert service.become_master_calls == 1
        assert service.iterations == 2

    def test_ensure_master_delegates_to_elector(self):
        elector = FakeElector([True])
        service = WatchedService(elector=elector)
        service._loop_iteration()

        service.ensure_master()

        assert elector.ensure_calls == 1

    def test_standby_rejects_boost_with_warning(self):
        service = WatchedService(elector=FakeElector([False]))

        with mock.patch("gcl_looper.services.basic.LOG") as mock_log:
            assert service.boost() is False

        assert service.is_boosted is False
        mock_log.warning.assert_called_once()

    def test_standby_skips_iterations_but_counts_them(self):
        service = WatchedService(elector=FakeElector([False, False]))
        service._loop_iteration()
        service._loop_iteration()
        assert service.iterations == 0
        assert service.is_master is False
        assert service._iteration_number == 2
        assert service.lose_master_calls == 0  # was never the master

    def test_becomes_master_later(self):
        service = WatchedService(elector=FakeElector([False, True]))
        service._loop_iteration()
        assert service.iterations == 0
        service._loop_iteration()
        assert service.iterations == 1
        assert service.is_master is True
        assert service.become_master_calls == 1

    def test_loses_master(self):
        service = WatchedService(elector=FakeElector([True, False]))
        service._loop_iteration()
        assert service.become_master_calls == 1
        service._loop_iteration()
        assert service.is_master is False
        assert service.lose_master_calls == 1
        assert service.iterations == 1

    def test_critical_elector_error_is_logged_not_fatal(self):
        elector = FakeElector([True, election_exc.BackendError(reason="db down")])
        service = WatchedService(elector=elector)
        service._loop_iteration()
        assert service.is_master is True

        with mock.patch("gcl_looper.services.basic.LOG") as mock_log:
            service._loop_iteration()
        mock_log.exception.assert_called_once()
        # Demoted on connection loss, hook fired once.
        assert service.is_master is False
        assert service.lose_master_calls == 1
        # The loop survived: next attempt is possible.
        elector.results = [True]
        service._loop_iteration()
        assert service.is_master is True

    def test_iteration_error_keeps_master_state(self):
        class BoomService(WatchedService):
            def _iteration(self):
                raise RuntimeError("business error")

        service = BoomService(elector=FakeElector([True]))
        service._loop_iteration()  # error is logged inside, not raised
        assert service.is_master is True

    def test_hooks_errors_do_not_break_the_loop(self):
        service = WatchedService(elector=FakeElector([True]))
        with mock.patch.object(
            service, "_on_become_master", side_effect=RuntimeError("hook error")
        ) as mock_become, mock.patch("gcl_looper.services.basic.LOG") as mock_log:
            service._loop_iteration()
        mock_log.exception.assert_called()
        mock_become.assert_called_once()
        # The transition is rolled back and the lock is released so a
        # healthy standby can take over.
        assert service.is_master is False
        assert service.iterations == 0
        assert service._elector.close_calls == 1

        # The hook is retried on the next iteration (now it succeeds).
        service._elector.results = [True]
        with mock.patch.object(service, "_on_become_master") as mock_become2:
            service._loop_iteration()
        mock_become2.assert_called_once()
        assert service.is_master is True
        assert service.iterations == 1

    def test_finish_calls_lose_master_before_close(self):
        elector = FakeElector([True])
        service = WatchedService(elector=elector)
        service._loop_iteration()
        assert service.is_master is True
        assert service.become_master_calls == 1
        assert service.lose_master_calls == 0
        service._finish()
        assert elector.close_calls == 1
        assert service.is_master is False
        assert service.lose_master_calls == 1

    def test_finish_closes_the_elector(self):
        elector = FakeElector([True])
        service = WatchedService(elector=elector)
        service._loop_iteration()
        service._finish()
        assert elector.close_calls == 1

    def test_no_elector_finish_is_safe(self):
        service = WatchedService()
        service._finish()  # must not raise


class TestLaunchpadElector:
    def test_child_boost_rejected_while_launchpad_is_standby(self):
        from gcl_looper.services.oslo import launchpad

        child = WatchedService()
        service = launchpad.LaunchpadService([child], elector=FakeElector([False]))

        with mock.patch("gcl_looper.services.basic.LOG") as mock_log:
            assert child.boost(0.5) is False

        assert child.is_boosted is False
        assert service.is_boosted is False
        mock_log.warning.assert_called_once()

    def test_losing_master_resets_boost_tree_with_warning(self):
        from gcl_looper.services.oslo import launchpad

        child = WatchedService()
        service = launchpad.LaunchpadService(
            [child], elector=FakeElector([True, False])
        )
        service._loop_iteration()
        assert child.boost(0.5) is True
        assert service.is_boosted is True

        with mock.patch("gcl_looper.services.basic.LOG") as mock_log:
            service._loop_iteration()

        assert child.is_boosted is False
        assert service.is_boosted is False
        mock_log.warning.assert_called_once()

    def test_inner_electors_torn_down_on_finish(self):
        from gcl_looper.services.oslo import launchpad

        el1 = FakeElector([True])
        el2 = FakeElector([True])
        s1 = WatchedService(elector=el1)
        s2 = WatchedService(elector=el2)

        # Make the inner services masters so demotion is exercised.
        s1._loop_iteration()
        s2._loop_iteration()
        assert s1.is_master is True
        assert s2.is_master is True

        service = launchpad.LaunchpadService([s1, s2])
        service._finish()
        assert el1.close_calls == 1
        assert el2.close_calls == 1
        # Inner services are demoted before their electors are torn down.
        assert s1.is_master is False
        assert s2.is_master is False
        assert s1.lose_master_calls == 1
        assert s2.lose_master_calls == 1

    def test_launchpad_elector_guards_inner_services(self):
        from gcl_looper.services.oslo import launchpad

        elector = FakeElector([False, True])
        inner = WatchedService()
        outer = WatchedService()
        service = launchpad.LaunchpadService([inner, outer], elector=elector)

        service._loop_iteration()
        assert inner.iterations == 0
        assert outer.iterations == 0

        service._loop_iteration()
        assert inner.iterations == 1
        assert outer.iterations == 1

    def test_launchpad_rejects_nested_electors(self):
        from gcl_looper.services.oslo import launchpad

        child = WatchedService(elector=FakeElector([True]))
        with pytest.raises(ValueError, match="either for the launchpad or its services"):
            launchpad.LaunchpadService([child], elector=FakeElector([True]))

    def test_inner_electors_close_before_outer_elector(self):
        from gcl_looper.services.oslo import launchpad

        calls = []
        inner_elector = FakeElector([True])
        outer_elector = FakeElector([True])
        inner_elector.close = lambda: calls.append("inner")
        outer_elector.close = lambda: calls.append("outer")
        child = WatchedService(elector=inner_elector)
        service = launchpad.LaunchpadService([child])
        service._elector = outer_elector

        service._finish()

        assert calls == ["inner", "outer"]


class TestLaunchpadFromCmdLine:
    """End-to-end check of the launchpad election oslo options."""

    INI = """
        [launchpad]
        services = mock.module:OpsSvc
        iter_min_period = 0
        iter_pause = 0
        {election}

        [mock.module:OpsSvc]
        param = abc
        """

    def _from_ini(self, monkeypatch, tmp_path, election):
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
        ini_file.write_text(textwrap.dedent(self.INI).format(election=election))
        return launchpad.LaunchpadService.from_cmd_line(
            ["--config-file", str(ini_file)]
        )

    def test_no_elector_by_default(self, monkeypatch, tmp_path):
        service = self._from_ini(monkeypatch, tmp_path, "")
        assert service._elector is None

    def test_always_elector_from_config(self, monkeypatch, tmp_path):
        service = self._from_ini(
            monkeypatch,
            tmp_path,
            "election_backend = always",
        )
        from gcl_looper.election.always import AlwaysLeader

        assert isinstance(service._elector, AlwaysLeader)

    def test_pg_elector_from_config(self, monkeypatch, tmp_path):
        service = self._from_ini(
            monkeypatch,
            tmp_path,
            "election_backend = postgres_table\n"
            "election_connection_url = postgresql://u:p@h:5432/d\n"
            "election_lock_key = cmd_line_lock\n"
            "election_lock_timeout = 11",
        )
        from gcl_looper.election.db import DbLeaderElector
        from gcl_looper.election.drivers.pg_table import (
            PostgresTableLockDriver,
        )

        assert isinstance(service._elector, DbLeaderElector)
        driver = service._elector.driver
        assert isinstance(driver, PostgresTableLockDriver)
        assert driver.lock_key == "cmd_line_lock"
        assert driver._lock_timeout == 11
