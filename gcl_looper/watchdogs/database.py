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
import threading
import typing as tp

from gcl_looper.watchdogs import base as wd_base
from gcl_looper.watchdogs import exceptions as exc
from gcl_looper.watchdogs.locks import base as locks_base

LOG = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_TIMEOUT = 120


class DbWatchDog(wd_base.TimedWatchDog):
    """Database-lock watchdog used for master election.

    Wraps a :class:`~gcl_looper.watchdogs.locks.base.BaseLockDriver` and acts
    as a per-iteration leadership guard: every time the service enters the
    watchdog context (i.e. once per iteration) it tries to (re)acquire the
    database lock.

    * If the lock is acquired the watchdog is *alive* and
      :attr:`is_master` becomes ``True`` — the service performs the iteration.
    * If the lock is held by another node, the watchdog raises
      :class:`~gcl_looper.watchdogs.exceptions.LockAcquireFailed` (a *minor*
      exception): the service treats the iteration as skipped and keeps trying
      on the next iteration. This is exactly how the original ``rooster``
      ``SoftIrqService`` skipped iterations when the node was not the master.

    Both the timer (``heartbeat_timeout``) and the lock mechanics work
    together, mirroring the original ``WatchDogMysql``/``WatchDogTableMysql``.

    When ``refresh_interval`` is set (table-based locks that expire after
    ``lock_timeout``), a background daemon thread refreshes the lock
    periodically while the context is entered, so a long iteration does not
    lose exclusivity. Advisory (session-level) locks do not need it and
    leave ``refresh_interval`` as ``None``.

    Args:
        driver: The concrete lock driver (advisory/table, PostgreSQL/...).
        heartbeat_timeout: The heartbeat timeout passed to the time-based
            watchdog base (the lock is refreshed every iteration).
        refresh_interval: When set, a background thread refreshes the lock
            every ``refresh_interval`` seconds while the watchdog context is
            entered. Use it for table locks whose lease expires after
            ``lock_timeout``; leave ``None`` for session-level locks.
    """

    def __init__(
        self,
        driver: locks_base.BaseLockDriver,
        heartbeat_timeout: float = DEFAULT_HEARTBEAT_TIMEOUT,
        refresh_interval: tp.Optional[float] = None,
    ) -> None:
        super(DbWatchDog, self).__init__(heartbeat_timeout=heartbeat_timeout)
        self._driver = driver
        self._master = False
        self._refresh_interval = refresh_interval
        self._refresh_thread: tp.Optional[threading.Thread] = None
        self._refresh_stop = threading.Event()
        self._refresh_error: tp.Optional[BaseException] = None

    @property
    def driver(self) -> locks_base.BaseLockDriver:
        return self._driver

    @property
    def is_master(self) -> bool:
        """Whether this node currently owns the master lock."""
        return self._master

    def _check_health(self) -> None:
        # Heartbeat/failed checks first (may raise critical exceptions).
        try:
            super(DbWatchDog, self)._check_health()
        except exc.WatchDogCriticalException:
            # Critical failure (marked failed, heartbeat timeout, ...):
            # we are definitely not the master anymore. Reset the state
            # and let the critical exception propagate.
            self._master = False
            raise

        try:
            acquired = self._driver.acquire()
        except exc.WatchDogMinorException:
            self._master = False
            raise
        except exc.WatchDogCriticalException:
            # Connection lost or a hard backend error: we are definitely not
            # the master anymore. Let it propagate to the service loop.
            self._master = False
            raise

        if acquired:
            self._master = True
            return None

        if self._master:
            LOG.info("Lost the master lock %r", self._driver.lock_key)
        self._master = False
        raise exc.LockAcquireFailed(lock_key=self._driver.lock_key)

    def _start_refresh_thread(self) -> None:
        if self._refresh_interval is None or self._refresh_interval <= 0:
            return
        self._refresh_stop.clear()
        self._refresh_error = None
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            name="db-watchdog-refresh-%r" % self._driver.lock_key,
            daemon=True,
        )
        self._refresh_thread.start()

    def _stop_refresh_thread(self) -> None:
        thread = self._refresh_thread
        if thread is None or not thread.is_alive():
            self._refresh_thread = None
            return
        self._refresh_stop.set()
        thread.join(timeout=self._refresh_interval + 1)
        self._refresh_thread = None

    def _refresh_loop(self) -> None:
        while not self._refresh_stop.wait(timeout=self._refresh_interval):
            try:
                acquired = self._driver.acquire()
            except exc.WatchDogCriticalException as e:
                LOG.warning(
                    "Lock refresh failed for %r: %r",
                    self._driver.lock_key,
                    e,
                )
                self._master = False
                self._refresh_error = e
                return
            except Exception as e:  # pragma: no cover - defensive
                LOG.warning(
                    "Unexpected error refreshing lock %r: %r",
                    self._driver.lock_key,
                    e,
                )
                self._master = False
                self._refresh_error = e
                return
            if not acquired:
                LOG.info(
                    "Lost the master lock %r during refresh", self._driver.lock_key
                )
                self._master = False
                return

    def __enter__(self) -> "DbWatchDog":
        try:
            self._in_context_local = True
            self._on_enter()
            self._check_health()
            self.generate_heartbeat()
            self._in_context = True
            self._start_refresh_thread()
        finally:
            self._in_context_local = False
        return self

    def __exit__(
        self,
        exc_type: tp.Optional[tp.Type[BaseException]],
        exc_val: tp.Optional[BaseException],
        exc_tb: tp.Any,
    ) -> None:
        self._stop_refresh_thread()
        self._in_context = False

    def teardown(self) -> None:
        """Best-effort release of the lock and release of DB resources."""
        self._stop_refresh_thread()
        try:
            self._driver.release()
        except Exception as e:  # pragma: no cover - driver release is lenient
            LOG.warning("Failed to release lock %r: %r", self._driver.lock_key, e)
        finally:
            try:
                self._driver.close()
            except Exception as e:  # pragma: no cover - defensive
                LOG.warning("Failed to close lock driver: %r", e)
        self._master = False
        return None

    def __repr__(self) -> str:
        return "<DbWatchDog lock=%r master=%s>" % (
            self._driver.lock_key,
            self._master,
        )


def make_db_watchdog(
    driver: locks_base.BaseLockDriver,
    heartbeat_timeout: float = DEFAULT_HEARTBEAT_TIMEOUT,
) -> DbWatchDog:
    return DbWatchDog(driver=driver, heartbeat_timeout=heartbeat_timeout)
