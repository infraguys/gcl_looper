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

import pytest

from gcl_looper.watchdogs import exceptions as exc
from gcl_looper.watchdogs.locks import pg_table


class FakeError(Exception):
    pass


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, sql, params=()):
        self._conn.statements.append((sql, params))
        script = self._conn.script.pop(0) if self._conn.script else 0
        if isinstance(script, Exception):
            raise script
        self._conn.last_rowcount = script
        return self

    @property
    def rowcount(self):
        return self._conn.last_rowcount


class FakeConnection:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.statements = []
        self.last_rowcount = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class FakePsycopgModule:
    Error = FakeError

    def __init__(self):
        self.connections = []
        self.connect_script = []

    def connect(self, conninfo, **kwargs):
        result = self.connect_script.pop(0) if self.connect_script else None
        if isinstance(result, Exception):
            raise result
        conn = result or FakeConnection()
        self.connections.append(conn)
        return conn


@pytest.fixture
def fake_pg(monkeypatch):
    module = FakePsycopgModule()
    monkeypatch.setattr(pg_table, "_import_psycopg", lambda: module)
    return module


def make_driver(**kwargs):
    kwargs.setdefault("connection_url", "postgresql://u:p@h:5432/d")
    kwargs.setdefault("lock_key", "master_key")
    kwargs.setdefault("lock_timeout", 30)
    return pg_table.PostgresTableLockDriver(**kwargs)


class TestPostgresTableLockDriver:
    def test_acquire_writes_the_row(self, fake_pg):
        conn = FakeConnection(script=[-1, 0, 1])
        fake_pg.connect_script.append(conn)

        driver = make_driver()
        assert driver.acquire() is True
        assert driver.is_held is True

        # create table, ensure row, acquire
        create_sql, _ = conn.statements[0]
        ensure_sql, ensure_params = conn.statements[1]
        acquire_sql, acquire_params = conn.statements[2]

        assert create_sql.startswith("CREATE TABLE IF NOT EXISTS")
        assert "ON CONFLICT (name) DO NOTHING" in ensure_sql
        assert ensure_params == ("master_key",)
        assert "worker_id = %s" in acquire_sql
        assert "make_interval" in acquire_sql
        assert acquire_params == (
            driver.worker_id,
            "master_key",
            driver.worker_id,
            30.0,
        )

    def test_acquire_denied_when_lock_timeout_not_passed(self, fake_pg):
        conn = FakeConnection(script=[-1, 0, 0])
        fake_pg.connect_script.append(conn)
        driver = make_driver()
        assert driver.acquire() is False
        assert driver.is_held is False

    def test_refresh_after_acquire_skips_create(self, fake_pg):
        conn = FakeConnection(script=[-1, 0, 1, 0, 1])
        fake_pg.connect_script.append(conn)
        driver = make_driver()
        assert driver.acquire() is True
        assert driver.acquire() is True
        assert len(conn.statements) == 5  # no second CREATE TABLE

    def test_connection_error_drops_connection(self, fake_pg):
        conn = FakeConnection(script=[-1, 0, FakeError("connection lost")])
        fake_pg.connect_script.append(conn)
        driver = make_driver()
        with pytest.raises(exc.LockConnectionError):
            driver.acquire()
        assert conn.closed is True
        assert driver.is_held is False

    def test_acquire_after_failure_reconnects(self, fake_pg):
        broken = FakeConnection(script=[-1, 0, FakeError("boom")])
        # The table already exists, so the fresh session only runs
        # ensure_row + acquire (no second CREATE TABLE).
        fresh = FakeConnection(script=[0, 1])
        fake_pg.connect_script.extend([broken, fresh])
        driver = make_driver()
        with pytest.raises(exc.LockConnectionError):
            driver.acquire()
        # The driver must create the fresh connection on the next call.
        assert driver.acquire() is True
        assert len(fresh.statements) == 2

    def test_release_clears_the_row(self, fake_pg):
        conn = FakeConnection(script=[-1, 0, 1, 1])
        fake_pg.connect_script.append(conn)
        driver = make_driver()
        assert driver.acquire() is True
        driver.release()
        release_sql, release_params = conn.statements[-1]
        assert "SET worker_id = NULL" in release_sql
        assert release_params == ("master_key", driver.worker_id)
        assert driver.is_held is False

    def test_release_without_connection_is_noop(self, fake_pg):
        driver = make_driver()
        driver.release()  # must not raise
        assert driver.is_held is False

    def test_release_swallows_connection_errors(self, fake_pg):
        conn = FakeConnection(script=[-1, 0, 1, FakeError("dead")])
        fake_pg.connect_script.append(conn)
        driver = make_driver()
        driver.acquire()
        driver.release()  # must not raise

    def test_connect_failure_raises_connection_error(self, fake_pg):
        fake_pg.connect_script.append(FakeError("no pg here"))
        driver = make_driver()
        with pytest.raises(exc.LockConnectionError):
            driver.acquire()

    def test_fork_forces_new_connection(self, fake_pg):
        first = FakeConnection(script=[-1, 0, 1])
        second = FakeConnection(script=[-1, 0, 1])
        fake_pg.connect_script.extend([first, second])
        driver = make_driver()
        assert driver.acquire() is True

        driver._conn_pid = "other_pid"
        assert driver.acquire() is True
        assert first is not second
        assert len(second.statements) == 3  # create + ensure + acquire

    def test_invalid_table_name_rejected(self, fake_pg):
        with pytest.raises(ValueError):
            make_driver(table_name="bad name; DROP TABLE x")

    def test_invalid_lock_timeout_rejected(self, fake_pg):
        with pytest.raises(ValueError):
            make_driver(lock_timeout=0)
