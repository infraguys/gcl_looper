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


def run_service(*args, **kwargs):
    service = bjoern_service.BjoernService(*args, **kwargs)
    service.start()


class TestBjoernService:
    def test_start_and_stop(self, wsgi_app):
        self.host = "127.0.0.1"
        self.port = 8082

        process = multiprocessing.get_context("fork").Process(
            target=run_service, args=(wsgi_app, self.host, self.port)
        )
        process.start()

        assert process.is_alive()

        url = "http://%s:%s/" % (self.host, self.port)
        response = requests.get(url)

        assert response.status_code == 200
        assert response.text == "TESTBJOERN"
        assert process.is_alive()

        # NOTE(g.melikov): see comment in BjoernService._exit_gracefully()
        for i in range(10):
            print(i)
            if not process.is_alive() or process.join(timeout=1):
                break
            try:
                os.kill(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

        assert not process.is_alive(), "Bjoern did not stop gracefully"
