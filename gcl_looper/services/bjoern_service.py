#    Copyright 2025 George Melikov <mail@gmelikov.ru>
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

import logging
import os
import signal
import socket
import stat
import tempfile

import bjoern

from gcl_looper.services import base

LOG = logging.getLogger(__name__)


class BjoernService(base.AbstractService):
    """Bjoern has it's own eventloop, so we don't need to loop explicitly"""

    __mp_downgrade_user__ = "nobody"

    def __init__(self, wsgi_app, host, port, bjoern_kwargs=None):
        super(BjoernService, self).__init__()
        self._wsgi_app = wsgi_app
        self._host = host
        self._port = port
        self._bjoern_kwargs = bjoern_kwargs or {}
        self._bjoern_kwargs.setdefault("reuse_port", False)
        self._socket = None
        self._unix_path = None
        self._unix_dir_fd = None
        self._unix_identity = None
        self.should_subscribe_signals = True

    def _bind(self, reuse_port=False, listen_backlog=bjoern.DEFAULT_LISTEN_BACKLOG):
        if self._host.startswith("unix:"):
            sock = socket.socket(socket.AF_UNIX)
            self._socket = sock
            address = self._host[5:]
            if address.startswith("@"):
                address = "\0" + address[1:]
            else:
                self._bind_unix_socket(address)
                sock.setblocking(False)
                return listen_backlog
        else:
            sock = socket.socket(socket.AF_INET)
            self._socket = sock
            address = (self._host, self._port)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if reuse_port:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self._socket = sock
        sock.bind(address)
        sock.setblocking(False)
        return listen_backlog

    def _bind_unix_socket(self, address):
        directory, name = os.path.split(address)
        self._unix_dir_fd = os.open(
            directory or ".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        )
        self._unix_path = name
        temporary = tempfile.mkdtemp(dir=f"/proc/self/fd/{self._unix_dir_fd}")
        temporary_fd = None
        bound = False
        try:
            temporary_fd = os.open(
                temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
            directory_stat = os.fstat(temporary_fd)
            if directory_stat.st_uid != os.geteuid() or directory_stat.st_mode & 0o077:
                raise PermissionError("Unix socket staging directory is not private")
            self._socket.bind(f"/proc/self/fd/{temporary_fd}/socket")
            bound = True
            entry = os.stat("socket", dir_fd=temporary_fd, follow_symlinks=False)
            os.link(
                "socket",
                name,
                src_dir_fd=temporary_fd,
                dst_dir_fd=self._unix_dir_fd,
                follow_symlinks=False,
            )
            self._unix_identity = (entry.st_dev, entry.st_ino)
        finally:
            if temporary_fd is not None:
                try:
                    if bound:
                        os.unlink("socket", dir_fd=temporary_fd)
                finally:
                    os.close(temporary_fd)
            os.rmdir(temporary)

    def _setup(self):
        backlog = self._bind(**self._bjoern_kwargs)
        super()._setup()
        self._socket.listen(backlog)

    def _exit_gracefully(self, signum, frame):
        # TODO(g.melikov): bjoern may have problems with exit on signals:
        #  - signals mangling with multiprocess
        #  - even if bjoern got our signal - it may not return before new
        #    client try to connect...
        #  - bjoern doesn't have graceful stop, beware!
        if self._socket is not None:
            self._socket.close()
        os.kill(os.getpid(), signal.SIGINT)

    def _subscribe_signals(self, handlers):
        signal.signal(signal.SIGTERM, self._exit_gracefully)

    def _loop(self):
        LOG.info("Bjoern server: %s:%s", self._host, self._port)
        try:
            bjoern.server_run(self._socket, self._wsgi_app)
        except KeyboardInterrupt:
            # Just a clean stop on Ctrl+C...
            pass

    def _finish(self):
        try:
            if self._unix_identity is not None:
                try:
                    entry = os.stat(
                        self._unix_path, dir_fd=self._unix_dir_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    pass
                else:
                    if (
                        stat.S_ISSOCK(entry.st_mode)
                        and (entry.st_dev, entry.st_ino) == self._unix_identity
                    ):
                        os.unlink(self._unix_path, dir_fd=self._unix_dir_fd)
        finally:
            self._unix_identity = None
            if self._unix_dir_fd is not None:
                os.close(self._unix_dir_fd)
                self._unix_dir_fd = None
            if self._socket is not None:
                self._socket.close()
        super()._finish()

    def stop(self):
        raise NotImplementedError()
