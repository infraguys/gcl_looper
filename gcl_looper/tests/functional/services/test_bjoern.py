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
import multiprocessing
import os
import requests
import signal
import socket

import pytest

from gcl_looper.services import bjoern_service

LOG = logging.getLogger(__name__)


@pytest.fixture
def wsgi_app():
    # Create a mock WSGI application for testing
    class MockWSGISubclass(object):
        def __call__(self, environ, start_response):
            start_response("200 yo", [("Content-Type", "text/plain")])
            return b"TESTBJOERN"

    return MockWSGISubclass()


def run_service(*args, ready, **kwargs):
    service = bjoern_service.BjoernService(*args, **kwargs)
    loop = service._loop

    def loop_and_publish_ready():
        ready.set()
        loop()

    service._loop = loop_and_publish_ready
    service.start()


class TestBjoernService:
    def test_start_and_stop(self, wsgi_app):
        host = "127.0.0.1"
        with socket.socket() as sock:
            sock.bind((host, 0))
            port = sock.getsockname()[1]
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        process = context.Process(
            target=run_service, args=(wsgi_app, host, port), kwargs={"ready": ready}
        )
        process.start()
        try:
            assert ready.wait(10), "Bjoern did not start"
            response = requests.get(
                f"http://{host}:{port}/", headers={"Connection": "close"}, timeout=5
            )
            assert response.status_code == 200
            assert response.text == "TESTBJOERN"
            assert process.is_alive()
            os.kill(process.pid, signal.SIGINT)
            process.join(timeout=5)
            assert not process.is_alive(), "Bjoern did not stop gracefully"
            assert process.exitcode == 0
        finally:
            if process.is_alive():
                process.kill()
            process.join(timeout=5)
            process.close()
