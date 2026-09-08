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
from gcl_looper.watchdogs.locks import pg_advisory


def advisory_number(key):
    return pg_advisory.advisory_lock_number(key)


class TestAdvisoryLockNumber:
    def test_stable_and_signed64(self):
        first = advisory_number("my_service_lock")
        second = advisory_number("my_service_lock")
        assert first == second
        assert -(2**63) <= first < 2**63

    def test_different_keys_differ(self):
        assert advisory_number("lock_a") != advisory_number("lock_b")


class FakeError(Exception):
    pass


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._last_row = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, sql, params=()):
        self._conn.statements.append((sql, params))
        script = self._conn.script.pop(0) if self._conn.script else None
        if isinstance(script, Exception):
            raise script
        self._last_row = script
        return self

    def fetchone(self):
        return self._last_row


class FakeConnection:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.statements = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class FakePsycopgModule:
    Error = FakeError

    def __init__(self, connect_script=None):
        self.connections = []
        self.connect_script = list(connect_script or [])

    def connect(self, conninfo, **kwargs):
        result = self.connect_script.pop(0) if self.connect_script else None
        if isinstance(result, Exception):
            raise result
        conn = result or FakeConnection()
        self.connections.append(conn)
        return conn


@pytest.fixture
def driver_cls_patched(monkeypatch):
    fake_pg = FakePsycopgModule()
    monkeypatch.setattr(
        pg_advisory.PostgresAdvisoryLockDriver,
        "_psycopg",
        staticmethod(lambda: fake_pg),
    )
    return fake_pg


def make_driver(**kwargs):
    kwargs.setdefault("connection_url", "postgresql://u:p@h:5432/d")
    kwargs.setdefault("lock_key", "master_key")
    return pg_advisory.PostgresAdvisoryLockDriver(**kwargs)


class TestPostgresAdvisoryLockDriver:
    def test_acquire_grants_lock(self, driver_cls_patched):
        driver_cls_patched.connect_script = []
        conn = FakeConnection(script=[(True,)])
        driver_cls_patched.connect_script.append(conn)

        driver = make_driver()
        assert driver.acquire() is True
        assert driver.is_held is True

        sql, params = conn.statements[0]
        assert "pg_try_advisory_lock" in sql
        assert params == (advisory_number("master_key"),)

    def test_acquire_denied(self, driver_cls_patched):
        driver_cls_patched.connect_script.append(FakeConnection(script=[(False,)]))
        driver = make_driver()
        assert driver.acquire() is False
        assert driver.is_held is False

    def test_refresh_does_not_stack_lock(self, driver_cls_patched):
        conn = FakeConnection(script=[(True,), (1,)])
        driver_cls_patched.connect_script.append(conn)

        driver = make_driver()
        assert driver.acquire() is True
        assert driver.acquire() is True

        lock_statements = [s for s, _ in conn.statements if "pg_try_advisory_lock" in s]
        assert len(lock_statements) == 1
        assert conn.statements[-1][0] == "SELECT 1"

    def test_connection_loss_reacquires_on_new_session(self, driver_cls_patched):
        first = FakeConnection(script=[(True,), FakeError("connection lost")])
        second = FakeConnection(script=[(True,)])
        driver_cls_patched.connect_script.extend([first, second])

        driver = make_driver()
        assert driver.acquire() is True

        # The ping fails (session died) -> reconnect and acquire again.
        assert driver.acquire() is True
        assert first.closed is True
        assert second.statements[0][0].startswith("SELECT pg_try_advisory_lock")

    def test_connect_failure_raises_connection_error(self, driver_cls_patched):
        driver_cls_patched.connect_script.append(FakeError("nope"))
        driver = make_driver()
        with pytest.raises(exc.LockConnectionError):
            driver.acquire()
        assert driver.is_held is False

    def test_query_failure_resets_state(self, driver_cls_patched):
        conn = FakeConnection(script=[FakeError("server gone")])
        driver_cls_patched.connect_script.append(conn)
        driver = make_driver()
        with pytest.raises(exc.LockConnectionError):
            driver.acquire()
        assert driver.is_held is False

    def test_release_unlocks(self, driver_cls_patched):
        conn = FakeConnection(script=[(True,), (True,)])
        driver_cls_patched.connect_script.append(conn)
        driver = make_driver()
        assert driver.acquire() is True
        driver.release()
        assert driver.is_held is False
        assert "pg_advisory_unlock" in conn.statements[-1][0]

    def test_release_without_connection_is_noop(self, driver_cls_patched):
        driver = make_driver()
        driver.release()  # must not raise

    def test_close_releases_session(self, driver_cls_patched):
        conn = FakeConnection(script=[(True,)])
        driver_cls_patched.connect_script.append(conn)
        driver = make_driver()
        driver.acquire()
        driver.close()
        assert conn.closed is True

    def test_fork_forces_new_connection(self, driver_cls_patched):
        first = FakeConnection(script=[(True,)])
        second = FakeConnection(script=[(True,)])
        driver_cls_patched.connect_script.extend([first, second])

        driver = make_driver()
        assert driver.acquire() is True

        # Simulate a fork: the child has another pid and must never reuse
        # the parent's connection nor its "held" state.
        driver._conn_pid = "some_other_pid"
        assert driver.acquire() is True
        assert first is not second
        assert second.statements[0][0].startswith("SELECT pg_try_advisory_lock")
