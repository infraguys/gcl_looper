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

import bjoern

from gcl_looper.services import base

LOG = logging.getLogger(__name__)


class BjoernService(base.AbstractService):
    """Bjoern has it's own eventloop, so we don't need to loop explicitly"""

    def __init__(self, wsgi_app, host, port, bjoern_kwargs=None):
        super(BjoernService, self).__init__()
        self._wsgi_app = wsgi_app
        self._host = host
        self._port = port
        self._bjoern_kwargs = bjoern_kwargs or {}
        self._bjoern_kwargs.setdefault("reuse_port", False)
        self.should_subscribe_signals = True

    def _setup(self):
        bjoern.listen(
            wsgi_app=self._wsgi_app,
            host=self._host,
            port=self._port,
            **self._bjoern_kwargs
        )
        return super()._setup()

    def _exit_gracefully(self, signum, frame):
        # TODO(g.melikov): bjoern may have problems with exit on signals:
        #  - signals mangling with multiprocess
        #  - even if bjoern got our signal - it may not return before new
        #    client try to connect...
        #  - bjoern doesn't have graceful stop, beware!
        if bjoern._default_instance:
            sock, wsgi_app = bjoern._default_instance
            sock.close()
        os.kill(os.getpid(), signal.SIGINT)

    def _subscribe_signals(self, handlers):
        signal.signal(signal.SIGTERM, self._exit_gracefully)

    def _loop(self):
        LOG.info("Bjoern server: %s:%s", self._host, self._port)
        try:
            bjoern.run()
        except KeyboardInterrupt:
            # Just a clean stop on Ctrl+C...
            pass

    def stop(self):
        raise NotImplementedError()
