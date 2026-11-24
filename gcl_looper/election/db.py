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
"""Database-backed leader elector.

This is the core master-election primitive, extracted from the original
``rooster``/``node-manager`` watchdog. It wraps a database lock driver
(:class:`~gcl_looper.election.drivers.base.BaseLockDriver`) and turns it
into a :class:`~gcl_looper.election.base.LeaderElector`: the process that
owns the database lock is the master.

The elector is driven by the service loop: :meth:`try_lead` is called once
per iteration and both acquires and keeps the leadership refreshed. Lock
checks are throttled by ``refresh_interval`` and no automatic refresh runs
during a business iteration. Long-running work can explicitly call
:meth:`ensure_leadership` to verify and refresh its ownership.

This class is *pure election*: it has no heartbeat timer and no notion of
process liveness.
"""

from __future__ import annotations

import logging
import time
import typing as tp

from gcl_looper.election import base
from gcl_looper.election import exceptions as exc
from gcl_looper.election.drivers import base as drivers_base

LOG = logging.getLogger(__name__)

DEFAULT_REFRESH_INTERVAL = 2.0


class DbLeaderElector(base.LeaderElector):
    """Leader elector backed by a distributed database lock.

    Args:
        driver: The concrete lock driver (advisory/table, PostgreSQL/...).
        refresh_interval: Minimum interval (seconds) between backing-store
            lock ownership checks while this node is the master. It must be
            strictly less than the driver's ``lock_timeout`` (for the
            table-based locks) so the master refreshes the lock before it
            can be considered abandoned and taken over.
    """

    def __init__(
        self,
        driver: drivers_base.BaseLockDriver,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
    ) -> None:
        super(DbLeaderElector, self).__init__()
        if refresh_interval <= 0:
            raise ValueError("`refresh_interval` must be greater than 0")
        lock_timeout = getattr(driver, "lock_timeout", None)
        if lock_timeout is not None and refresh_interval >= lock_timeout:
            raise ValueError(
                "`refresh_interval` must be less than the lock `lock_timeout`"
            )
        self._driver = driver
        self._leader = False
        self._refresh_interval = float(refresh_interval)
        self._next_refresh_at = 0.0

    @property
    def driver(self) -> drivers_base.BaseLockDriver:
        return self._driver

    @property
    def lock_key(self) -> tp.Optional[str]:
        return self._driver.lock_key

    @property
    def is_leader(self) -> bool:
        """Whether this node currently owns the master lock."""
        return self._leader

    def _acquire(self) -> bool:
        was_leader = self._leader
        try:
            acquired = self._driver.acquire()
        except exc.ElectionError:
            self._leader = False
            self._next_refresh_at = 0.0
            raise

        self._leader = acquired
        self._next_refresh_at = (
            time.monotonic() + self._refresh_interval if acquired else 0.0
        )
        if was_leader and not acquired:
            LOG.info("Lost the master lock %r", self._driver.lock_key)
        return acquired

    def try_lead(self) -> bool:
        """Acquire or keep the master lock (throttled, non-blocking).

        Returns ``True`` when this node is the master after the call.
        Raises :class:`BackendError` when the backing store is unreachable
        (the leadership is lost).
        """
        now = time.monotonic()
        if self._leader and now < self._next_refresh_at:
            return True
        return self._acquire()

    def ensure_leadership(self) -> None:
        """Verify ownership with the backend and raise when it was lost."""
        if not self._leader:
            raise exc.LeadershipLostError(lock_key=self._driver.lock_key)
        if not self._acquire():
            raise exc.LeadershipLostError(lock_key=self._driver.lock_key)

    def close(self) -> None:
        """Best-effort release of the lock and the DB resources."""
        try:
            self._driver.release()
        except Exception as e:  # pragma: no cover - driver release is lenient
            LOG.warning("Failed to release lock %r: %r", self._driver.lock_key, e)
        finally:
            try:
                self._driver.close()
            except Exception as e:  # pragma: no cover - defensive
                LOG.warning("Failed to close lock driver: %r", e)
        self._leader = False
        self._next_refresh_at = 0.0
        return None

    def __repr__(self) -> str:
        return "<DbLeaderElector lock=%r leader=%s>" % (
            self._driver.lock_key,
            self._leader,
        )


def make_db_elector(
    driver: drivers_base.BaseLockDriver,
    refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
) -> DbLeaderElector:
    return DbLeaderElector(driver=driver, refresh_interval=refresh_interval)
