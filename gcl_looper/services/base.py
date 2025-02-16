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

import abc
import logging
import signal


LOG = logging.getLogger(__name__)


class AbstractService(abc.ABC):

    def __init__(self):
        super(AbstractService, self).__init__()
        self._should_subscribe_signals = True

    @property
    def should_subscribe_signals(self):
        return self._should_subscribe_signals

    @should_subscribe_signals.setter
    def should_subscribe_signals(self, value):
        LOG.info("Set should_subscribe_signals=%r", value)
        self._should_subscribe_signals = value

    def start(self):
        """Infinite loop itself"""
        try:
            self._setup()
            if self.should_subscribe_signals:
                self._subscribe_signals(self._get_sig_handlers())
            LOG.info("Start loop")
            self._loop()
            LOG.info("Loop finished.")
        finally:
            self._finish()

    @abc.abstractmethod
    def _loop(self):
        """Implement your loop logic here"""
        raise NotImplementedError()

    @abc.abstractmethod
    def stop(self):
        """Implement stop logic here"""
        raise NotImplementedError()

    def _setup(self):
        LOG.info("Setup loop")
        pass

    def _finish(self):
        LOG.info("Finish loop")
        pass

    def _get_sig_handlers(self):
        def stop_callback(s, frame):
            self.stop()

        base_handlers = {
            signal.SIGINT: stop_callback,
            signal.SIGTERM: stop_callback,
        }

        return base_handlers

    def _subscribe_signals(self, handlers):
        # TODO(g.melikov): implement `handlers` validation

        # default handlers
        handlers.setdefault(signal.SIGCHLD, signal.SIG_DFL)

        for sig, handl in handlers.items():
            signal.signal(sig, handl)
