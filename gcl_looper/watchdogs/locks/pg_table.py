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
"""PostgreSQL table-based master election lock.

Backend of :class:`~gcl_looper.watchdogs.locks.table.TableLockDriver` for
PostgreSQL (server 18+), using ``psycopg`` (v3), the same client library the
restalchemy/``configure_postgresql_factory`` stack relies on.

To add a MySQL (MariaDB) backend later, subclass
:class:`~gcl_looper.watchdogs.locks.table.TableLockDriver` and provide the
``mysql-connector`` connection plus the MySQL SQL flavor
(``INSERT IGNORE`` / ``NOW() - INTERVAL %s SECOND``) and register it.
"""

from __future__ import annotations

import logging
import typing as tp

from gcl_looper.watchdogs import exceptions as exc
from gcl_looper.watchdogs.locks import table as table_base

LOG = logging.getLogger(__name__)


def _import_psycopg() -> tp.Any:
    try:
        import psycopg
    except ImportError as e:  # pragma: no cover - depends on optional extra
        raise ImportError(
            "The PostgreSQL watchdog backend requires the 'psycopg' package. "
            "Install the optional dependency: pip install gcl_looper[pg]"
        ) from e
    return psycopg


class PostgresTableLockDriver(table_base.TableLockDriver):
    """Table-based distributed lock stored in PostgreSQL."""

    def __init__(
        self,
        connection_url: str,
        lock_key: str,
        lock_timeout: int = table_base.DEFAULT_LOCK_TIMEOUT,
        table_name: str = "gcl_looper_locks",
        create_table: bool = True,
        connect_timeout: int = 10,
    ) -> None:
        super().__init__(
            connection_url=connection_url,
            lock_key=lock_key,
            lock_timeout=lock_timeout,
            table_name=table_name,
            create_table=create_table,
        )
        self._connect_timeout = connect_timeout

    def _connect(self) -> tp.Any:
        psycopg = _import_psycopg()
        try:
            # autocommit: every lock statement commits on its own; the table
            # lock must not depend on an open transaction.
            return psycopg.connect(
                self._connection_url,
                autocommit=True,
                connect_timeout=self._connect_timeout,
            )
        except psycopg.Error as e:
            raise exc.LockConnectionError(reason=repr(e)) from e

    def _execute(self, sql: str, params: tp.Sequence[tp.Any]) -> int:
        psycopg = _import_psycopg()
        conn = self._ensure_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return cur.rowcount
        except psycopg.Error as e:
            # Any error invalidates the (possibly broken) connection.
            raise exc.LockConnectionError(reason=repr(e)) from e

    def _sql_create_table(self) -> str:
        return (
            "CREATE TABLE IF NOT EXISTS {table} ("
            "name text PRIMARY KEY, "
            "worker_id text, "
            "locked_at timestamptz, "
            "created timestamptz NOT NULL DEFAULT now(), "
            "updated timestamptz NOT NULL DEFAULT now()"
            ")"
        ).format(table=self.table_name)

    def _sql_ensure_row(self) -> str:
        return (
            "INSERT INTO {table} (name) VALUES (%s) ON CONFLICT (name) DO NOTHING"
        ).format(table=self.table_name)

    def _sql_acquire(self) -> str:
        # Take over when the row is free, when the current owner stopped
        # refreshing it (expired), or when we already own it (refresh).
        return (
            "UPDATE {table} "
            "SET worker_id = %s, locked_at = now(), updated = now() "
            "WHERE name = %s AND ("
            "worker_id IS NULL "
            "OR worker_id = %s "
            "OR locked_at IS NULL "
            "OR locked_at < now() - make_interval(secs => %s)"
            ")"
        ).format(table=self.table_name)

    def _sql_release(self) -> str:
        return (
            "UPDATE {table} "
            "SET worker_id = NULL, locked_at = NULL, updated = now() "
            "WHERE name = %s AND worker_id = %s"
        ).format(table=self.table_name)

    def _acquire_params(self) -> tp.Tuple[tp.Any, ...]:
        # Matches the placeholder order in `_sql_acquire`:
        # worker_id, name, worker_id (self-refresh), lock_timeout.
        return (
            self.worker_id,
            self.lock_key,
            self.worker_id,
            float(self._lock_timeout),
        )
