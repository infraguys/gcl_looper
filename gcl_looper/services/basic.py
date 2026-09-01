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
import threading
import time
import typing

from gcl_looper.services import base

LOG = logging.getLogger(__name__)


class BoostSpec(typing.NamedTuple):
    """Iteration pacing override used in the boost mode."""

    iter_min_period: float
    iter_pause: float = 0.0


class BoostableServiceMixin:
    """Dynamic iteration pacing (the boost mode) for loop services.

    The default iteration pace (``iter_min_period``/``iter_pause``) can be
    temporarily overridden by the business logic, for instance, to react on
    events faster for a while or for a limited number of iterations, and
    then reset the pace back to the defaults.

    A boosted service automatically boosts its boost parent (see
    ``set_boost_parent``). So a boost of a service which is running inside
    a launchpad-like container speeds up the iterations of the whole
    container and, thus, of all its services.

    The boost can be abused by a buggy business logic, for instance, a
    service which enables the boost on every iteration because of a
    persistent error. Such a service stays in the boost mode forever and
    may starve other services which share its loop. The overheat
    protection (``configure_boost_protection``) limits the number of
    consecutive boosted iterations and forces the service to cool down
    in the default pace afterwards.
    """

    # Attributes the mixin expects to be defined by the service class
    _iter_min_period: float
    _iter_pause: float

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super(BoostableServiceMixin, self).__init__(*args, **kwargs)
        self._boost_lock = threading.Lock()
        self._boost_spec = None
        self._boost_remaining = None
        self._boost_parent = None
        self._boost_children = []
        self._wake_event = threading.Event()
        # Boost overheat protection state
        self._max_boost_iterations = None
        self._boost_cooldown_iterations = None
        self._boost_cooldown_remaining = None
        self._boosted_iteration_count = 0
        self._boost_overheat_count = 0

    # Public boost interface

    def boost(
        self,
        iter_min_period: float = 0,
        iter_pause: float = 0,
        iterations: typing.Optional[int] = 5,
        force: bool = False,
    ) -> bool:
        """Switch the service into the boost mode.

        The iteration pace is overridden with ``iter_min_period`` and
        ``iter_pause`` until the boost is reset (``reset_boost``) or
        ``iterations`` iterations are performed. ``None`` means the boost
        never expires by itself, but is only allowed when at least one
        of ``iter_min_period`` or ``iter_pause`` is greater than zero —
        otherwise the loop would spin without any delay. By default,
        the next 5 iterations run without a minimum period or pause.

        A repeated call replaces the previous boost. If the service has a
        boost parent, the boost is propagated to it.

        The boost is refused while the service or any of its boost
        ancestors is cooling down after overheating (see
        ``configure_boost_protection``).

        The loop is only woken up when the effective pacing actually
        changes, so a repeated ``boost()`` with the same values from inside
        ``_iteration()`` does not cause a busy-loop. Pass ``force=True``
        to wake the loop even when the pacing is unchanged.

        Args:
            iter_min_period: Boosted minimum period between iterations.
            iter_pause: Boosted pause between iterations.
            iterations: The number of iterations the boost is applied to.
            force: Wake the loop even if the boost pacing is unchanged.

        Returns:
            True if the boost has been applied, False if it has been
            refused because of a cooldown (own or ancestor).
        """
        if iter_min_period < 0:
            raise ValueError("`iter_min_period` can not be negative")
        if iter_pause < 0:
            raise ValueError("`iter_pause` can not be negative")
        if iterations is not None and iterations <= 0:
            raise ValueError("`iterations` must be greater than 0")
        if iterations is None and iter_min_period <= 0 and iter_pause <= 0:
            raise ValueError(
                "`iterations` can not be None with zero "
                "`iter_min_period` and `iter_pause` — "
                "this would cause an infinite busy-loop"
            )

        spec = BoostSpec(float(iter_min_period), float(iter_pause))

        with self._boost_lock:
            if self._boost_cooldown_remaining is not None:
                LOG.debug(
                    "Boost refused for %s: cooling down, %d iteration(s) remaining",
                    self.__class__.__name__,
                    self._boost_cooldown_remaining,
                )
                return False
            if self._is_ancestor_cooling_down():
                LOG.debug(
                    "Boost refused for %s: an ancestor is cooling down",
                    self.__class__.__name__,
                )
                return False
            previous_spec = self._boost_spec
            self._boost_spec = spec
            self._boost_remaining = iterations
        LOG.debug(
            "Boost mode enabled for %s: period=%s pause=%s remaining=%s",
            self.__class__.__name__,
            spec.iter_min_period,
            spec.iter_pause,
            iterations,
        )
        # Only wake the loop if the pacing changed or the caller explicitly
        # requested it. Repeated equal boosts must not cause a busy-loop.
        if force or previous_spec != spec:
            self._boost_changed()
        return True

    def reset_boost(self) -> None:
        """Reset the boost mode and return to the default iteration pace."""
        with self._boost_lock:
            was_boosted = self._boost_spec is not None
            self._boost_spec = None
            self._boost_remaining = None
        if was_boosted:
            LOG.debug("Boost mode reset for %s", self.__class__.__name__)

    def configure_boost_protection(
        self,
        max_boost_iterations: typing.Optional[int] = None,
        cooldown_iterations: typing.Optional[int] = None,
    ) -> None:
        """Configure the boost overheat protection.

        If the service stays in the boost mode for
        ``max_boost_iterations`` iterations in a row, the boost is dropped
        and the service is forced to perform ``cooldown_iterations``
        iterations in the default pace. The boost requests
        (``boost``/``boosted``) are refused while the service is cooling
        down. Once the cooldown is over, the service can be boosted again.

        The consecutive boosted iteration counter is reset as soon as the
        service performs an iteration in the default pace.

        Args:
            max_boost_iterations: The maximum number of consecutive
                boosted iterations before overheating. ``None`` disables
                the protection.
            cooldown_iterations: The number of forced default pace
                iterations after overheating.
        """
        if max_boost_iterations is None:
            if cooldown_iterations is not None:
                raise ValueError(
                    "`cooldown_iterations` requires `max_boost_iterations`"
                )
        else:
            if cooldown_iterations is None:
                raise ValueError(
                    "`max_boost_iterations` requires `cooldown_iterations`"
                )
            if max_boost_iterations <= 0:
                raise ValueError("`max_boost_iterations` must be greater than 0")
            if cooldown_iterations <= 0:
                raise ValueError("`cooldown_iterations` must be greater than 0")

        self._max_boost_iterations = max_boost_iterations
        self._boost_cooldown_iterations = cooldown_iterations

    def set_boost_parent(
        self, parent: typing.Optional["BoostableServiceMixin"]
    ) -> None:
        """Register the boost parent for the boost propagation.

        If the service is boosted, its parent (for example, a launchpad
        service which runs the service) is boosted as well, so the parent
        picks up the minimum iteration period of all its boosted children.
        """
        if parent is self:
            raise ValueError("A service can not be a boost parent for itself")
        # Check that ``parent`` is not a descendant of ``self`` — setting
        # it would create a cycle in the boost parent/child graph and
        # cause infinite recursion in ``_effective_boost_spec``.
        if parent is not None:
            ancestor = parent._boost_parent
            while ancestor is not None:
                if ancestor is self:
                    raise ValueError("Setting this boost parent would create a cycle")
                ancestor = ancestor._boost_parent

        with self._boost_lock:
            previous_parent = self._boost_parent
            self._boost_parent = parent

        if previous_parent is not None:
            previous_parent._remove_boost_child(self)

        if parent is not None:
            parent._add_boost_child(self)
            if self._effective_boost_spec() is not None:
                self._boost_changed()

    @property
    def boost_spec(self) -> typing.Optional[BoostSpec]:
        """The boost spec of the service itself (without children)."""
        return self._boost_spec

    @property
    def boost_remaining_iterations(self) -> typing.Optional[int]:
        """The number of remaining boosted iterations, if limited."""
        return self._boost_remaining

    @property
    def is_boosted(self) -> bool:
        """True if the service or one of its children is boosted."""
        if self._boost_cooldown_remaining is not None:
            return False
        return self._effective_boost_spec() is not None

    @property
    def effective_iter_min_period(self) -> float:
        """The current (boosted or default) minimum period."""
        return self._current_pacing().iter_min_period

    @property
    def effective_iter_pause(self) -> float:
        """The current (boosted or default) pause."""
        return self._current_pacing().iter_pause

    @property
    def is_cooling_down(self) -> bool:
        """True if the service is in the boost cooldown."""
        return self._boost_cooldown_remaining is not None

    @property
    def boost_cooldown_remaining(self) -> typing.Optional[int]:
        """The number of remaining cooldown iterations, if cooling down."""
        return self._boost_cooldown_remaining

    @property
    def boost_overheat_count(self) -> int:
        """The number of times the boost mode has overheated."""
        return self._boost_overheat_count

    # Internal boost logic

    def _add_boost_child(self, child: "BoostableServiceMixin") -> None:
        with self._boost_lock:
            if child not in self._boost_children:
                self._boost_children.append(child)

    def _remove_boost_child(self, child: "BoostableServiceMixin") -> None:
        with self._boost_lock:
            if child in self._boost_children:
                self._boost_children.remove(child)

    def _effective_boost_spec(
        self, _visited: typing.Optional[typing.Set[int]] = None
    ) -> typing.Optional[BoostSpec]:
        """The most aggressive boost of the service and its children."""
        if _visited is None:
            _visited = set()
        if id(self) in _visited:
            return None
        _visited.add(id(self))
        specs = []
        if self._boost_spec is not None:
            specs.append(self._boost_spec)
        for child in list(self._boost_children):
            child_spec = child._effective_boost_spec(_visited)
            if child_spec is not None:
                specs.append(child_spec)
        if not specs:
            return None
        return min(specs, key=lambda spec: (spec.iter_min_period, spec.iter_pause))

    def _current_pacing(self) -> BoostSpec:
        """The pacing (period/pause) currently used by the service loop."""
        # During cooldown, ignore all boosts (own and children's) to
        # enforce the default pace.
        if self._boost_cooldown_remaining is not None:
            return BoostSpec(
                float(self._iter_min_period),
                float(self._iter_pause),
            )
        spec = self._effective_boost_spec()
        if spec is None:
            return BoostSpec(
                float(self._iter_min_period),
                float(self._iter_pause),
            )
        return spec

    def _is_ancestor_cooling_down(self) -> bool:
        """Check if any boost ancestor is in the cooldown state."""
        parent = self._boost_parent
        while parent is not None:
            if parent._boost_cooldown_remaining is not None:
                return True
            parent = parent._boost_parent
        return False

    def _boost_changed(self) -> None:
        """Wake this service and its boost parent after a pacing change."""
        self._wake_event.set()
        parent = self._boost_parent
        if parent is not None:
            parent._boost_changed()

    def _consume_boost_iteration(self) -> None:
        with self._boost_lock:
            # Cooldown: count down forced default-pace iterations after an
            # overheat; boost stays refused until the counter hits zero.
            if self._boost_cooldown_remaining is not None:
                self._boost_cooldown_remaining -= 1
                if self._boost_cooldown_remaining <= 0:
                    self._boost_cooldown_remaining = None
                    LOG.debug(
                        "Boost cooldown finished for %s",
                        self.__class__.__name__,
                    )
                return

            # Use the effective spec (own + children) so that a launchpad
            # which is boosted through its children tracks the streak too.
            if self._effective_boost_spec() is None:
                # Default pace: a non-boosted iteration breaks the streak
                # of consecutive boosted iterations.
                self._boosted_iteration_count = 0
                return

            # Boosted iteration: grow the streak first, then check limits.
            self._boosted_iteration_count += 1

            # Overheat: too many boosted iterations in a row — drop the
            # boost and force a cooldown of `_boost_cooldown_iterations`.
            if (
                self._max_boost_iterations is not None
                and self._boosted_iteration_count >= self._max_boost_iterations
            ):
                self._boost_spec = None
                self._boost_remaining = None
                self._boosted_iteration_count = 0
                self._boost_overheat_count += 1
                self._boost_cooldown_remaining = self._boost_cooldown_iterations
                # Reset children's boosts so the effective spec drops
                # immediately. _effective_boost_spec already reads
                # children without their locks, so this is consistent.
                for child in list(self._boost_children):
                    child._boost_spec = None
                    child._boost_remaining = None
                LOG.debug(
                    "Boost mode overheated for %s after %d iteration(s): "
                    "%d iteration(s) cooldown",
                    self.__class__.__name__,
                    self._max_boost_iterations,
                    self._boost_cooldown_remaining,
                )
                return

            # Limited boost: count down the requested iterations; once it
            # reaches zero the boost expires and the default pace resumes.
            if self._boost_remaining is not None:
                self._boost_remaining -= 1
                if self._boost_remaining > 0:
                    return
                self._boost_spec = None
                self._boost_remaining = None
                self._boosted_iteration_count = 0
                LOG.debug("Boost mode expired for %s", self.__class__.__name__)


