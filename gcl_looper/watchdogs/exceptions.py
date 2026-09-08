#    Copyright 2025 Genesis Corporation.
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
"""Exceptions raised by the watchdogs.

The exception hierarchy is inherited from the ``rooster`` project so the
master-election watchdogs keep the original semantics:

* a :class:`WatchDogMinorException` is *recoverable*: the service is simply
  not the master right now (the lock is taken by another node) so the current
  iteration is skipped and the service keeps trying on the next one;
* a :class:`WatchDogCriticalException` is *fatal* for the current iteration
  (e.g. the heartbeat timed out or the database connection is lost) and is
  surfaced to the caller.
"""

from __future__ import annotations


class WatchDogException(Exception):
    """Base watchdog exception.

    Subclasses should define ``msg_template`` which is interpolated with the
    keyword arguments passed to the constructor, e.g.::

        raise LockNotAcquired(lock_key="my_lock")
    """

    msg_template = "An unknown watchdog exception occurred."

    def __init__(self, **kwargs: object) -> None:
        self._kwargs = kwargs
        super(WatchDogException, self).__init__()

    def __repr__(self) -> str:
        qual_name = "%s.%s" % (self.__module__, type(self).__name__)
        kwargs = ", ".join("%s=%r" % (k, v) for k, v in self._kwargs.items())
        return "%s(%s)" % (qual_name, kwargs)

    def __str__(self) -> str:
        try:
            return self.msg_template % self._kwargs
        except (TypeError, KeyError):
            return "%s: %r" % (self.msg_template, self._kwargs)

    @property
    def message(self) -> str:
        return str(self)

    @property
    def kwargs(self) -> dict:
        return dict(self._kwargs)


class WatchDogCriticalException(WatchDogException):
    """A fatal watchdog failure for the current iteration.

    It is *not* swallowed by the service loop and is reported as an error.
    """

    msg_template = "Watchdog critical exception: %(reason)r"


class WatchDogMinorException(WatchDogException):
    """A recoverable watchdog failure.

    The service is temporarily not the master; the loop swallows the exception
    and skips the iteration.
    """

    msg_template = "Watchdog minor exception: %(reason)r"


class ServiceIsMarkedFailed(WatchDogCriticalException):
    msg_template = "Service is marked as failed."


class ServiceHeartbeatTimeout(WatchDogCriticalException):
    msg_template = (
        "Service heartbeat timed out at %(check_time)s:"
        " %(delta)s > %(timeout)s (last: %(last_heartbeat)s)"
    )


class LockDriverNotFound(WatchDogCriticalException):
    """Requested lock driver type is not available/registered."""

    msg_template = "Unknown lock driver %(driver)r. Allowed variants: %(allowed)s."


class LockAcquireFailed(WatchDogMinorException):
    """The lock is currently held by somebody else (not the master)."""

    msg_template = "Can not acquire lock %(lock_key)r."


class LockConnectionError(WatchDogCriticalException):
    """The database connection needed by the lock is lost or unusable."""

    msg_template = "Lock database error: %(reason)r"
