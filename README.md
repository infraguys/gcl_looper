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

**Public interface:**
-----------------------------
* **`start()`**: Starts the service.
* **`stop()`**: Stop the service.
* **`_loop_iteration()`**: Performs one iteration of the service loop.

**Implement these methods to get usable service:**
---------------------------

* **`_iteration()`**: This method must be implemented by subclasses to perform the actual work at each iteration.