class BasicService(BoostableServiceMixin, base.AbstractService):
    __log_iteration__ = True

    def __init__(self, iter_min_period=1, iter_pause=0.1):
        super(BasicService, self).__init__()
        self._enabled = False
        self._stop_event = threading.Event()
        self._iter_min_period = iter_min_period
        self._iter_pause = iter_pause
        self._iteration_number = 0

    def _loop_iteration(self):
        iteration = self._iteration_number
        if self.__log_iteration__:
            LOG.debug(
                "Iteration #%d started for %s",
                iteration,
                self.__class__.__name__,
            )
        try:
            self._iteration()
            if self.__log_iteration__:
                LOG.debug(
                    "Iteration #%d finished for %s",
                    iteration,
                    self.__class__.__name__,
                )
        except Exception:
            LOG.exception(
                "Unexpected error during iteration #%d for %s",
                iteration,
                self.__class__.__name__,
            )
        finally:
            self._iteration_number += 1
            self._consume_boost_iteration()

    def _loop(self):
        self._enabled = True
        self._stop_event.clear()
        self._wake_event.clear()
        next_iteration_time = 0
        while self._enabled:
            current_time = time.monotonic()

            pacing = self._current_pacing()

            if self._wake_event.is_set():
                self._wake_event.clear()
                next_iteration_time = current_time

            if current_time >= next_iteration_time:
                self._loop_iteration()
                # stop() may have been called from inside _iteration();
                # exit immediately instead of sleeping for a full
                # iter_min_period before noticing _enabled is False.
                if not self._enabled:
                    break
                # Clear the wake before reading pacing: a boost() or
                # stop() that arrived during the iteration has already
                # updated _boost_spec under the lock, so reading pacing
                # after the clear picks up the new values. A wake that
                # arrives after the clear is not erased and will be
                # handled on the next loop pass.
                self._wake_event.clear()
                # Schedule the next run using pacing effective after the
                # iteration, which may change or expire the boost.
                pacing = self._current_pacing()
                next_iteration_time = current_time + pacing.iter_min_period

            time_to_sleep = next_iteration_time - time.monotonic()
            if time_to_sleep > 0 or pacing.iter_pause > 0:
                # Pacing changes and stop() interrupt the sleep.
                self._wake_event.wait(
                    timeout=max(time_to_sleep, pacing.iter_pause),
                )

    @abc.abstractmethod
    def _iteration(self):
        """Implement your logic per one iteration here"""
        raise NotImplementedError()

    def stop(self):
        LOG.info("Stop service")
        self._enabled = False
        self._stop_event.set()
        # Wake the loop up so it notices ``_enabled`` is False. The wake
        # event is the one the loop sleeps on (see ``_loop``).
        self._wake_event.set()
