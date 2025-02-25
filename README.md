**GenesisCoreLibs Looper Documentation**
==========================

**Overview**
------------

GCL Looper is a Python library designed to create daemon-like services that can run indefinitely, performing tasks at regular intervals or on demand.

**Usage Examples**
-----------------

### Basic Service

- Iterate infinitely
- There should be at least 5 seconds between start of previous and next iteration (`iter_min_period`)
- pause for 1 second between iterations (`iter_pause`)

```python
from gcl_looper.services import basic

class MyService(basic.BasicService):
    def __init__(self, iter_min_period=5, iter_pause=1):
        super(MyService, self).__init__(iter_min_period, iter_pause)

    def _iteration(self):
        print("Iteration", self._iteration_number)

service = MyService()
service.start()
```

### Finite Service without any pauses in-between

```python
from gcl_looper.services import basic

class MyFiniteService(basic.BasicService):
    def __init__(self, iter_min_period=0, iter_pause=0):
        super(MyFiniteService, self).__init__(iter_min_period, iter_pause)
        self.countdown = 3

    def _iteration(self):
        if self.countdown > 1:
            self.countdown -= 1
        else:
            self.stop()

service = MyFiniteService()
service.start()
```

### API service with database (restalchemy)

```python
from gcl_looper.services import bjoern_service
from gcl_looper.services import hub
from oslo_config import cfg
from restalchemy.storage.sql import engines
from restalchemy.common import config_opts as db_config_opts

from MY_PACKAGE.user_api import app

api_cli_opts = [
    cfg.StrOpt(
        "bind-host", default="127.0.0.1", help="The host IP to bind to"
    ),
    cfg.IntOpt("bind-port", default=8080, help="The port to bind to"),
    cfg.IntOpt(
        "workers", default=1, help="How many http servers should be started"
    ),
]

DOMAIN = "user_api"

CONF = cfg.CONF
CONF.register_cli_opts(api_cli_opts, DOMAIN)
db_config_opts.register_posgresql_db_opts(conf=CONF)


def main():

    serv_hub = hub.ProcessHubService()

    for _ in range(CONF[DOMAIN].workers):
        service = bjoern_service.BjoernService(
            wsgi_app=app.build_wsgi_application(),
            host=CONF[DOMAIN].bind_host,
            port=CONF[DOMAIN].bind_port,
            bjoern_kwargs=dict(reuse_port=True),
        )

        service.add_setup(
            lambda: engines.engine_factory.configure_postgresql_factory(
                conf=CONF
            )
        )

        serv_hub.add_service(service)

    serv_hub.start()


if __name__ == "__main__":
    main()

```

**Public interface:**
-----------------------------
* **`start()`**: Starts the service.
* **`stop()`**: Stop the service.
* **`_loop_iteration()`**: Performs one iteration of the service loop.

**Implement these methods to get usable service:**
---------------------------

* **`_iteration()`**: This method must be implemented by subclasses to perform the actual work at each iteration.
