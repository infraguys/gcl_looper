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
"""Oslo configuration glue for the leader election.

The options are defined with a configurable prefix (``election_`` by
default) so that a service can register them straight into its own
launchpad section and pass the parsed values through to the constructor::

    from gcl_looper.election import config as election_config
    from gcl_looper.services import basic

    class MyMasterService(basic.BasicService):
        @classmethod
        def svc_get_config_opts(cls):
            return [
                # ... own options ...
                *election_config.get_config_opts(),
            ]

        def __init__(self, name, **kwargs):
            elector, kwargs = election_config.build_elector_from_kwargs(
                default_lock_key=f"{name}_lock", **kwargs
            )
            super().__init__(elector=elector, **kwargs)

with the ini file:

    [my_package.my_module:MyMasterService]
    name = my_master
    election_backend = postgres_advisory
    election_connection_url = postgresql://user:pass@dbhost:5432/mydb
"""

from __future__ import annotations

import logging
import typing as tp

from oslo_config import cfg

from gcl_looper.election import base as elector_base
from gcl_looper.election import factory as elector_factory
from gcl_looper.election.drivers import registry as drivers_registry

LOG = logging.getLogger(__name__)

BACKEND_NONE = "none"

#: All election options (names without the prefix).
ELECTION_OPTS = (
    "backend",
    "connection_url",
    "lock_key",
    "refresh_interval",
    "lock_timeout",
    "table_name",
    "create_table",
)

DEFAULT_TABLE_NAME = "gcl_looper_locks"


def get_config_opts(prefix: str = "election_") -> tp.List[cfg.Opt]:
    """Return the oslo config options of the leader election.

    Args:
        prefix: Option name prefix. Use ``""`` to register the options
            into a dedicated config section and ``election_`` (the
            default) to mix them into the options of a service.
    """
    return [
        cfg.StrOpt(
            prefix + "backend",
            default=BACKEND_NONE,
            help=(
                "Leader election backend. "
                "`none` (default) disables the election (the service always "
                "acts as the master). "
                "`always` selects the no-coordination elector. "
                "Any registered lock driver selects the database master "
                "election: %s. A new database backend (e.g. MySQL) can be "
                "registered with gcl_looper.election.drivers.register_driver."
                % ", ".join(drivers_registry.available_drivers())
            ),
        ),
        cfg.StrOpt(
            prefix + "connection_url",
            default=None,
            secret=True,
            help=(
                "Connection URL to the database used for the master "
                "election, e.g. postgresql://user:password@host:5432/dbname"
            ),
        ),
        cfg.StrOpt(
            prefix + "lock_key",
            default=None,
            help=(
                "Name of the lock the service instances compete for. All "
                "the instances sharing one lock_key elect one master. "
                "Required unless a service supplies its own default."
            ),
        ),
        cfg.FloatOpt(
            prefix + "refresh_interval",
            default=2.0,
            min=0.1,
            help=(
                "Minimum number of seconds between database lock ownership "
                "checks while the service is master."
            ),
        ),
        cfg.IntOpt(
            prefix + "lock_timeout",
            default=30,
            min=1,
            help=(
                "Table-based locks only: the number of seconds after which "
                "a lock stopped being refreshed by its owner is considered "
                "abandoned and can be taken over by another node. "
                "Ignored by the session-level locks (e.g. postgres_advisory) "
                "which are released instantly on connection loss."
            ),
        ),
        cfg.StrOpt(
            prefix + "table_name",
            default=DEFAULT_TABLE_NAME,
            help=(
                "Table-based locks only: the name of the table storing the "
                "locks (created automatically if absent)."
            ),
        ),
        cfg.BoolOpt(
            prefix + "create_table",
            default=True,
            help=(
                "Table-based locks only: create the locks table on demand "
                "if it does not exist."
            ),
        ),
    ]


def _pop_opts(prefix: str, params: tp.Dict[str, tp.Any]) -> tp.Dict[str, tp.Any]:
    """Extract the election options from the constructor kwargs."""
    return {
        name: params.pop(prefix + name)
        for name in ELECTION_OPTS
        if prefix + name in params
    }


def build_elector_from_kwargs(
    prefix: str = "election_",
    default_lock_key: tp.Optional[str] = None,
    **kwargs: tp.Any,
) -> tp.Tuple[tp.Optional[elector_base.LeaderElector], tp.Dict[str, tp.Any]]:
    """Build the elector from (oslo parsed) configuration keyword arguments.

    Consumes the election options from ``kwargs`` (the names are built with
    ``prefix``), builds the elector and returns it together with the
    remaining kwargs, so a service constructor can pass everything else
    to ``super().__init__()``.

    Returns:
        tuple: ``(elector or None, remaining_kwargs)``.
    """
    opts = _pop_opts(prefix, kwargs)
    elector = build_elector(
        default_lock_key=default_lock_key,
        **opts,
    )
    return elector, kwargs


def build_elector(
    backend: str = BACKEND_NONE,
    connection_url: tp.Optional[str] = None,
    lock_key: tp.Optional[str] = None,
    refresh_interval: float = 2.0,
    lock_timeout: int = 30,
    table_name: str = DEFAULT_TABLE_NAME,
    create_table: bool = True,
    default_lock_key: tp.Optional[str] = None,
) -> tp.Optional[elector_base.LeaderElector]:
    """Build an elector instance from plain configuration values.

    Returns ``None`` when the election is disabled (``backend`` is
    ``none`` or empty).
    """
    if not backend or backend == BACKEND_NONE:
        return None

    if backend == elector_factory.ALWAYS_TYPE:
        return elector_factory.create_always_elector()

    # A database lock driver.
    if not connection_url:
        raise ValueError(
            "The `connection_url` option is required for the master "
            "election (backend=%r)" % backend
        )

    lock_key = lock_key or default_lock_key
    if not lock_key:
        raise ValueError(
            "The `lock_key` option is required for the master "
            "election (backend=%r)" % backend
        )

    return elector_factory.create_db_elector(
        lock_driver=backend,
        refresh_interval=refresh_interval,
        connection_url=connection_url,
        lock_key=lock_key,
        lock_timeout=lock_timeout,
        table_name=table_name,
        create_table=create_table,
    )
