#    Copyright 2026 George Melikov <mail@gmelikov.ru>
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

from contextlib import ExitStack
import errno
import fcntl
import logging
import os
from pathlib import Path
import socket
import stat
import sys
import threading

LOG = logging.getLogger(__name__)
_SUCCESS = b"1"
_FAILURE = b"0"


def systemd_socket_path():
    """Locate the reload socket for a unit with a single RuntimeDirectory."""
    directory = os.environ.get("RUNTIME_DIRECTORY")
    return Path(directory) / "reload.sock" if directory else None


def request_reload(path, timeout=125):
    """Ask a local hub to replace its workers and wait for the result."""
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        client.connect(os.fspath(path))
        return client.recv(1) == _SUCCESS


class _Listener:
    def __init__(self, path):
        self.path = Path(path)
        self._resources = ExitStack()

    def open(self, reload):
        lock_path = self.path.with_name(self.path.name + ".lock")
        with ExitStack() as resources:
            fd = os.open(
                lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
            )
            resources.callback(os.close, fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(
                    errno.EINVAL, "Reload socket lock must be a regular file", lock_path
                )
            # Keep the lock file in place so simultaneous starts contend on one inode.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._remove_stale_socket()
            listener = resources.enter_context(socket.socket(socket.AF_UNIX))
            listener.bind(os.fspath(self.path))
            resources.callback(self._unlink_socket, self.path.lstat())
            self.path.chmod(0o600)
            listener.listen()
            thread = threading.Thread(
                target=self._serve, args=(listener, reload), daemon=True
            )
            thread.start()
            resources.callback(thread.join)
            resources.callback(listener.shutdown, socket.SHUT_RDWR)
            self._resources = resources.pop_all()

    def _remove_stale_socket(self):
        try:
            existing = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.geteuid():
            return
        with socket.socket(socket.AF_UNIX) as probe:
            probe.setblocking(False)
            if probe.connect_ex(os.fspath(self.path)) == errno.ECONNREFUSED:
                self._unlink_socket(existing)

    def _unlink_socket(self, existing):
        try:
            if os.path.samestat(existing, self.path.lstat()):
                self.path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _serve(listener, reload):
        while True:
            try:
                client, _ = listener.accept()
            except OSError as error:
                # Linux interrupts accept() with EINVAL when the listener shuts down.
                if error.errno != errno.EINVAL:
                    LOG.error("Could not accept a reload request: %s", error)
                return
            with client:
                success = reload().result()
                try:
                    client.sendall(_SUCCESS if success else _FAILURE)
                except OSError as error:
                    LOG.warning("Could not send reload result: %s", error)

    def close(self):
        self._resources.close()


def main():
    path = systemd_socket_path()
    if path is None:
        print("RUNTIME_DIRECTORY is required for reload", file=sys.stderr)
        return 1
    try:
        if request_reload(path):
            return 0
        print("Worker replacement failed", file=sys.stderr)
    except OSError as error:
        print(f"Reload failed: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
