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
"""Watchdog factory.

A port of ``rooster.watchdogs.factory`` extended with the database lock
driver selection. The watchdog type is a plain string so it can come straight
from the configuration file::

    [my_service:MyService]
    lock_type = postgres_advisory
    connection_url = postgresql://user:pass@dbhost:5432/mydb
    lock_key = my_master_service

    [my_service:MyService]        # table-based flavor
    lock_type = postgres_table
    lock_timeout = 30
"""

from __future__ import annotations

import inspect
import logging
import typing as tp

from gcl_looper.watchdogs import base as wd_base
from gcl_looper.watchdogs import database as wd_db
from gcl_looper.watchdogs.locks import base as locks_base
from gcl_looper.watchdogs.locks import registry as locks_registry

LOG = logging.getLogger(__name__)

DUMMY_TYPE = "dummy"
TIMED_TYPE = "timed"

NON_DB_WATCHDOG_TYPES = (DUMMY_TYPE, TIMED_TYPE)


def create_dummy_watchdog(**kwargs: tp.Any) -> wd_base.WatchDogBase:
    """Create a watchdog that does nothing (always the master)."""
    return wd_base.WatchDogBase()


def create_timed_watchdog(
    heartbeat_timeout: float = wd_db.DEFAULT_HEARTBEAT_TIMEOUT, **kwargs: tp.Any
) -> wd_base.TimedWatchDog:
    """Create a watchdog that relies on the heartbeat timing only."""
    return wd_base.TimedWatchDog(heartbeat_timeout=heartbeat_timeout)


def _driver_kwargs(
    driver_class: tp.Type[locks_base.BaseLockDriver],
    params: tp.Dict[str, tp.Any],
) -> tp.Dict[str, tp.Any]:
    """Keep only the parameters accepted by the driver constructor."""
    signature = inspect.signature(driver_class.__init__)
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return dict(params)
    accepted = set(signature.parameters) - {"self"}
    return {k: v for k, v in params.items() if k in accepted}


def create_db_watchdog(
    heartbeat_timeout: float = wd_db.DEFAULT_HEARTBEAT_TIMEOUT,
    **params: tp.Any,
) -> wd_db.DbWatchDog:
    """Create a database-lock (master election) watchdog.

    The ``lock_driver`` parameter selects the registered driver by name (see
    ``gcl_looper.watchdogs.locks.registry``); the rest of the parameters are
    passed to the driver constructor (unknown ones are dropped, so it is safe
    to pass the full configuration set to any driver).
    """
    try:
        driver_name = params.pop("lock_driver")
    except KeyError:
        raise ValueError(
            "`lock_driver` must be specified for the database watchdog; "
            "available drivers: %s" % ", ".join(locks_registry.available_drivers())
        ) from None

    if not driver_name:
        raise ValueError("`lock_driver` must not be empty")

    driver_class = locks_registry.get_driver_class(driver_name)
    driver = driver_class(**_driver_kwargs(driver_class, params))

    # Table-based locks expire after `lock_timeout`; a background refresh
    # thread keeps the lease alive during long iterations. Session-level
    # locks (advisory) do not expire and do not need it.
    refresh_interval: tp.Optional[float] = None
    lock_timeout = params.get("lock_timeout")
    if lock_timeout and hasattr(driver, "_lock_timeout"):
        refresh_interval = max(float(lock_timeout) / 3.0, 1.0)

    LOG.info(
        "Created database watchdog: driver=%s lock_key=%r",
        driver_name,
        driver.lock_key,
    )
    return wd_db.DbWatchDog(
        driver=driver,
        heartbeat_timeout=heartbeat_timeout,
        refresh_interval=refresh_interval,
    )


def get_watchdog(
    watchdog_type: tp.Optional[str], **kwargs: tp.Any
) -> wd_base.WatchDogBase:
    """Return a watchdog instance depending on the requested type.

    ``watchdog_type`` is either a special type (``dummy``, ``timed``) or the
    name of a registered database lock driver (``postgres_advisory``,
    ``postgres_table``, ...).
    """
    if watchdog_type is None or watchdog_type == DUMMY_TYPE:
        return create_dummy_watchdog(**kwargs)

    if watchdog_type == TIMED_TYPE:
        return create_timed_watchdog(**kwargs)

    # Anything else must be a registered lock driver.
    return create_db_watchdog(lock_driver=watchdog_type, **kwargs)
