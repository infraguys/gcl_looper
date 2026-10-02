#    Copyright 2026 George Melikov <mail@gmelikov.ru>
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

import functools
import os
import signal
import threading
import time

import pytest
import requests

from gcl_looper.services import bjoern_service
from gcl_looper.services import hub

HOST = "127.0.0.1"


def _build_worker(version_file, port):
    # Read at build time: stands for code loaded when the worker starts.
    with open(version_file) as f:
        version = f.read()
    if version == "broken":
        raise RuntimeError("broken release")

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [f"{version} {os.getpid()}".encode()]

    service = bjoern_service.BjoernService(
        wsgi_app=app,
        host=HOST,
        port=port,
        bjoern_kwargs={"reuse_port": True},
    )
    service.__mp_downgrade_user__ = None
    if version == "stalled":

        def stall():
            with open(str(version_file) + ".setup", "w") as marker:
                marker.write(str(os.getpid()))
            time.sleep(60)

        service.add_setup(stall)
    return service


def _get(port):
    # A fresh connection per request: keep-alive would pin it to one worker.
    version, pid = requests.get(
        f"http://{HOST}:{port}/", headers={"Connection": "close"}, timeout=5
    ).text.split()
    return version, int(pid)


def _wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except requests.ConnectionError:
            pass
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.fixture
def running_hub(tmp_path):
    started = []

    def start(port, workers=2, iter_min_period=0.1):
        version_file = tmp_path / "version"
        version_file.write_text("v1")
        h = hub.ReloadableProcessHubService(
            iter_min_period=iter_min_period, ready_timeout=15, drain_timeout=5
        )
        h.should_subscribe_signals = False
        for _ in range(workers):
            h.add_service_factory(functools.partial(_build_worker, version_file, port))
        thread = threading.Thread(target=h.start)
        thread.start()
        started.append((h, thread))
        _wait_for(
            lambda: (
                len(h._instances) == workers
                and all(i.ready.is_set() for i in h._instances.values())
            )
        )
        return h, version_file

    yield start

    for h, thread in started:
        h.stop()
        thread.join(timeout=10)


def test_reload_replaces_workers_without_failed_requests(running_hub):
    port = 8093
    h, version_file = running_hub(port)
    old = list(h._instances.values())
    old_pids = {i.pid for i in old}

    failures = []
    served = []
    done = threading.Event()

    def load():
        while not done.is_set():
            try:
                served.append(_get(port))
            except requests.RequestException as e:
                failures.append(e)

    loader = threading.Thread(target=load)
    loader.start()
    try:
        version_file.write_text("v2")
        h.reload()
        _wait_for(lambda: _get(port)[0] == "v2")
        # Keep the load on until the old generation is swapped out and gone.
        _wait_for(
            lambda: (
                old_pids.isdisjoint(i.pid for i in h._instances.values())
                and not any(i.is_alive() for i in old)
            )
        )
    finally:
        done.set()
        loader.join()

    assert not failures
    new_pids = {i.pid for i in h._instances.values()}
    assert len(new_pids) == 2
    assert not new_pids & old_pids
    assert {pid for v, pid in served if v == "v2"} <= new_pids
    # Only the new generation serves after the reload.
    assert all(_get(port)[0] == "v2" for _ in range(20))


def test_failed_reload_keeps_old_workers(running_hub):
    port = 8094
    h, version_file = running_hub(port)
    old_pids = {i.pid for i in h._instances.values()}

    version_file.write_text("broken")
    done = h._iteration_number
    h.reload()
    # The iteration running now may have missed the request; the next one
    # handles it, and the reload is over once that one finishes.
    _wait_for(lambda: h._iteration_number > done + 1)

    assert {i.pid for i in h._instances.values()} == old_pids
    assert all(i.is_alive() for i in h._instances.values())
    assert _get(port)[0] == "v1"

    # A fixed release goes through afterwards.
    version_file.write_text("v3")
    h.reload()
    _wait_for(lambda: _get(port)[0] == "v3")


def test_reload_wakes_the_hub_up(running_hub):
    port = 8096
    h, version_file = running_hub(port, iter_min_period=60)

    version_file.write_text("v2")
    h.reload()

    # Far sooner than the next scheduled iteration.
    _wait_for(lambda: _get(port)[0] == "v2", timeout=10)


def test_add_service_is_rejected():
    h = hub.ReloadableProcessHubService()

    with pytest.raises(TypeError):
        h.add_service(bjoern_service.BjoernService(None, HOST, 0))


def test_sighup_reloads_while_workers_spawn():
    h = hub.ReloadableProcessHubService()
    original = signal.getsignal(signal.SIGHUP)
    try:
        h._setup()

        signal.raise_signal(signal.SIGHUP)

        assert h._reload_requested
    finally:
        signal.signal(signal.SIGHUP, original)


def test_stop_racing_a_reload_leaves_no_workers(running_hub):
    port = 8097
    h, version_file = running_hub(port)
    spawned = list(h._instances.values())
    wait_ready = h._wait_ready

    def ready_then_stopped(generation):
        spawned.extend(generation.values())
        ok = wait_ready(generation)
        # stop() lands right before the hub swaps the generations.
        h.stop()
        return ok

    h._wait_ready = ready_then_stopped
    version_file.write_text("v2")
    h.reload()

    _wait_for(lambda: len(spawned) == 4 and not any(i.is_alive() for i in spawned))


