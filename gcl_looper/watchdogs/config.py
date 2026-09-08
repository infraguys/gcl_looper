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
"""Oslo configuration glue for the watchdogs.

The options are defined with a configurable prefix (``watchdog_`` by default)
so that a service can register them straight into its own launchpad section
and pass the parsed values through to the constructor::

    from gcl_looper.watchdogs import config as watchdogs_config
    from gcl_looper.watchdogs import factory as watchdogs_factory
    from gcl_looper.services import basic

    class MyMasterService(basic.BasicService):
        @classmethod
        def svc_get_config_opts(cls):
            return [
                # ... own options ...
                *watchdogs_config.get_config_opts(),
            ]

        def __init__(self, name, **kwargs):
            watchdog, kwargs = watchdogs_config.build_watchdog_from_kwargs(
                default_lock_key=f"{name}_lock", **kwargs
            )
            super().__init__(watchdog=watchdog, **kwargs)

with the ini file:

    [my_package.my_module:MyMasterService]
    name = my_master
    watchdog_lock_type = postgres_advisory
    watchdog_connection_url = postgresql://user:pass@dbhost:5432/mydb
"""

from __future__ import annotations

import logging
import typing as tp

from oslo_config import cfg

from gcl_looper.watchdogs import factory as wd_factory
from gcl_looper.watchdogs import base as wd_base
from gcl_looper.watchdogs.locks import registry as locks_registry

LOG = logging.getLogger(__name__)

LOCK_TYPE_NONE = "none"

#: All watchdog options (names without the prefix).
WATCHDOG_OPTS = (
    "lock_type",
    "connection_url",
    "lock_key",
    "heartbeat_timeout",
    "lock_timeout",
    "table_name",
    "create_table",
)

DEFAULT_TABLE_NAME = "gcl_looper_locks"


def get_config_opts(prefix: str = "watchdog_") -> tp.List[cfg.Opt]:
    """Return the oslo config options of the master election watchdog.

    Args:
        prefix: Option name prefix. Use ``""`` to register the options into a
            dedicated config section and ``watchdog_`` (the default) to mix
            them into the options of a service.
    """
    return [
        cfg.StrOpt(
            prefix + "lock_type",
            default=LOCK_TYPE_NONE,
            help=(
                "Master election watchdog type. "
                "`none` (default) disables the watchdog (the service always "
                "acts as the master). "
                "`dummy`/`timed` select the non-database watchdogs. "
                "Any registered lock driver selects the database master "
                "election: %s. A new database backend (e.g. MySQL) can be "
                "registered with gcl_looper.watchdogs.locks.register_driver."
                % ", ".join(locks_registry.available_drivers())
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
        cfg.IntOpt(
            prefix + "heartbeat_timeout",
            default=120,
            min=1,
            help=(
                "The heartbeat timeout of the time-based watchdog part, "
                "seconds. The lock itself is refreshed on every service "
                "iteration."
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
    """Extract the watchdog options from the constructor kwargs."""
    return {
        name: params.pop(prefix + name)
        for name in WATCHDOG_OPTS
        if prefix + name in params
    }


def build_watchdog_from_kwargs(
    prefix: str = "watchdog_",
    default_lock_key: tp.Optional[str] = None,
    **kwargs: tp.Any,
) -> tp.Tuple[tp.Optional[wd_base.WatchDogBase], tp.Dict[str, tp.Any]]:
    """Build the watchdog from (oslo parsed) configuration keyword arguments.

    Consumes the watchdog options from ``kwargs`` (the names are built with
    ``prefix``), builds the watchdog and returns it together with the
    remaining kwargs, so a service constructor can pass everything else to
    ``super().__init__()``.

    Returns:
        tuple: ``(watchdog or None, remaining_kwargs)``.
    """
    opts = _pop_opts(prefix, kwargs)
    watchdog = build_watchdog(
        default_lock_key=default_lock_key,
        **opts,
    )
    return watchdog, kwargs


def build_watchdog(
    lock_type: str = LOCK_TYPE_NONE,
    connection_url: tp.Optional[str] = None,
    lock_key: tp.Optional[str] = None,
    heartbeat_timeout: int = 120,
    lock_timeout: int = 30,
    table_name: str = DEFAULT_TABLE_NAME,
    create_table: bool = True,
    default_lock_key: tp.Optional[str] = None,
) -> tp.Optional[wd_base.WatchDogBase]:
    """Build a watchdog instance from plain configuration values.

    Returns ``None`` when the watchdog is disabled (``lock_type`` is ``none``
    or empty).
    """
    if not lock_type or lock_type == LOCK_TYPE_NONE:
        return None

    if lock_type == wd_factory.TIMED_TYPE:
        return wd_factory.create_timed_watchdog(heartbeat_timeout=heartbeat_timeout)
    if lock_type == wd_factory.DUMMY_TYPE:
        return wd_factory.create_dummy_watchdog()

    # A database lock driver.
    if not connection_url:
        raise ValueError(
            "The `connection_url` option is required for the master election "
            "watchdog (lock_type=%r)" % lock_type
        )

    lock_key = lock_key or default_lock_key
    if not lock_key:
        raise ValueError(
            "The `lock_key` option is required for the master election "
            "watchdog (lock_type=%r)" % lock_type
        )

    return wd_factory.create_db_watchdog(
        lock_driver=lock_type,
        heartbeat_timeout=heartbeat_timeout,
        connection_url=connection_url,
        lock_key=lock_key,
        lock_timeout=lock_timeout,
        table_name=table_name,
        create_table=create_table,
    )
