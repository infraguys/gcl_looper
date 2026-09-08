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
"""Table-based master election lock (portable across databases).

The lock lives in a database table row. A worker owns the lock by writing its
unique ``worker_id`` and a ``locked_at`` timestamp into the row with a single
*atomic* ``UPDATE`` that only succeeds when the row is free, expired (the
previous owner stopped refreshing it), or already owned by this very worker.

This is a direct port of the ``WatchDogTableMysql`` idea from the original
node-manager/rooster stack. Because the mechanics rely on plain
``INSERT``/``UPDATE`` statements only, they are trivial to port between
databases: a new backend is created by subclassing
:class:`TableLockDriver` and implementing the connection and SQL-flavor hooks
(see :class:`gcl_looper.watchdogs.locks.pg_table.PostgresTableLockDriver`).
"""

from __future__ import annotations

import abc
import logging
import os
import typing as tp
import uuid

from gcl_looper.watchdogs import exceptions as exc
from gcl_looper.watchdogs.locks import base as locks_base

LOG = logging.getLogger(__name__)

DEFAULT_LOCK_TIMEOUT = 30


class TableLockDriver(locks_base.BaseLockDriver, abc.ABC):
    """Generic table-based distributed lock.

    Args:
        connection_url: DSN/connection URL of the database.
        lock_key: Logical lock (row) name.
        lock_timeout: Seconds after which an unrefreshed lock is considered
            abandoned and can be taken over by another worker. It must be
            clearly greater than the master heartbeat/refresh interval.
        table_name: Name of the table that stores the locks.
        create_table: Create the table on demand if it does not exist.
    """

    def __init__(
        self,
        connection_url: str,
        lock_key: str,
        lock_timeout: int = DEFAULT_LOCK_TIMEOUT,
        table_name: str = "gcl_looper_locks",
        create_table: bool = True,
    ) -> None:
        super(TableLockDriver, self).__init__(
            connection_url=connection_url, lock_key=lock_key
        )
        if lock_timeout <= 0:
            raise ValueError("`lock_timeout` must be greater than 0")

        self._lock_timeout = lock_timeout
        self._table_name = locks_base.validate_identifier(table_name)
        self._create_table = create_table
        self._worker_id = str(uuid.uuid4())

        self._conn: tp.Any = None
        self._conn_pid: tp.Optional[int] = None
        self._table_ready = False

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def table_name(self) -> str:
        return self._table_name

    # --- public driver interface ------------------------------------------

    def acquire(self) -> bool:
        """Acquire or refresh the lock (non-blocking).

        Returns ``True`` when this worker owns the lock after the call.
        """
        self._ensure_ready()
        try:
            affected = self._execute(self._sql_acquire(), self._acquire_params())
        except exc.LockConnectionError:
            # The connection died mid-way: we can no longer assume ownership.
            self._drop_connection()
            self._held = False
            raise

        self._held = affected == 1
        if self._held:
            LOG.debug(
                "Acquired table lock %r by worker %s", self._lock_key, self._worker_id
            )
        return self._held

    def release(self) -> None:
        """Release the lock if it is still owned by this worker."""
        if self._conn is None:
            # No live connection: an advisory-style release is impossible, but
            # the table lock will simply expire after `lock_timeout`.
            self._held = False
            return
        try:
            self._execute(self._sql_release(), self._release_params())
        except exc.LockConnectionError as e:
            LOG.warning(
                "Failed to release table lock %r: %r (it will expire in %ss)",
                self._lock_key,
                e,
                self._lock_timeout,
            )
        self._held = False

    def close(self) -> None:
        self._drop_connection()
        self._table_ready = False

    # --- connection handling (lazy + fork safe) ----------------------------

    def _ensure_connection(self) -> tp.Any:
        """Return a live connection, (re)connecting when needed.

        The connection is opened lazily on first use *after* the service has
        been started. If the process forked (ProcessHubService) between the
        object creation and the first lock operation, the inherited connection
        is discarded and a fresh one is opened in the child process.
        """
        current_pid = os.getpid()
        if self._conn is not None and self._conn_pid == current_pid:
            return self._conn

        if self._conn is not None and self._conn_pid != current_pid:
            # Inherited from the parent process after a fork: drop the
            # reference without touching the socket (the parent owns it).
            self._conn = None
            self._table_ready = False

        self._conn = self._connect()
        self._conn_pid = current_pid
        return self._conn

    def _drop_connection(self) -> None:
        conn, self._conn = self._conn, None
        self._conn_pid = None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover - best effort
                pass

    def _ensure_ready(self) -> None:
        self._ensure_connection()
        if self._create_table and not self._table_ready:
            # Concurrent workers may race to create the table; retry once
            # (e.g. on a catalog-level "tuple concurrently updated" error)
            # before reporting a hard failure.
            try:
                self._execute(self._sql_create_table(), ())
            except exc.LockConnectionError:
                LOG.debug(
                    "Table lock create attempt failed, retrying once",
                    exc_info=True,
                )
                self._execute(self._sql_create_table(), ())
            self._table_ready = True
        # Ensure the lock row exists so the acquire UPDATE can match it.
        self._execute(self._sql_ensure_row(), self._ensure_row_params())

    # --- DB API ------------------------------------------------------------

    @abc.abstractmethod
    def _connect(self) -> tp.Any:
        """Open and return a DB-API connection in autocommit mode."""
        raise NotImplementedError()

    @abc.abstractmethod
    def _execute(self, sql: str, params: tp.Sequence[tp.Any]) -> int:
        """Execute a single statement and return the affected row count.

        Must translate client errors into
        :class:`~gcl_looper.watchdogs.exceptions.LockConnectionError`.
        """
        raise NotImplementedError()

    # --- SQL flavor (override for a new database) --------------------------

    @abc.abstractmethod
    def _sql_create_table(self) -> str:
        raise NotImplementedError()

    @abc.abstractmethod
    def _sql_ensure_row(self) -> str:
        raise NotImplementedError()

    @abc.abstractmethod
    def _sql_acquire(self) -> str:
        raise NotImplementedError()

    @abc.abstractmethod
    def _sql_release(self) -> str:
        raise NotImplementedError()

    def _ensure_row_params(self) -> tp.Tuple[tp.Any, ...]:
        return (self._lock_key,)

    def _acquire_params(self) -> tp.Tuple[tp.Any, ...]:
        """Placeholder values for :meth:`_sql_acquire`.

        The order must match the ``%s`` placeholders in :meth:`_sql_acquire`.
        The default matches the PostgreSQL flavor: the worker id (to write),
        the lock key, the worker id (self refresh) and the lock timeout.
        """
        return (self._worker_id, self._lock_key, self._worker_id, self._lock_timeout)

    def _release_params(self) -> tp.Tuple[tp.Any, ...]:
        return (self._lock_key, self._worker_id)
