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
from oslo_config import cfg

from gcl_looper.watchdogs import base as wd_base
from gcl_looper.watchdogs import config as wd_config
from gcl_looper.watchdogs import database as wd_db
from gcl_looper.watchdogs import exceptions as exc
from gcl_looper.watchdogs import factory as wd_factory


class TestFactory:
    def test_dummy(self):
        watchdog = wd_factory.get_watchdog(wd_factory.DUMMY_TYPE)
        assert isinstance(watchdog, wd_base.WatchDogBase)
        assert watchdog.is_master is True

    def test_none_type_is_dummy(self):
        assert isinstance(wd_factory.get_watchdog(None), wd_base.WatchDogBase)

    def test_timed(self):
        watchdog = wd_factory.get_watchdog(wd_factory.TIMED_TYPE, heartbeat_timeout=42)
        assert isinstance(watchdog, wd_base.TimedWatchDog)
        assert watchdog._heartbeat_timeout == 42

    def test_db_watchdog(self):
        watchdog = wd_factory.get_watchdog(
            "postgres_table",
            connection_url="postgresql://u:p@h/d",
            lock_key="k",
            heartbeat_timeout=99,
        )
        assert isinstance(watchdog, wd_db.DbWatchDog)
        assert watchdog.driver.lock_key == "k"
        assert watchdog._heartbeat_timeout == 99

    def test_db_watchdog_drops_unknown_driver_params(self):
        # `lock_timeout` is accepted by postgres_table, `application_name`
        # is silently dropped by the driver kwargs filter.
        watchdog = wd_factory.get_watchdog(
            "postgres_table",
            connection_url="postgresql://u:p@h/d",
            lock_key="k",
            lock_timeout=5,
            application_name="whatever",
        )
        assert watchdog.driver._lock_timeout == 5

    def test_table_watchdog_gets_refresh_interval(self):
        # Table locks expire after `lock_timeout`; the factory must enable
        # the background refresh thread so long iterations do not lose
        # exclusivity.
        watchdog = wd_factory.get_watchdog(
            "postgres_table",
            connection_url="postgresql://u:p@h/d",
            lock_key="k",
            lock_timeout=30,
        )
        assert watchdog._refresh_interval is not None
        assert watchdog._refresh_interval == 10  # 30 / 3

    def test_advisory_watchdog_has_no_refresh_interval(self):
        # Advisory (session-level) locks do not expire, so no refresh
        # thread is needed.
        watchdog = wd_factory.get_watchdog(
            "postgres_advisory",
            connection_url="postgresql://u:p@h/d",
            lock_key="k",
            lock_timeout=30,
        )
        assert watchdog._refresh_interval is None

    def test_unknown_watchdog_type_raises(self):
        with pytest.raises(exc.LockDriverNotFound):
            wd_factory.get_watchdog(
                "no_such_driver",
                connection_url="postgresql://u:p@h/d",
                lock_key="k",
            )

    def test_db_requires_driver(self):
        with pytest.raises(ValueError):
            wd_factory.create_db_watchdog(connection_url="x", lock_key="y")

    def test_lock_key_required_by_driver(self):
        with pytest.raises(ValueError):
            wd_factory.get_watchdog(
                "postgres_table",
                connection_url="postgresql://u:p@h/d",
                lock_key="",
            )


class TestBuildWatchdog:
    def test_none_returns_no_watchdog(self):
        assert wd_config.build_watchdog(lock_type="none") is None
        assert wd_config.build_watchdog(lock_type="") is None

    def test_timed(self):
        watchdog = wd_config.build_watchdog(lock_type="timed", heartbeat_timeout=7)
        assert isinstance(watchdog, wd_base.TimedWatchDog)

    def test_db_requires_connection_url(self):
        with pytest.raises(ValueError):
            wd_config.build_watchdog(lock_type="postgres_table", lock_key="k")

    def test_db_requires_lock_key(self):
        with pytest.raises(ValueError):
            wd_config.build_watchdog(
                lock_type="postgres_table",
                connection_url="postgresql://u:p@h/d",
            )

    def test_default_lock_key_used(self):
        watchdog = wd_config.build_watchdog(
            lock_type="postgres_table",
            connection_url="postgresql://u:p@h/d",
            default_lock_key="service_default_lock",
        )
        assert watchdog.driver.lock_key == "service_default_lock"

    def test_explicit_lock_key_wins(self):
        watchdog = wd_config.build_watchdog(
            lock_type="postgres_table",
            connection_url="postgresql://u:p@h/d",
            lock_key="explicit",
            default_lock_key="default",
        )
        assert watchdog.driver.lock_key == "explicit"


class TestBuildFromKwargs:
    def test_disabled_removes_opts_returns_none(self):
        watchdog, rest = wd_config.build_watchdog_from_kwargs(
            watchdog_lock_type="none",
            name="svc",
            iter_min_period=2,
        )
        assert watchdog is None
        assert rest == {"name": "svc", "iter_min_period": 2}

    def test_db_watchdog_built_from_kwargs(self):
        watchdog, rest = wd_config.build_watchdog_from_kwargs(
            watchdog_lock_type="postgres_advisory",
            watchdog_connection_url="postgresql://u:p@h/d",
            watchdog_lock_key="k",
            watchdog_heartbeat_timeout=55,
            name="svc",
        )
        assert isinstance(watchdog, wd_db.DbWatchDog)
        assert watchdog._heartbeat_timeout == 55
        assert watchdog.driver.lock_key == "k"
        assert rest == {"name": "svc"}

    def test_empty_prefix_dedicated_section(self):
        watchdog, rest = wd_config.build_watchdog_from_kwargs(
            prefix="",
            lock_type="timed",
            heartbeat_timeout=11,
        )
        assert isinstance(watchdog, wd_base.TimedWatchDog)
        assert rest == {}


class TestConfigOpts:
    def test_opts_registered_and_parsed(self):
        opts = wd_config.get_config_opts()
        assert len(opts) == len(wd_config.WATCHDOG_OPTS)

        conf = cfg.ConfigOpts()
        conf.register_cli_opts(wd_config.get_config_opts(), "svc")
        conf(
            args=[
                "--svc-watchdog_lock_type",
                "timed",
                "--svc-watchdog_heartbeat_timeout",
                "15",
            ]
        )
        assert conf["svc"].watchdog_lock_type == "timed"
        assert conf["svc"].watchdog_heartbeat_timeout == 15

    def test_connection_url_is_secret(self):
        opts = wd_config.get_config_opts()
        url_opt = next(o for o in opts if o.name == "watchdog_connection_url")
        assert url_opt.secret is True

    @mock.patch("gcl_looper.watchdogs.config.build_watchdog")
    def test_build_watchdog_kwargs_passthrough(self, build_mock):
        build_mock.return_value = None
        wd_config.build_watchdog_from_kwargs(
            watchdog_lock_type="none",
            watchdog_lock_key="k",
            watchdog_lock_timeout=9,
            extra_option="keep me",
        )
        build_mock.assert_called_once_with(
            lock_type="none",
            lock_key="k",
            lock_timeout=9,
            default_lock_key=None,
        )