def test_spawn_failing_midway_leaves_no_workers(running_hub):
    port = 8098
    h, version_file = running_hub(port)
    old_pids = {i.pid for i in h._instances.values()}
    spawn = h._spawn
    spawned = []

    def spawn_once(factory):
        if spawned:
            raise OSError("fork failed")
        spawned.append(spawn(factory))
        return spawned[-1]

    h._spawn = spawn_once
    version_file.write_text("v2")
    done = h._iteration_number
    h.reload()
    _wait_for(lambda: h._iteration_number > done + 1)

    assert not spawned[0].is_alive()
    assert {i.pid for i in h._instances.values()} == old_pids
    assert _get(port)[0] == "v1"


def test_reload_rejects_worker_that_exited_after_ready(running_hub):
    port = 8099
    h, version_file = running_hub(port)
    old = dict(h._instances)
    spawn_generation = h._spawn_generation
    replacements = []

    def spawn_with_dead_worker():
        generation = spawn_generation()
        replacements.extend(generation.values())
        assert all(i.ready.wait(timeout=15) for i in replacements)
        replacements[0].kill()
        replacements[0].join(timeout=5)
        assert not replacements[0].is_alive()
        return generation

    h._spawn_generation = spawn_with_dead_worker
    version_file.write_text("v2")
    done = h._iteration_number
    h.reload()
    _wait_for(lambda: h._iteration_number > done + 1)

    assert h._enabled
    assert h._instances == old
    assert all(i.is_alive() for i in old.values())
    assert not any(i.is_alive() for i in replacements)
    assert _get(port)[0] == "v1"


def _stubborn_worker(started, terminated):
    signal.signal(signal.SIGTERM, lambda s, frame: terminated.set())
    started.set()
    while True:
        signal.pause()


def test_stop_during_drain_bounds_both_generations():
    h = hub.ReloadableProcessHubService(drain_timeout=2)
    processes = []
    thread = None
    try:
        for _ in range(2):
            started = h._mp_context.Event()
            terminated = h._mp_context.Event()
            process = h._mp_context.Process(
                target=_stubborn_worker, args=(started, terminated)
            )
            process.start()
            processes.append((process, terminated))
            assert started.wait(timeout=15)
        h._instances = {0: processes[1][0]}

        def drain_then_finish():
            h._drain([processes[0][0]])
            h._finish()

        thread = threading.Thread(target=drain_then_finish)
        thread.start()
        assert processes[0][1].wait(timeout=5)
        started_stop = time.monotonic()
        h.stop()
        thread.join(timeout=3)

        assert not thread.is_alive()
        assert time.monotonic() - started_stop < 3
        assert not any(p.is_alive() for p, _ in processes)
    finally:
        for process, _ in processes:
            if process.is_alive():
                process.kill()
            process.join(timeout=5)
        if thread is not None:
            thread.join(timeout=5)


def test_stalled_setup_does_not_take_traffic(running_hub):
    port = 8100
    h, version_file = running_hub(port, workers=1)
    old = dict(h._instances)
    h._ready_timeout = 3
    version_file.write_text("stalled")
    iteration = h._iteration_number
    h.reload()
    marker = version_file.with_name("version.setup")
    _wait_for(marker.exists)
    for _ in range(40):
        response = requests.get(f"http://{HOST}:{port}/", timeout=0.5)
        assert response.text.split()[0] == "v1"
    _wait_for(lambda: h._iteration_number > iteration + 1)
    assert h._instances == old


class _TrackerProbe(bjoern_service.base.AbstractService):
    __mp_downgrade_user__ = "nobody"

    def __init__(self, connection, early_drop=False, automatic_drop=True):
        super().__init__()
        self.connection = connection
        if not automatic_drop:
            self.__mp_downgrade_user__ = None
        if early_drop:
            from gcl_looper import utils

            self.add_setup(utils.downgrade_user_group_privileges)

    def _loop(self):
        from multiprocessing import resource_tracker, shared_memory

        tracker = resource_tracker._resource_tracker
        detached = tracker._fd is None
        assert detached, "worker retains the privileged tracker channel"
        memory = shared_memory.SharedMemory(create=True, size=1)
        memory.close()
        memory.unlink()
        tracker_uid = os.stat(f"/proc/{tracker._pid}").st_uid
        self.connection.send((os.getuid(), detached, tracker_uid))
        self.connection.close()

    def stop(self):
        pass


@pytest.mark.skipif(os.getuid() != 0, reason="requires root to drop worker privileges")
@pytest.mark.parametrize(
    "early_drop, automatic_drop", [(False, True), (True, True), (True, False)]
)
def test_privilege_drop_detaches_tracker_and_preserves_readiness(
    early_drop, automatic_drop
):
    import pwd

    h = hub.ReloadableProcessHubService()
    parent, child = h._mp_context.Pipe(duplex=False)
    worker = h._spawn(
        functools.partial(_TrackerProbe, child, early_drop, automatic_drop)
    )
    child.close()
    try:
        assert worker.ready.wait(timeout=15)
        assert parent.poll(15)
        uid, detached, tracker_uid = parent.recv()
        assert uid == pwd.getpwnam("nobody").pw_uid
        assert detached
        assert tracker_uid == uid
        worker.join(timeout=5)
        assert worker.exitcode == 0
    finally:
        if worker.is_alive():
            worker.kill()
        worker.join(timeout=5)
        parent.close()
