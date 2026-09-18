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
* **Boost mode**: `boost()`, `reset_boost()` — see the Boost mode section
  below.

**Implement these methods to get usable service:**
---------------------------

* **`_iteration()`**: This method must be implemented by subclasses to perform the actual work at each iteration.

### Boost mode (dynamic iteration pace)

By default the iteration pace (`iter_min_period`/`iter_pause`) is static.
The boost mode allows the business logic to change the pace dynamically,
for instance, to react on events faster for a while and then return to the
default pace:

```python
service = MyService(iter_min_period=3, iter_pause=0.1)

# Run the next 5 iterations as fast as possible (the default)
service.boost()

# Or iterate each 0.5 second for 5 iterations
service.boost(0.5)

# Or boost until explicitly reset (pass iterations=None)
service.boost(0.5, iterations=None)

# Or boost only the next 10 iterations
service.boost(0.5, iterations=10)

# Reapply the boost and wake the loop even if the pacing is unchanged
service.boost(0.5, force=True)

# Return to the default pace
service.reset_boost()
```

**Boost interface:**

* **`boost(iter_min_period=0, iter_pause=0, iterations=5, force=False)`**:
  Switch the service into boost mode. With no arguments, the next 5
  iterations run without a minimum period or pause. A repeated call replaces
  the previous boost; `force=True` also wakes the loop when the pacing is
  unchanged. `iterations=None` means the boost never expires by itself, but
  is only allowed when at least one of `iter_min_period` or `iter_pause` is
  greater than zero — otherwise the loop would spin without any delay.
  Returns `False` if the boost is refused during the overheat cooldown (own
  or an ancestor's).
* **`reset_boost()`**: Return to the default iteration pace.
* **`is_boosted`**, **`boost_remaining_iterations`**,
  **`effective_iter_min_period`**, **`effective_iter_pause`**: Inspect the
  current pace.

A buggy business logic can enable the boost on every iteration and keep
the service in the boost mode forever, starving the other services which
share its loop. The overheat protection limits that:

```python
# No more than 100 boosted iterations in a row, then force 200
# iterations in the default pace during which boost is refused
service.configure_boost_protection(
    max_boost_iterations=100,
    cooldown_iterations=200,
)
```

The counter of consecutive boosted iterations is reset as soon as the
service performs an iteration in the default pace. When the service
overheats, the boost is dropped and `boost()` returns `False` and has no
effect until the cooldown iterations are done.

* **`configure_boost_protection(max_boost_iterations, cooldown_iterations)`**:
  Configure the protection (`None, None` disables it).
* **`is_cooling_down`**, **`boost_cooldown_remaining`**,
  **`boost_overheat_count`**: Inspect the cooldown state.

Boost mode works inside the `LaunchpadService` as well: since the inner
services are iterated by the launchpad itself, boosting any of the inner
services boosts the whole launchpad and, thus, all of its services. The
launchpad uses the minimum iteration period between its own pace and the
pace of all boosted inner services. A boost is applied to the launchpad
until all inner boosts are reset or expired.

### Process Hub service

Process Hub allows running multiple services in separate processes. It's useful when you want to run multiple instances of a service (e.g., multiple API workers) or different services that should be isolated.

**Security Feature: Privilege Downgrade**

When using `ProcessHubService`, you can set `__mp_downgrade_user__` on a child service to automatically downgrade process privileges after the fork. This is a security best practice to minimize attack surface - start as root (if needed to bind to privileged ports), then downgrade to an unprivileged user.

* **`__mp_downgrade_user__`**: Class attribute set to a username (e.g., `"nobody"`). When set, the child process will downgrade to this user after forking. Default is `None` (no downgrade).

```python
from gcl_looper.services import hub
from gcl_looper.services import bjoern_service

serv_hub = hub.ProcessHubService()

# BjoernService has __mp_downgrade_user__ = "nobody" by default
for _ in range(4):  # 4 workers
    service = bjoern_service.BjoernService(
        wsgi_app=my_app,
        host="0.0.0.0",
        port=80,  # Privileged port, needs root to bind
        bjoern_kwargs=dict(reuse_port=True),
    )
    serv_hub.add_service(service)

serv_hub.start()
# Each worker starts as root to bind port 80, then downgrades to 'nobody'
```

**Note:** This feature only works on Linux and requires the process to start as root. The target user must exist on the system.

**Manual Privilege Downgrade**

You can also manually downgrade privileges using the utility function:

```python
from gcl_looper import utils

# Downgrade to 'nobody' user
utils.downgrade_user_group_privileges("nobody")
```

### Launchpad Service

Launchpad service is a service that can run multiple services and execute them sequentially. It's convenient when you have multiple services that need to be run in a specific order or the services aren't heavy and you don't want to use multiprocessing. Also it simplifies the configuration of the services.

**Basic usage:**

```python
from gcl_looper.services.oslo import launchpad

services = [
    MyService(),
    MyFiniteService(),
]

service = launchpad.LaunchpadService(services)
service.start()
```

The most important part in the launchpad service is its configuration. In the configuration you specify how to run inner services, how to configure them and how to initialize them.

**Configuration options:**

* **`services`**: List of services to run. Each service can be specified as a string in the format `module.path:ServiceName::count` where `count` is optional and defaults to 1.
* **`common_registrator_opts`**: Common options for all services. These options are passed to the service constructor.
* **`common_initializer`**: Common initializer for all services. This initializer is called after the service is created and before it is started.
* **`iter_min_period`**: Minimum period between iterations of the service loop.
* **`iter_pause`**: Pause between iterations of the service loop.

**Example:**

```ini
[DEFAULT]
verbose = True
debug = True

[launchpad]
services =
    my_package.service_foo:FooService,
    my_package.service_bar:BarService,
    my_package.service_baz:BazService
common_registrator_opts = my_package.service_common:common_opts
common_initializer = my_package.service_common:common_init

[my_package.service_foo:FooService]
name = foo

[my_package.service_bar:BarService]
name = bar
project_id = 123

[my_package.service_baz:BazService]
param1 = value1
param2 = value2
```

**Example with multiple instances of the same service:**

```ini
[DEFAULT]
verbose = True
debug = True

[launchpad]
services =
    my_package.service_foo:FooService,
    my_package.service_bar:BarService::2

[my_package.service_foo:FooService]
name = foo

[my_package.service_bar:BarService::0]
name = bar0
project_id = 123

[my_package.service_bar:BarService::1]
name = bar1
project_id = 456
```

### Leader (master) election

When the same daemon runs on several nodes, you usually want only **one**
instance (the *master*) to do the real work while the others stay on standby.
GCL Looper implements this with a `LeaderElector`: every service iteration
asks the elector for the leadership (backed by a distributed lock); only the
node that owns the lock runs the iteration, the others silently skip it until
they take over.

This is a port of the MySQL `GET_LOCK`/table-lock mechanism from the original
`rooster`/`node-manager` stack, rebuilt around a clean election abstraction
and a database-agnostic lock-driver interface. Only PostgreSQL ships today,
but a new backend (e.g. MySQL) is added by implementing one small driver
class and registering it.

**Install the PostgreSQL extra:**

```bash
pip install gcl_looper[pg]   # brings in psycopg (v3)
```

#### Two PostgreSQL lock backends

* `postgres_advisory` — session-level advisory locks (`pg_try_advisory_lock`).
  A direct port of MySQL `GET_LOCK`: non-blocking acquire and **instant
  failover** (the lock dies the moment the session/connection drops). The
  elector keeps its **own dedicated connection** and never shares the
  application pool (restalchemy/psycopg_pool keep working as usual).
* `postgres_table` — a lock row in a table with an atomic `UPDATE`. Portable
  across databases and works through a normal connection. A dead master is
  taken over after `lock_timeout` seconds (the original node-manager used this
  style).

#### Programmatic usage

```python
from gcl_looper.election import db
from gcl_looper.election.drivers import pg_table
from gcl_looper.services import basic

elector = db.DbLeaderElector(
    pg_table.PostgresTableLockDriver(
        connection_url="postgresql://user:pass@db:5432/mydb",
        lock_key="my_master_service",
        lock_timeout=30,
    ),
)


class MyMasterService(basic.BasicService):
    def _iteration(self):
        # Runs only on the node that currently holds the lock.
        self.ensure_master()
        print("I am the master now")

    # Optional hooks, fired on every leadership transition:
    def _on_become_master(self):
        ...  # create master-only resources

    def _on_lose_master(self):
        ...  # release them

# Every iteration is guarded: when another node is the master the iteration
# is skipped. Only one node is the master at a time.
MyMasterService(elector=elector).start()
```

A service without an elector is always the master (unchanged behavior). The
lock is released automatically on shutdown so a standby takes over
immediately (table locks also expire by themselves after `lock_timeout`).

Locks are refreshed at iteration boundaries; no background refresh runs while
business code is executing. Long-running work can call `self.ensure_master()`
to verify ownership and refresh a table lease. A lease cannot revoke Python
code between a successful check and its next side effect, so operations that
must reject stale masters need a fencing token or an equivalent destination
check.
Boost is refused with a warning while the service or its launchpad is in
standby. Losing leadership resets boost for the complete nested service tree.

#### Launchpad / oslo configuration

The whole launchpad (or any single service) can be guarded through config.
Add the election options to the `[launchpad]` section (they use the
`election_` prefix):

```ini
[launchpad]
services =
    my_package.service_foo:FooService
# Master election for the whole launchpad:
election_backend = postgres_advisory
election_connection_url = postgresql://user:pass@db:5432/mydb
election_lock_key = my_daemon_master
election_refresh_interval = 2
# Table-based locks only:
election_lock_timeout = 30
election_table_name = gcl_looper_locks
```

While a node is master, lock ownership is checked in the service thread on the
first iteration after `election_refresh_interval` elapses. Calling
`ensure_master()` performs an immediate backend check and refreshes a table
lease. For table locks, `lock_timeout` must exceed the maximum permitted time
between these checks, including iteration time and scheduling delays. A stuck
iteration therefore stops refreshing its lease and allows takeover. Standby
nodes retry at their normal, non-boosted service pace.

When `election_backend` is set, the launchpad runs its inner services only
while it owns the lock; standby launchpads stay idle. Configure election either
for the whole launchpad or for individual services, not at both levels.
`election_backend` accepts `none` (default), `always`, or a registered lock
driver (`postgres_advisory`, `postgres_table`, ...).

To guard an individual service instead of the whole launchpad, register the
same options (with any prefix) in the service's `svc_get_config_opts` and
build the elector in its constructor:

```python
from gcl_looper.election import config as election_config


class MyMasterService(basic.BasicService):
    @classmethod
    def svc_get_config_opts(cls):
        return [
            # ... own options ...
            *election_config.get_config_opts(),
        ]

    def __init__(self, name, **kwargs):
        elector, kwargs = election_config.build_elector_from_kwargs(
            default_lock_key=f"{name}_lock", **kwargs
        )
        super().__init__(elector=elector, **kwargs)
```

#### Adding a new database (e.g. MySQL)

1. Subclass `gcl_looper.election.drivers.base.BaseLockDriver` (for a table
   lock it is usually enough to subclass
   `gcl_looper.election.drivers.table.TableLockDriver` and override the
   connection and SQL-flavor hooks), or
2. register it by name and select it via `election_backend`:

```python
from gcl_looper.election.drivers import register_driver

register_driver("mysql", "my_package.locks:MySQLTableLockDriver")
```

#### Running the functional tests

The PostgreSQL functional tests are skipped unless a server is provided:

```bash
podman run -d --name gcl_looper_pg -e POSTGRES_PASSWORD=*** \
    -e POSTGRES_DB=gcltest -p 5433:5432 docker.io/library/postgres:18
GCL_LOOPER_TEST_PG_URL=postgresql://postgres:test@127.0.0.1:5433/gcltest \
    pytest gcl_looper/tests/functional/election
```
