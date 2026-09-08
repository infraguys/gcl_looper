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
#
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""Master election functional tests against a real PostgreSQL server.

The tests are skipped unless ``GCL_LOOPER_TEST_PG_URL`` is set, e.g.::

    podman run -d --name gcl_looper_pg18 -e POSTGRES_PASSWORD=*** \
        -e POSTGRES_DB=gcltest -p 5433:5432 docker.io/library/postgres:18
    GCL_LOOPER_TEST_PG_URL=postgresql://postgres:test@127.0.0.1:5433/gcltest \
        pytest gcl_looper/tests/functional/watchdogs
"""

import multiprocessing
import os
import time
import uuid

import pytest

from gcl_looper.services import basic as basic_service
from gcl_looper.watchdogs import exceptions as wd_exc
from gcl_looper.watchdogs.database import DbWatchDog
from gcl_looper.watchdogs.locks.pg_advisory import PostgresAdvisoryLockDriver
from gcl_looper.watchdogs.locks.pg_table import PostgresTableLockDriver

pytestmark = pytest.mark.skipif(
    not os.environ.get("GCL_LOOPER_TEST_PG_URL"),
    reason="GCL_LOOPER_TEST_PG_URL is not set",
)


def pg_url():
    return os.environ["GCL_LOOPER_TEST_PG_URL"]


def unique_key(prefix="lock"):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:12])


def acquire_within(driver, timeout=2.0):
    """Retry driver.acquire() until it succeeds or timeout elapses.

    Returns the elapsed seconds, or ``None`` if it never succeeded.
    """
    start = time.time()
    while time.time() - start < timeout:
        if driver.acquire():
            return time.time() - start
        time.sleep(0.02)
    return None


def make_table_driver(lock_key, **kwargs):
    kwargs.setdefault("lock_timeout", 30)
    return PostgresTableLockDriver(connection_url=pg_url(), lock_key=lock_key, **kwargs)


def make_advisory_driver(lock_key, **kwargs):
    return PostgresAdvisoryLockDriver(
        connection_url=pg_url(), lock_key=lock_key, **kwargs
    )


class TestPostgresTableLock:
    def test_exclusive_master_with_takeover(self):
        key = unique_key("table_exclusive")
        a = make_table_driver(key)
        b = make_table_driver(key)
        try:
            assert a.acquire() is True
            assert b.acquire() is False
            # The master refreshes its lock without losing it.
            assert a.acquire() is True
            assert b.acquire() is False

            a.release()
            assert b.acquire() is True
            assert a.acquire() is False
        finally:
            a.close()
            b.close()

    def test_stale_master_takeover_after_timeout(self):
        key = unique_key("table_stale")
        a = make_table_driver(key, lock_timeout=2)
        b = make_table_driver(key, lock_timeout=2)
        try:
            assert a.acquire() is True
            # The master "dies" (stops refreshing).
            a.close()
            # A standby can only take the table lock after it expires.
            assert b.acquire() is False
            time.sleep(2.5)
            assert b.acquire() is True
        finally:
            a.close()
            b.close()

    def test_release_does_not_steal_foreign_lock(self):
        key = unique_key("table_steal")
        a = make_table_driver(key)
        b = make_table_driver(key)
        try:
            assert a.acquire() is True
            # b never acquired: releasing must not touch the row of a.
            b.release()
            assert a.acquire() is True
        finally:
            a.close()
            b.close()

    def test_missing_table_without_autocreate(self):
        key = unique_key("table_missing")
        missing_table = "gcl_looper_absent_%s" % uuid.uuid4().hex[:12]
        driver = make_table_driver(key, table_name=missing_table, create_table=False)
        try:
            with pytest.raises(wd_exc.LockConnectionError):
                driver.acquire()
        finally:
            driver.close()

    def test_concurrent_fork_processes_one_master(self):
        # Several "nodes" (forked processes) race for the lock; the number
        # of concurrent masters must never exceed one.
        key = unique_key("table_fork")
        concurrent = multiprocessing.Value("i", 0)
        violations = multiprocessing.Value("i", 0)

        def node():
            driver = make_table_driver(key, lock_timeout=5)
            deadline = time.time() + 3
            try:
                while time.time() < deadline:
                    if driver.acquire():
                        with concurrent.get_lock():
                            concurrent.value += 1
                            if concurrent.value > 1:
                                violations.value += 1
                        time.sleep(0.05)
                        with concurrent.get_lock():
                            concurrent.value -= 1
                        # Keep the lock for a while, then hand it over.
                        if os.getpid() % 3 == 0:
                            driver.release()
                    else:
                        time.sleep(0.02)
            finally:
                driver.release()
                driver.close()

        processes = [
            multiprocessing.get_context("fork").Process(target=node) for _ in range(4)
        ]
        for p in processes:
            p.start()
        for p in processes:
            p.join(30)
            assert p.exitcode == 0
        assert violations.value == 0


class TestPostgresAdvisoryLock:
    def test_exclusive_master_with_takeover(self):
        key = unique_key("adv_exclusive")
        a = make_advisory_driver(key)
        b = make_advisory_driver(key)
        try:
            assert a.acquire() is True
            assert b.acquire() is False
            # Repeated acquire does not stack the lock.
            assert a.acquire() is True

            a.release()
            assert b.acquire() is True
            assert a.acquire() is False
        finally:
            a.close()
            b.close()

    def test_instant_failover_when_session_dies(self):
        # The lock must be released the instant the holder's session dies,
        # without waiting for any timeout.
        key = unique_key("adv_failover")
        a = make_advisory_driver(key)
        b = make_advisory_driver(key)
        try:
            assert a.acquire() is True
            assert b.acquire() is False

            # Simulate a node crash: kill the backend session of `a`.
            row = a._query_one("SELECT pg_backend_pid()", ())
            b._query_one("SELECT pg_terminate_backend(%s)", (row[0],))
            # Takeover is near-instant once the session is gone (the standby
            # picks it up on its next poll), far below any lock timeout.
            elapsed = acquire_within(b, timeout=2.0)
            assert elapsed is not None
            assert elapsed < 1.0
            # The crashed master notices the loss on the next refresh and
            # does not reclaim the lock held by `b`.
            assert a.acquire() is False
            assert a.is_held is False
        finally:
            a.close()
            b.close()

    def test_holder_visible_in_pg_locks(self):
        key = unique_key("adv_visible")
        a = make_advisory_driver(key)
        try:
            assert a.acquire() is True
            row = a._query_one(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND pid = pg_backend_pid()",
                (),
            )
            assert row[0] >= 1
        finally:
            a.close()

    def test_different_keys_do_not_conflict(self):
        key_a = unique_key("adv_key_1")
        key_b = unique_key("adv_key_2")
        d1 = make_advisory_driver(key_a)
        d2 = make_advisory_driver(key_b)
        try:
            assert d1.acquire() is True
            assert d2.acquire() is True
        finally:
            d1.close()
            d2.close()

    def test_forked_child_uses_own_session(self):
        key = unique_key("adv_fork")
        parent_driver = make_advisory_driver(key)
        assert parent_driver.acquire() is True

        ctx = multiprocessing.get_context("fork")

        def child(result_pipe):
            # The child must NOT inherit the "held" state nor the session:
            # while the parent session is alive, the child can not take it.
            driver = PostgresAdvisoryLockDriver(connection_url=pg_url(), lock_key=key)
            try:
                result_pipe.send(driver.acquire())
            finally:
                driver.close()
                result_pipe.close()

        parent_conn, child_conn = ctx.Pipe()
        proc = ctx.Process(target=child, args=(child_conn,))
        proc.start()
        acquired_by_child = parent_conn.recv()
        proc.join(10)

        parent_driver.close()
        assert acquired_by_child is False

        # After the parent released the lock a new session can take it.
        driver2 = make_advisory_driver(key)
        try:
            assert driver2.acquire() is True
        finally:
            driver2.close()


class WatchdogService(basic_service.BasicService):
    __test__ = False

    def __init__(self, **kwargs):
        super(WatchdogService, self).__init__(iter_min_period=0, iter_pause=0, **kwargs)
        self.iterations = 0
        self.became = 0
        self.lost = 0

    def _iteration(self):
        self.iterations += 1

    def _on_become_master(self):
        self.became += 1

    def _on_lose_master(self):
        self.lost += 1


class TestServiceMasterElection:
    def test_service_skips_iterations_until_it_becomes_master(self):
        # All nodes share one lock_timeout (a single cluster-wide setting).
        key = unique_key("svc_election")
        current_master = make_table_driver(key, lock_timeout=2)
        assert current_master.acquire() is True

        service_wd = DbWatchDog(make_table_driver(key, lock_timeout=2))
        service = WatchdogService(watchdog=service_wd)
        try:
            for _ in range(3):
                service._loop_iteration()
            assert service.iterations == 0
            assert service.is_master is False
            assert service.became == 0

            # The current master goes down (abruptly, no graceful release):
            # the standby can only take over once the lock expires.
            current_master.close()
            time.sleep(2.5)

            service._loop_iteration()
            assert service.is_master is True
            assert service.became == 1
            assert service.iterations == 1

            service._loop_iteration()
            assert service.became == 1  # no repeated hook
            assert service.iterations == 2
        finally:
            service._finish()
            current_master.close()

    def test_service_demoted_when_master_lock_taken_over(self):
        key = unique_key("svc_demote")
        service_wd = DbWatchDog(make_table_driver(key, lock_timeout=2))
        service = WatchdogService(watchdog=service_wd)
        try:
            service._loop_iteration()
            assert service.is_master is True
            assert service.became == 1

            # Simulate a takeover: let the service lock expire (the service
            # stops refreshing it between iterations) and steal it.
            thief = make_table_driver(key, lock_timeout=1)
            time.sleep(2.5)
            assert thief.acquire() is True

            service._loop_iteration()  # notices the loss, skips the work
            assert service.is_master is False
            assert service.lost == 1
            assert service.iterations == 1

            # Once the thief's lock expires (thief stops refreshing), the
            # service reclaims leadership.
            thief.close()
            time.sleep(2.5)
            service._loop_iteration()
            assert service.is_master is True
            assert service.became == 2
            assert service.iterations == 2
        finally:
            service._finish()
