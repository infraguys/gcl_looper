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
"""Database-agnostic interface of the master-election lock drivers.

A lock driver implements the minimal set of operations the watchdog needs to
elect exactly one *master* between service instances running on different
nodes:

* :meth:`BaseLockDriver.acquire` — a *non-blocking* attempt to acquire the
  lock or to refresh it when it is already held. Must be safe to call
  repeatedly (every service iteration calls it);
* :meth:`BaseLockDriver.release` — release the lock when the service holds
  it;
* :meth:`BaseLockDriver.close` — release the underlying DB resources.

The concrete implementations are registered by name in
``gcl_looper.watchdogs.locks.registry`` and are selected from the
configuration file, so adding a new backend (e.g. MySQL) only requires a new
driver class plus a registry entry.
"""

from __future__ import annotations

import abc
import re
import typing as tp

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def validate_identifier(name: str) -> str:
    """Validate a (optionally schema-qualified) SQL identifier.

    The identifiers are interpolated into the SQL statements, so they must be
    checked instead of escaped. Returns the name for convenience.
    """
    if not _IDENTIFIER_RE.match(name):
        raise ValueError("Invalid SQL identifier: %r" % name)
    return name


class BaseLockDriver(abc.ABC):
    """Abstract master-election lock stored in a database.

    Args:
        connection_url: DSN/connection URL of the database.
        lock_key: Logical name of the lock. The instances using the same
            ``lock_key`` compete for the same lock.
    """

    def __init__(self, connection_url: str, lock_key: str) -> None:
        super(BaseLockDriver, self).__init__()
        if not connection_url:
            raise ValueError("`connection_url` must not be empty")
        if not lock_key:
            raise ValueError("`lock_key` must not be empty")
        self._connection_url = connection_url
        self._lock_key = lock_key
        self._held = False

    @property
    def lock_key(self) -> str:
        return self._lock_key

    @property
    def connection_url(self) -> str:
        return self._connection_url

    @property
    def is_held(self) -> bool:
        """Whether *this* driver instance holds the lock (local state)."""
        return self._held

    @abc.abstractmethod
    def acquire(self) -> bool:
        """Try to acquire or refresh the lock without blocking.

        Calling it again while the lock is held must refresh/keep the lock
        instead of failing (idempotent re-acquisition).

        Returns:
            ``True`` if the lock is (still) held by this driver, ``False``
            when the lock is taken by another owner.

        Raises:
            gcl_looper.watchdogs.exceptions.LockConnectionError: when the
                database is not reachable (the driver must reset its
                internal state, the lock is lost).
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def release(self) -> None:
        """Release the lock if it is held by this driver.

        Must not raise when the lock is not held or when the database is
        unreachable: the watchdog treats the release as best-effort and the
        lock is eventually lost anyway (session end / expiration).
        """
        raise NotImplementedError()

    def close(self) -> None:
        """Close the underlying database resources."""
        pass

    def __enter__(self) -> "BaseLockDriver":
        return self

    def __exit__(self, *exc_info: tp.Any) -> None:
        self.close()
