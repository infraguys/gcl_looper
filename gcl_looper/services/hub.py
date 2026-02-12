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

from gcl_looper.services import base
from gcl_looper.services import basic
from gcl_looper import utils

LOG = logging.getLogger(__name__)


class BasicHubService(basic.BasicService):
    __log_iteration__ = False

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
        raise NotImplementedError()

    def _setup(self):
        raise NotImplementedError()

    def _stop_instance(self, service, instance):
        raise NotImplementedError()

    def stop(self):
        raise NotImplementedError()


class ProcessHubService(BasicHubService):
    _instance_class = multiprocessing.get_context("fork").Process

    def add_service(self, service):
        if service.__mp_downgrade_user__:
            service.add_setup(
                lambda: utils.downgrade_user_group_privileges(
                    service.__mp_downgrade_user__
                )
            )
        super().add_service(service)

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

    def add_service(self, service):
        super(ProcessHubService, self).add_service(service)

    def _setup(self):
        # Threads can't hangle signals so we need to disable them
        for service in self._services:
            service.should_subscribe_signals = False
        super()._setup()

    def _stop_instance(self, service, instance):
        LOG.info("Stop child service(native_id:%i)", instance.native_id)
        service.stop()
