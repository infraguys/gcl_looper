#    Copyright 2026 George Melikov <mail@gmelikov.ru>
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#         http://www.apache.org/licenses/LICENSE-2.0
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

"""Private exec entrypoint; accepts factory data only from the supervisor."""

import os
import pickle
import runpy
import signal
import sys
import types

from gcl_looper.services import hub


class _Ready:
    def __init__(self, fd):
        self.fd = fd

    def set(self):
        os.write(self.fd, b"R")
        os.close(self.fd)


def main():
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    # Factories may belong to the caller's __main__, just like module functions.
    # Read the header before unpickling the callable to restore that namespace.
    ready = _Ready(int(sys.argv[1]))
    (main_module, main_path, argv), factory_data = pickle.load(sys.stdin.buffer)
    sys.stdin.close()
    with open(os.devnull) as devnull:
        os.dup2(devnull.fileno(), 0)
    sys.stdin = open(0, closefd=False)
    sys.argv = argv
    if main_module == "__main__" or (main_module and main_module.endswith(".__main__")):
        main_module = main_path = None
    if main_module or (main_path and os.path.isfile(main_path)):
        module = types.ModuleType("__service_main__")
        namespace = (
            runpy.run_module(main_module, run_name="__service_main__", alter_sys=True)
            if main_module
            else runpy.run_path(main_path, run_name="__service_main__")
        )
        module.__dict__.update(namespace)
        sys.modules["__main__"] = sys.modules["__service_main__"] = module
    factory = pickle.loads(factory_data)
    hub._run_service_factory(factory, ready)


if __name__ == "__main__":
    main()
