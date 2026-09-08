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
from __future__ import annotations

import logging
import typing as tp

from gcl_looper import utils
from gcl_looper.watchdogs import exceptions as exc
from gcl_looper.watchdogs.locks import base as locks_base

LOG = logging.getLogger(__name__)

#: Builtin drivers shipped with gcl_looper.
#:
#: Adding a new database (e.g. MySQL) is a two-step task:
#:
#: 1. implement a :class:`~gcl_looper.watchdogs.locks.base.BaseLockDriver`
#:    subclass (for the table-based locks it is usually enough to subclass
#:    :class:`~gcl_looper.watchdogs.locks.table.TableLockDriver` and override
#:    the SQL flavor methods);
#: 2. register it here (or via :func:`register_driver` from the application
#:    code before the watchdog is built) so it can be referenced by name from
#:    the configuration file.
BUILTIN_DRIVERS: tp.Dict[str, str] = {
    "postgres_advisory": (
        "gcl_looper.watchdogs.locks.pg_advisory:PostgresAdvisoryLockDriver"
    ),
    "postgres_table": "gcl_looper.watchdogs.locks.pg_table:PostgresTableLockDriver",
}

_driver_registry: tp.Dict[str, str] = dict(BUILTIN_DRIVERS)


def register_driver(name: str, driver_class_path: str) -> None:
    """Register (or override) a lock driver under the given name.

    Args:
        name: The name used in the configuration file.
        driver_class_path: Import path of the driver class, e.g.
            ``my_package.locks:MySQLNamedLockDriver``.
    """
    LOG.debug("Registering lock driver %r -> %s", name, driver_class_path)
    _driver_registry[name] = driver_class_path


def available_drivers() -> tp.Tuple[str, ...]:
    """Names of all currently registered lock drivers."""
    return tuple(sorted(_driver_registry))


def get_driver_class(name: str) -> tp.Type[locks_base.BaseLockDriver]:
    """Resolve the registered lock driver name to the driver class.

    The driver module is imported lazily so the database client libraries
    (psycopg and friends) are only required when the driver is actually
    used.
    """
    try:
        driver_path = _driver_registry[name]
    except KeyError:
        raise exc.LockDriverNotFound(
            driver=name, allowed=", ".join(available_drivers())
        ) from None

    driver_class = utils.cfg_load_module_attr(driver_path)

    if not (
        isinstance(driver_class, type)
        and issubclass(driver_class, locks_base.BaseLockDriver)
    ):
        raise TypeError(
            "Lock driver %r must be a BaseLockDriver subclass; got: %r"
            % (name, driver_class)
        )
    return driver_class


def create_driver(name: str, **kwargs: tp.Any) -> locks_base.BaseLockDriver:
    """Instantiate the registered lock driver by its name."""
    driver_class = get_driver_class(name)
    return driver_class(**kwargs)
