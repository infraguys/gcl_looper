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
import time
import typing as tp

from gcl_looper.watchdogs import exceptions as exc

LOG = logging.getLogger(__name__)


class WatchDogBase:
    """Base watchdog.

    A fully functional watchdog that does nothing: it is always alive and it
    is always the master. It should be subclassed for the real watchdog
    checks.

    The watchdog is used as a context manager. The service wraps every
    iteration with ``with watchdog:`` so that:

    * ``__enter__`` runs the health check (which, for the locking watchdogs,
      also (re)acquires the lock) and raises
      :class:`~gcl_looper.watchdogs.exceptions.WatchDogMinorException`
      if the iteration should be skipped;
    * ``__exit__`` leaves the context.

    This is a port of ``rooster.watchdogs.base.WatchDogBase`` adapted for the
    gcl_looper single-loop services (the multiprocessing shared state is not
    needed anymore as every process owns its own watchdog instance).
    """

    def __init__(self) -> None:
        super(WatchDogBase, self).__init__()
        self._failed = False
        self._in_context_local = False
        self._in_context_value = False

    @property
    def _in_context(self) -> bool:
        return self._in_context_value or self._in_context_local

    @_in_context.setter
    def _in_context(self, value: bool) -> None:
        self._in_context_value = value

    @property
    def is_master(self) -> bool:
        """Whether the watchdog currently holds the leadership.

        The base watchdog does not handle any lock so it is always the master.
        """
        return True

    def _on_enter(self) -> None:
        pass

    def __enter__(self) -> WatchDogBase:
        try:
            self._in_context_local = True
            self._on_enter()
            self._check_health()
            self.generate_heartbeat()
            self._in_context = True
        finally:
            self._in_context_local = False
        return self

    def __exit__(
        self,
        exc_type: tp.Optional[tp.Type[BaseException]],
        exc_val: tp.Optional[BaseException],
        exc_tb: tp.Any,
    ) -> None:
        self._in_context = False

    def _check_health(self) -> None:
        if self._failed:
            raise exc.ServiceIsMarkedFailed()

    def is_alive(self) -> bool:
        """Check if the watchdog is alive (always returns a boolean)."""
        try:
            self._check_health()
            LOG.debug("The service is alive.")
            return True
        except exc.WatchDogMinorException as e:
            LOG.info("The service is not alive due to %r", e)
        except Exception:
            LOG.exception("Unexpected error during health check:")
            return False
        return False

    def mark_failed(self) -> None:
        """Manually mark the watchdog as failed."""
        self._failed = True
        LOG.info("Watchdog was manually marked as failed")

    def generate_heartbeat(self) -> None:
        """Generate the watchdog heartbeat."""
        pass

    def teardown(self) -> None:
        """Gracefully teardown the watchdog (release resources)."""
        pass


class TimedWatchDog(WatchDogBase):
    """Time-based watchdog.

    Handles a shared timer to detect stale services with outdated heartbeat
    timestamps. This is the gcl_looper port of ``rooster.watchdogs.base.
    WatchDog``.
    """

    def __init__(self, heartbeat_timeout: float) -> None:
        super(TimedWatchDog, self).__init__()
        self._heartbeat_lock = threading.Lock()
        self._last_heartbeat = time.time()
        self._heartbeat_timeout = heartbeat_timeout

    def _check_health(self) -> None:
        super(TimedWatchDog, self)._check_health()
        with self._heartbeat_lock:
            curr_time = time.time()
            last_heartbeat = self._last_heartbeat
        delta = curr_time - last_heartbeat
        if delta >= self._heartbeat_timeout:
            raise exc.ServiceHeartbeatTimeout(
                timeout=self._heartbeat_timeout,
                delta=delta,
                last_heartbeat=last_heartbeat,
                check_time=curr_time,
            )

    def generate_heartbeat(self) -> None:
        super(TimedWatchDog, self).generate_heartbeat()
        with self._heartbeat_lock:
            self._last_heartbeat = time.time()
        LOG.debug("Heartbeat time record has been updated.")
