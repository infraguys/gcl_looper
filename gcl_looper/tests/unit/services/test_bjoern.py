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

import os
import socket
from unittest import mock

import pytest

from gcl_looper.services import bjoern_service


def test_setup_runs_with_bound_socket_before_listening():
    service = bjoern_service.BjoernService(None, "127.0.0.1", 0)
    observed = []

    def setup():
        observed.append(service._socket.getsockname()[1])
        assert not service._socket.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)

    service.add_setup(setup)
    try:
        service._setup()
        assert observed[0] != 0
        assert service._socket.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
    finally:
        service._finish()


def test_failed_setup_closes_and_removes_unix_socket(tmp_path):
    path = tmp_path / "worker.sock"
    service = bjoern_service.BjoernService(None, f"unix:{path}", None)

    def fail():
        raise RuntimeError("setup failed")

    service.add_setup(fail)
    with pytest.raises(RuntimeError, match="setup failed"):
        service.start()
    assert service._socket.fileno() == -1
    assert not path.exists()


def test_closed_unix_listener_is_removed_on_finish(tmp_path):
    path = tmp_path / "worker.sock"
    service = bjoern_service.BjoernService(None, f"unix:{path}", None)
    service._setup()
    service._socket.close()
    service._finish()
    assert not path.exists()


def test_failed_setup_cleanup_survives_directory_symlink_swap(tmp_path):
    directory = tmp_path / "run"
    directory.mkdir()
    protected = tmp_path / "admin"
    protected.mkdir()
    victim = protected / "api.sock"
    victim.write_text("must survive")
    moved = tmp_path / "old-run"
    service = bjoern_service.BjoernService(None, f"unix:{directory}/api.sock", None)

    def replace_directory():
        directory.rename(moved)
        directory.symlink_to(protected, target_is_directory=True)
        raise RuntimeError("setup failed")

    service.add_setup(replace_directory)
    with pytest.raises(RuntimeError, match="setup failed"):
        service.start()
    assert victim.read_text() == "must survive"
    assert not (moved / "api.sock").exists()
    assert service._unix_dir_fd is None
    assert service._socket.fileno() == -1


@pytest.mark.parametrize("replacement", ["file", "socket", "symlink", "missing"])
def test_cleanup_preserves_replaced_socket_entry(tmp_path, replacement):
    path = tmp_path / "api.sock"
    moved = tmp_path / "original.sock"
    victim = tmp_path / "protected"
    victim.write_text("must survive")
    service = bjoern_service.BjoernService(None, f"unix:{path}", None)
    replacement_socket = None
    try:
        service._setup()
        path.rename(moved)
        if replacement == "file":
            path.write_text("replacement")
        elif replacement == "socket":
            replacement_socket = socket.socket(socket.AF_UNIX)
            replacement_socket.bind(str(path))
        elif replacement == "symlink":
            path.symlink_to(victim)
        service._finish()
        if replacement == "missing":
            assert not path.exists()
        else:
            assert path.lstat()
        assert moved.exists()
        assert victim.read_text() == "must survive"
        assert service._unix_dir_fd is None
    finally:
        service._finish()
        if replacement_socket is not None:
            replacement_socket.close()


def test_failed_bind_preserves_existing_entry_and_closes_directory_fd(tmp_path):
    path = tmp_path / "api.sock"
    path.write_text("existing")
    service = bjoern_service.BjoernService(None, f"unix:{path}", None)
    with pytest.raises(OSError):
        service.start()
    assert path.read_text() == "existing"
    assert service._unix_dir_fd is None
    assert service._socket.fileno() == -1


def test_unix_socket_accepts_connections_at_configured_path(tmp_path):
    path = tmp_path / "api.sock"
    service = bjoern_service.BjoernService(None, f"unix:{path}", None)
    try:
        service._setup()
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path))
            connection, _ = service._socket.accept()
            connection.close()
        directory_fd = service._unix_dir_fd
    finally:
        service._finish()
    with pytest.raises(OSError):
        os.fstat(directory_fd)


def test_long_relative_unix_socket_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    name = "s" * 100
    service = bjoern_service.BjoernService(None, f"unix:{name}", None)
    try:
        service._setup()
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(name)
            connection, _ = service._socket.accept()
            connection.close()
    finally:
        service._finish()
    assert not (tmp_path / name).exists()
    assert not list(tmp_path.iterdir())


def test_unix_bind_survives_ancestor_swap_after_directory_open(tmp_path):
    directory = tmp_path / "run"
    directory.mkdir()
    moved = tmp_path / "old-run"
    protected = tmp_path / "admin"
    protected.mkdir()
    victim = protected / "api.sock"
    victim.write_text("must survive")
    service = bjoern_service.BjoernService(None, f"unix:{directory}/api.sock", None)
    original_open = os.open

    def open_then_swap(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if path == str(directory):
            directory.rename(moved)
            directory.symlink_to(protected, target_is_directory=True)
        return fd

    try:
        with mock.patch.object(os, "open", side_effect=open_then_swap):
            service._setup()
        assert (moved / "api.sock").is_socket()
        assert victim.read_text() == "must survive"
    finally:
        service._finish()
    assert victim.read_text() == "must survive"
    assert not list(moved.iterdir())
