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

from gcl_looper.election import always as always_elector
from gcl_looper.election import config as election_config
from gcl_looper.election import db as db_elector
from gcl_looper.election import exceptions as exc
from gcl_looper.election import factory as elector_factory


class TestFactory:
    def test_always(self):
        elector = elector_factory.get_elector(elector_factory.ALWAYS_TYPE)
        assert isinstance(elector, always_elector.AlwaysLeader)
        assert elector.is_leader is True

    def test_none_type_is_always(self):
        assert isinstance(
            elector_factory.get_elector(None), always_elector.AlwaysLeader
        )

    def test_db_elector(self):
        elector = elector_factory.get_elector(
            "postgres_table",
            connection_url="postgresql://u:p@h/d",
            lock_key="k",
            refresh_interval=0.5,
        )
        assert isinstance(elector, db_elector.DbLeaderElector)
        assert elector.driver.lock_key == "k"
        assert elector._refresh_interval == 0.5

    def test_db_elector_drops_unknown_driver_params(self):
        # `lock_timeout` is accepted by postgres_table, `application_name`
        # is silently dropped by the driver kwargs filter.
        elector = elector_factory.get_elector(
            "postgres_table",
            connection_url="postgresql://u:p@h/d",
            lock_key="k",
            lock_timeout=5,
            application_name="whatever",
        )
        assert elector.driver._lock_timeout == 5

    def test_table_elector_gets_refresh_interval(self):
        elector = elector_factory.get_elector(
            "postgres_table",
            connection_url="postgresql://u:p@h/d",
            lock_key="k",
            lock_timeout=30,
        )
        assert elector._refresh_interval == 2

    def test_advisory_elector_gets_refresh_interval(self):
        elector = elector_factory.get_elector(
            "postgres_advisory",
            connection_url="postgresql://u:p@h/d",
            lock_key="k",
            lock_timeout=30,
        )
        assert elector._refresh_interval == 2

    def test_table_refresh_interval_must_be_less_than_lock_timeout(self):
        with pytest.raises(ValueError, match="less than"):
            elector_factory.get_elector(
                "postgres_table",
                connection_url="postgresql://u:p@h/d",
                lock_key="k",
                lock_timeout=2,
                refresh_interval=2,
            )

    def test_unknown_backend_raises(self):
        with pytest.raises(exc.ElectorNotFound):
            elector_factory.get_elector(
                "no_such_driver",
                connection_url="postgresql://u:p@h/d",
                lock_key="k",
            )

    def test_db_requires_driver(self):
        with pytest.raises(ValueError):
            elector_factory.create_db_elector(connection_url="x", lock_key="y")

    def test_lock_key_required_by_driver(self):
        with pytest.raises(ValueError):
            elector_factory.get_elector(
                "postgres_table",
                connection_url="postgresql://u:p@h/d",
                lock_key="",
            )


class TestBuildElector:
    def test_none_returns_no_elector(self):
        assert election_config.build_elector(backend="none") is None
        assert election_config.build_elector(backend="") is None

    def test_always(self):
        elector = election_config.build_elector(backend="always")
        assert isinstance(elector, always_elector.AlwaysLeader)

    def test_db_requires_connection_url(self):
        with pytest.raises(ValueError):
            election_config.build_elector(backend="postgres_table", lock_key="k")

    def test_db_requires_lock_key(self):
        with pytest.raises(ValueError):
            election_config.build_elector(
                backend="postgres_table",
                connection_url="postgresql://u:p@h/d",
            )

    def test_default_lock_key_used(self):
        elector = election_config.build_elector(
            backend="postgres_table",
            connection_url="postgresql://u:p@h/d",
            default_lock_key="service_default_lock",
        )
        assert elector.driver.lock_key == "service_default_lock"

    def test_explicit_lock_key_wins(self):
        elector = election_config.build_elector(
            backend="postgres_table",
            connection_url="postgresql://u:p@h/d",
            lock_key="explicit",
            default_lock_key="default",
        )
        assert elector.driver.lock_key == "explicit"


class TestBuildFromKwargs:
    def test_disabled_removes_opts_returns_none(self):
        elector, rest = election_config.build_elector_from_kwargs(
            election_backend="none",
            name="svc",
            iter_min_period=2,
        )
        assert elector is None
        assert rest == {"name": "svc", "iter_min_period": 2}

    def test_db_elector_built_from_kwargs(self):
        elector, rest = election_config.build_elector_from_kwargs(
            election_backend="postgres_advisory",
            election_connection_url="postgresql://u:p@h/d",
            election_lock_key="k",
            election_refresh_interval=0.5,
            name="svc",
        )
        assert isinstance(elector, db_elector.DbLeaderElector)
        assert elector._refresh_interval == 0.5
        assert elector.driver.lock_key == "k"
        assert rest == {"name": "svc"}

    def test_empty_prefix_dedicated_section(self):
        elector, rest = election_config.build_elector_from_kwargs(
            prefix="",
            backend="always",
        )
        assert isinstance(elector, always_elector.AlwaysLeader)
        assert rest == {}


class TestConfigOpts:
    def test_opts_registered_and_parsed(self):
        opts = election_config.get_config_opts()
        assert len(opts) == len(election_config.ELECTION_OPTS)

        conf = cfg.ConfigOpts()
        conf.register_cli_opts(election_config.get_config_opts(), "svc")
        conf(
            args=[
                "--svc-election_backend",
                "always",
                "--svc-election_refresh_interval",
                "0.5",
            ]
        )
        assert conf["svc"].election_backend == "always"
        assert conf["svc"].election_refresh_interval == 0.5

    def test_connection_url_is_secret(self):
        opts = election_config.get_config_opts()
        url_opt = next(o for o in opts if o.name == "election_connection_url")
        assert url_opt.secret is True

    @mock.patch("gcl_looper.election.config.build_elector")
    def test_build_elector_kwargs_passthrough(self, build_mock):
        build_mock.return_value = None
        election_config.build_elector_from_kwargs(
            election_backend="none",
            election_lock_key="k",
            election_refresh_interval=0.5,
            election_lock_timeout=9,
            extra_option="keep me",
        )
        build_mock.assert_called_once_with(
            backend="none",
            lock_key="k",
            refresh_interval=0.5,
            lock_timeout=9,
            default_lock_key=None,
        )
