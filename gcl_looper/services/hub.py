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

import multiprocessing
import logging
import threading
import sys

from gcl_looper.services import base
from gcl_looper.services import basic


LOG = logging.getLogger(__name__)


class ProcessHubService(basic.BasicService):
    _instance_class = multiprocessing.Process

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._services = []
        self._instances = {}

    def add_service(self, service):
        """Add a service to the list of services to start."""
        if isinstance(service, base.AbstractService):
            self._services.append(service)
        else:
            raise ValueError(
                "Service must implement the AbstractService interface."
            )

    def _iteration(self):
        for instance in self._instances.values():
            if not instance.is_alive():
                LOG.error(
                    "Child service(pid:%i) is not running, let's stop",
                    instance.pid,
                )
                self.stop()
                return

    def _setup(self):
        if sys.platform == "darwin" and sys.version_info >= (3, 8):
            multiprocessing.set_start_method("fork")

        for service in self._services:
            instance = self._instance_class(target=service.start)
            self._instances[service] = instance
            instance.start()

    def _stop_instance(self, service, instance):
        LOG.info("Stop child service(pid:%i)", instance.pid)
        try:
            instance.terminate()
        except OSError as e:  # Process doesn't exist
            LOG.exception(
                "Failed to terminate child service, pid:%i", instance.pid
            )

    def stop(self):
        LOG.info("Stop service")
        self._enabled = False
        # Stop all managed services
        for service, instance in self._instances.items():
            self._stop_instance(service, instance)
        for instance in self._instances.values():
            instance.join()


class ThreadHubService(ProcessHubService):
    _instance_class = threading.Thread

    def _setup(self):
        # Threads can't hangle signals so we need to disable them
        for service in self._services:
            service.should_subscribe_signals = False
        super()._setup()

    def _stop_instance(self, service, instance):
        LOG.info("Stop child service(native_id:%i)", instance.native_id)
        service.stop()
