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
"""PostgreSQL advisory-lock master election (session level).

A direct port of the original ``WatchDogMysql`` (which used MySQL
``GET_LOCK``). PostgreSQL session-level advisory locks provide the exact same
mechanics:

* ``pg_try_advisory_lock(key)`` is a non-blocking acquire that returns
  ``true``/``false`` (``GET_LOCK(key, 0)`` in MySQL);
* the lock is owned by the *connection* and is released automatically the
  moment the session ends (crash, network drop), giving *instant* failover
  without any timeout.

Because an advisory lock is bound to a session, this driver keeps **one
dedicated connection** for its whole life (it never shares a pool). This is a
single extra connection per watchdog and is completely independent of any
application connection pool (restalchemy/psycopg_pool): those keep serving
business queries as usual.

The logical ``lock_key`` string is mapped to the advisory ``bigint`` key with
a stable BLAKE2b digest.
"""

from __future__ import annotations

import hashlib
import logging
import os
import typing as tp

from gcl_looper.watchdogs import exceptions as exc
from gcl_looper.watchdogs.locks import base as locks_base

LOG = logging.getLogger(__name__)

_INT64_MODULUS = 1 << 64
_INT64_SIGN_BIT = 1 << 63


def advisory_lock_number(lock_key: str) -> int:
    """Map an arbitrary ``lock_key`` string to a stable signed ``bigint``.

    PostgreSQL advisory locks are keyed by a 64-bit integer. A 64-bit
    BLAKE2b digest of the name is reinterpreted as a *signed* integer to fit
    the ``bigint`` range. Collisions are cryptographically negligible.
    """
    digest = hashlib.blake2b(lock_key.encode("utf-8"), digest_size=8).digest()
    number = int.from_bytes(digest, "big")
    if number >= _INT64_SIGN_BIT:
        number -= _INT64_MODULUS
    return number


class PostgresAdvisoryLockDriver(locks_base.BaseLockDriver):
    """Session-level advisory-lock based distributed lock.

    Args:
        connection_url: DSN/connection URL of the database.
        lock_key: Logical lock name (hashed to a ``bigint`` advisory key).
        application_name: Optional ``application_name`` connection parameter
            so the lock holder is easy to spot in ``pg_stat_activity``.
    """

    def __init__(
        self,
        connection_url: str,
        lock_key: str,
        application_name: tp.Optional[str] = None,
        connect_timeout: int = 10,
    ) -> None:
        super(PostgresAdvisoryLockDriver, self).__init__(
            connection_url=connection_url, lock_key=lock_key
        )
        self._lock_number = advisory_lock_number(lock_key)
        self._application_name = application_name or "gcl_looper_watchdog"
        self._connect_timeout = connect_timeout

        self._conn: tp.Any = None
        self._conn_pid: tp.Optional[int] = None

    @property
    def lock_number(self) -> int:
        """The advisory ``bigint`` key derived from the lock name."""
        return self._lock_number

    # --- public driver interface ------------------------------------------

    def acquire(self) -> bool:
        """Try to acquire (or keep) the session-level advisory lock."""
        self._ensure_connection()

        # The advisory lock dies with the session. If the connection broke we
        # must reconnect and try again: another node may have taken over.
        if self._held and not self._ping():
            LOG.warning(
                "Lost advisory lock %r (connection dropped), reconnecting",
                self._lock_key,
            )
            self._reset_connection()
            # Open a fresh session right away so the (re)acquisition below
            # has a connection to work with.
            self._ensure_connection()

        if not self._held:
            try:
                row = self._query_one(
                    "SELECT pg_try_advisory_lock(%s)", (self._lock_number,)
                )
            except exc.LockConnectionError:
                self._reset_connection()
                raise
            self._held = bool(row[0])
            if self._held:
                LOG.info(
                    "Acquired advisory lock %r (key=%d)",
                    self._lock_key,
                    self._lock_number,
                )
        return self._held

    def release(self) -> None:
        """Explicitly unlock (best effort). Closing also releases it."""
        if self._conn is None:
            self._held = False
            return
        if self._held:
            try:
                self._query_one("SELECT pg_advisory_unlock(%s)", (self._lock_number,))
                LOG.info("Released advisory lock %r", self._lock_key)
            except exc.LockConnectionError as e:
                LOG.warning(
                    "Failed to explicitly release advisory lock %r: %r",
                    self._lock_key,
                    e,
                )
        self._held = False

    def close(self) -> None:
        # Closing the session releases any session-level advisory lock held
        # by it automatically.
        self._reset_connection()

    # --- connection handling (single dedicated connection, fork safe) ------

    def _ensure_connection(self) -> None:
        current_pid = os.getpid()
        if self._conn is not None and self._conn_pid == current_pid:
            return

        if self._conn is not None and self._conn_pid != current_pid:
            # Inherited after a fork: drop the reference quietly, the child
            # must open its own connection (and re-acquire the lock).
            self._conn = None

        psycopg = self._psycopg()
        try:
            self._conn = psycopg.connect(
                self._connection_url,
                autocommit=True,
                connect_timeout=self._connect_timeout,
                application_name=self._application_name,
            )
        except psycopg.Error as e:
            self._held = False
            raise exc.LockConnectionError(reason=repr(e)) from e
        self._conn_pid = current_pid
        # A brand new session never holds the advisory lock yet.
        self._held = False

    def _reset_connection(self) -> None:
        conn, self._conn = self._conn, None
        self._conn_pid = None
        self._held = False
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover - best effort
                pass

    def _ping(self) -> bool:
        if self._conn is None:
            return False
        psycopg = self._psycopg()
        try:
            self._query_one("SELECT 1", ())
            return True
        except exc.LockConnectionError:
            return False
        except psycopg.Error:  # pragma: no cover - defensive
            return False

    # --- low level helpers -------------------------------------------------

    @staticmethod
    def _psycopg() -> tp.Any:
        try:
            import psycopg
        except ImportError as e:  # pragma: no cover - optional extra
            raise ImportError(
                "The PostgreSQL watchdog backend requires the 'psycopg' "
                "package. Install the optional dependency: "
                "pip install gcl_looper[pg]"
            ) from e
        return psycopg

    def _query_one(self, sql: str, params: tp.Sequence[tp.Any]) -> tp.Any:
        psycopg = self._psycopg()
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return cur.fetchone()
        except psycopg.Error as e:
            raise exc.LockConnectionError(reason=repr(e)) from e
