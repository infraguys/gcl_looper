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

import threading
import time

import pytest

from gcl_looper.services import basic
from gcl_looper.services.oslo import launchpad


class TestBoostService(basic.BasicService):
    __test__ = False

    def _iteration(self):
        pass


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestBoostMode:
    def setup_method(self):
        self.service = TestBoostService(iter_min_period=3, iter_pause=0.1)

    def test_boost_overrides_pacing(self):
        assert self.service.effective_iter_min_period == 3
        assert self.service.effective_iter_pause == 0.1
        assert not self.service.is_boosted

        self.service.boost(0.5, iterations=None)

        assert self.service.effective_iter_min_period == 0.5
        assert self.service.effective_iter_pause == 0
        assert self.service.is_boosted
        assert self.service.boost_remaining_iterations is None
        assert self.service.boost_spec == basic.BoostSpec(0.5, 0.0)

    def test_boost_defaults_to_fastest_pacing(self):
        self.service.boost()

        assert self.service.boost_spec == basic.BoostSpec(0.0, 0.0)
        assert self.service.boost_remaining_iterations == 5

    def test_force_wakes_loop_when_pacing_is_unchanged(self):
        self.service.boost(0.5)
        self.service._wake_event.clear()

        self.service.boost(0.5)
        assert not self.service._wake_event.is_set()

        self.service.boost(0.5, force=True)
        assert self.service._wake_event.is_set()

    def test_boost_with_pause(self):
        self.service.boost(0.5, iter_pause=0.2)
        assert self.service.effective_iter_min_period == 0.5
        assert self.service.effective_iter_pause == 0.2

    def test_reset_boost(self):
        self.service.boost(0.5)
        self.service.reset_boost()

        assert not self.service.is_boosted
        assert self.service.boost_spec is None
        assert self.service.boost_remaining_iterations is None
        assert self.service.effective_iter_min_period == 3
        assert self.service.effective_iter_pause == 0.1

    def test_boost_limited_iterations(self):
        self.service.boost(0.5, iterations=2)

        self.service._loop_iteration()
        assert self.service.is_boosted
        assert self.service.boost_remaining_iterations == 1

        self.service._loop_iteration()
        assert not self.service.is_boosted
        assert self.service.effective_iter_min_period == 3

        # The boost doesn't affect iterations after expiration
        self.service._loop_iteration()
        assert self.service._iteration_number == 3
        assert self.service.boost_remaining_iterations is None

    def test_boost_replaces_previous(self):
        self.service.boost(1, iterations=5)
        self.service.boost(0.2, iterations=2)

        assert self.service.effective_iter_min_period == 0.2
        assert self.service.boost_remaining_iterations == 2

    def test_boost_validation(self):
        with pytest.raises(ValueError):
            self.service.boost(-1)
        with pytest.raises(ValueError):
            self.service.boost(1, iter_pause=-1)
        with pytest.raises(ValueError):
            self.service.boost(1, iterations=0)

    def test_boost_infinite_with_zero_pacing_rejected(self):
        # iterations=None with zero iter_min_period and zero iter_pause
        # would cause an infinite busy-loop — must be rejected.
        with pytest.raises(ValueError, match="busy-loop"):
            self.service.boost(0, iter_pause=0, iterations=None)
        # Allowed when at least one of them is non-zero.
        self.service.boost(0, iter_pause=0.1, iterations=None)
        self.service.reset_boost()
        self.service.boost(0.1, iter_pause=0, iterations=None)

    def test_boost_parent_validation(self):
        with pytest.raises(ValueError):
            self.service.set_boost_parent(self.service)


class TestBoostPropagation:
    def setup_method(self):
        self.child1 = TestBoostService(iter_min_period=3, iter_pause=0.1)
        self.child2 = TestBoostService(iter_min_period=5, iter_pause=0.1)
        self.parent = launchpad.LaunchpadService(
            [self.child1, self.child2],
            iter_min_period=1,
            iter_pause=0.1,
        )

    def test_child_boost_propagates_to_parent(self):
        assert self.parent.effective_iter_min_period == 1

        self.child1.boost(0.5)

        assert self.parent.is_boosted
        assert self.parent.effective_iter_min_period == 0.5

    def test_parent_uses_min_period_of_children(self):
        self.child1.boost(0.5, iter_pause=0.3)
        self.child2.boost(0.2, iter_pause=0.05)

        assert self.parent.effective_iter_min_period == 0.2
        assert self.parent.effective_iter_pause == 0.05

        self.child2.reset_boost()
        assert self.parent.effective_iter_min_period == 0.5
        assert self.parent.effective_iter_pause == 0.3

        self.child1.reset_boost()
        assert not self.parent.is_boosted
        assert self.parent.effective_iter_min_period == 1

    def test_parent_boost_applies_to_children_pacing(self):
        self.parent.boost(0.1)
        assert self.child1.effective_iter_min_period == 3  # not affected
        assert self.parent.effective_iter_min_period == 0.1

    def test_child_boost_expires_within_parent_iteration(self):
        self.child1.boost(0.5, iterations=1)
        assert self.parent.effective_iter_min_period == 0.5

        self.parent._iteration()

        assert not self.child1.is_boosted
        assert self.parent.effective_iter_min_period == 1

    def test_reparenting(self):
        other_parent = launchpad.LaunchpadService([], iter_min_period=2)

        self.child1.boost(0.5)
        assert self.parent.effective_iter_min_period == 0.5

        self.child1.set_boost_parent(other_parent)
        assert self.parent.effective_iter_min_period == 1
        assert other_parent.effective_iter_min_period == 0.5

        self.child1.set_boost_parent(None)
        assert not other_parent.is_boosted


class TestBoostOverheat:
    def setup_method(self):
        self.service = TestBoostService(iter_min_period=3, iter_pause=0.1)

    def test_overheat_forces_cooldown(self):
        self.service.configure_boost_protection(3, 5)

        self.service.boost(0.5)
        for _ in range(3):
            self.service._loop_iteration()

        # The boost is dropped and the cooldown is on
        assert not self.service.is_boosted
        assert self.service.effective_iter_min_period == 3
        assert self.service.is_cooling_down
        assert self.service.boost_cooldown_remaining == 5
        assert self.service.boost_overheat_count == 1

        # Boost requests are refused while cooling down
        assert not self.service.boost(0.5)
        assert not self.service.is_boosted

        for _ in range(5):
            self.service._loop_iteration()

        assert not self.service.is_cooling_down
        assert self.service.boost_cooldown_remaining is None

        # Boost is available again once the cooldown is over
        assert self.service.boost(0.5)
        assert self.service.effective_iter_min_period == 0.5

    def test_default_iteration_resets_overheat_counter(self):
        self.service.configure_boost_protection(3, 5)

        # Boost, leave the boost, boost again - no overheating
        self.service.boost(0.5)
        self.service._loop_iteration()
        self.service._loop_iteration()
        self.service.reset_boost()
        self.service._loop_iteration()  # default pace iteration

        self.service.boost(0.5)
        self.service._loop_iteration()
        self.service._loop_iteration()

        assert self.service.is_boosted
        assert not self.service.is_cooling_down
        assert self.service.boost_overheat_count == 0

        # The streak is 3 iterations in a row now - overheat
        self.service._loop_iteration()
        assert self.service.is_cooling_down
        assert self.service.boost_overheat_count == 1

    def test_repeated_boost_calls_lead_to_overheat(self):
        # Simulates a buggy business logic which enables the boost on
        # every iteration
        self.service.configure_boost_protection(4, 10)

        for _ in range(4):
            assert self.service.boost(0.5)
            self.service._loop_iteration()

        assert self.service.is_cooling_down
        assert not self.service.is_boosted
        assert self.service.effective_iter_min_period == 3

    def test_overheat_with_limited_boost(self):
        # A logic which keeps refreshing a limited boost must overheat as
        # well as an unlimited boost one
        self.service.configure_boost_protection(3, 5)

        for _ in range(3):
            self.service.boost(0.5, iterations=10)
            self.service._loop_iteration()

        assert self.service.is_cooling_down
        assert not self.service.is_boosted
        assert self.service.boost_overheat_count == 1

    def test_protection_disabled_by_default(self):
        self.service.boost(0.5, iterations=None)
        for _ in range(100):
            self.service._loop_iteration()

        assert self.service.is_boosted
        assert not self.service.is_cooling_down

    def test_disable_protection(self):
        self.service.configure_boost_protection(3, 5)
        self.service.configure_boost_protection(None, None)

        self.service.boost(0.5, iterations=None)
        for _ in range(5):
            self.service._loop_iteration()

        assert self.service.is_boosted
        assert not self.service.is_cooling_down

    def test_protection_validation(self):
        with pytest.raises(ValueError):
            self.service.configure_boost_protection(0, 5)
        with pytest.raises(ValueError):
            self.service.configure_boost_protection(5, 0)
        with pytest.raises(ValueError):
            self.service.configure_boost_protection(5, None)
        with pytest.raises(ValueError):
            self.service.configure_boost_protection(None, 5)

    def test_overheat_propagates_to_parent(self):
        parent = launchpad.LaunchpadService(
            [self.service],
            iter_min_period=1,
            iter_pause=0.1,
        )
        self.service.configure_boost_protection(2, 4)

        self.service.boost(0.5)
        assert parent.effective_iter_min_period == 0.5

        # The parent iterations drive the child iterations
        parent._iteration()
        parent._iteration()

        assert self.service.is_cooling_down
        assert not parent.is_boosted
        assert parent.effective_iter_min_period == 1

        # Child boost requests are refused during the cooldown
        assert not self.service.boost(0.5)
        assert parent.effective_iter_min_period == 1


class TestWake:
    def test_boost_wakes_loop_up(self):
        service = TestBoostService(iter_min_period=60, iter_pause=0)
        iterations = []
        service._iteration = lambda: iterations.append(1)

        loop_thread = threading.Thread(target=service._loop)
        loop_thread.start()

        assert wait_for(lambda: len(iterations) == 1)

        service.boost(0.01)
        assert wait_for(lambda: len(iterations) >= 3), (
            "boost() did not speed up the loop"
        )

        service.reset_boost()
        service.stop()
        loop_thread.join(timeout=2)
        assert not loop_thread.is_alive()

    def test_boost_from_inside_iteration_speeds_up_next(self):
        # When boost is called from within _iteration, the next iteration
        # must use the boosted period, not the period that was in effect
        # before the iteration started.
        service = TestBoostService(iter_min_period=60, iter_pause=0)
        iterations = []

        def _iteration():
            iterations.append(time.monotonic())
            if len(iterations) == 1:
                service.boost(0.01)

        service._iteration = _iteration

        loop_thread = threading.Thread(target=service._loop)
        loop_thread.start()

        # First iteration runs immediately, calls boost(0.01) inside it.
        # The second iteration must happen ~0.01s later, not ~60s later.
        assert wait_for(lambda: len(iterations) >= 3, timeout=5), (
            "boost() called inside _iteration did not speed up the next iteration"
        )

        service.reset_boost()
        service.stop()
        loop_thread.join(timeout=2)
        assert not loop_thread.is_alive()

    def test_reset_boost_from_inside_iteration_slows_down_next(self):
        # When reset_boost is called from within _iteration, the next
        # iteration must use the default period, not the boosted period
        # that was in effect before the iteration started.
        service = TestBoostService(iter_min_period=60, iter_pause=0)
        iterations = []

        def _iteration():
            iterations.append(time.monotonic())
            if len(iterations) == 3:
                service.reset_boost()

        service._iteration = _iteration
        service.boost(0.01)

        loop_thread = threading.Thread(target=service._loop)
        loop_thread.start()

        # Three fast boosted iterations, the third one resets the boost.
        assert wait_for(lambda: len(iterations) >= 3, timeout=5)

        # The fourth iteration must NOT come quickly — the default period
        # is 60s, so within 1s there should be no new iteration.
        time.sleep(1)
        assert len(iterations) == 3, (
            "reset_boost() inside _iteration did not slow down the next "
            "iteration — stale boosted next_iteration_time was used"
        )

        service.stop()
        loop_thread.join(timeout=2)
        assert not loop_thread.is_alive()


class TestBoostNoBusyLoop:
    def test_repeated_boost_from_iteration_respects_period(self):
        """A service that calls boost() on every iteration must not
        busy-loop — the boost period must be respected."""
        service = TestBoostService(iter_min_period=10, iter_pause=0)
        service.boost(0.05, iterations=None)

        loop_thread = threading.Thread(target=service._loop)
        loop_thread.start()

        # Let it run for 0.5s. With a 0.05s boost period, we expect
        # roughly 10 iterations, not 100k+.
        time.sleep(0.5)
        count = service._iteration_number
        service.stop()
        loop_thread.join(timeout=2)

        # Allow generous headroom for scheduling jitter, but a busy-loop
        # would produce 50k+ iterations.
        assert count < 100, (
            f"Expected ~10 iterations in 0.5s with 0.05s period, "
            f"got {count} — busy-loop detected"
        )

    def test_boost_from_inside_iteration_respects_period(self):
        """A service that calls boost() inside _iteration() on every
        iteration must respect the boost period, not busy-loop.

        The wake set by boost() is cleared after the iteration (before
        reading pacing), so it cannot make the next wait() return
        immediately.
        """

        class GreedyBoostService(basic.BasicService):
            def _iteration(self):
                self.boost(0.05, iterations=5, force=True)

        service = GreedyBoostService(iter_min_period=10, iter_pause=0)
        loop_thread = threading.Thread(target=service._loop)
        loop_thread.start()

        time.sleep(0.5)
        count = service._iteration_number
        service.stop()
        service._wake_event.set()
        loop_thread.join(timeout=2)

        assert count < 100, (
            f"Expected ~10 iterations in 0.5s with 0.05s period, "
            f"got {count} — boost() from _iteration() caused busy-loop"
        )


class TestLaunchpadOverheat:
    def test_launchpad_overheat_with_greedy_child(self):
        """A launchpad with overheat protection must overheat when a child
        keeps boosting on every iteration."""

        class GreedyChild(basic.BasicService):
            def _iteration(self):
                self.boost(0.001, iterations=None)

        child = GreedyChild(iter_min_period=10, iter_pause=0)
        parent = launchpad.LaunchpadService([child], iter_min_period=1, iter_pause=0)
        parent.configure_boost_protection(5, 10)

        for _ in range(100):
            parent._loop_iteration()

        assert parent.boost_overheat_count >= 1, (
            "Launchpad overheat protection never triggered with a greedy child"
        )
        assert parent.is_cooling_down or parent.boost_overheat_count > 1

    def test_child_boost_refused_during_parent_cooldown(self):
        """When the parent is in cooldown, child boost requests must be
        refused."""

        class GreedyChild(basic.BasicService):
            def _iteration(self):
                self.boost(0.001, iterations=None)

        child = GreedyChild(iter_min_period=10, iter_pause=0)
        parent = launchpad.LaunchpadService([child], iter_min_period=1, iter_pause=0)
        parent.configure_boost_protection(3, 10)

        # Overheat the parent
        for _ in range(3):
            parent._loop_iteration()

        assert parent.is_cooling_down

        # Child boost must be refused
        assert not child.boost(0.001, iterations=None), (
            "Child boost was not refused during parent cooldown"
        )

    def test_default_pacing_during_cooldown(self):
        """During cooldown, the launchpad must use the default pacing
        regardless of children's boosts."""
        child = TestBoostService(iter_min_period=10, iter_pause=0)
        parent = launchpad.LaunchpadService([child], iter_min_period=5, iter_pause=0.1)
        parent.configure_boost_protection(2, 5)

        child.boost(0.001, iterations=None)
        assert parent.effective_iter_min_period == 0.001

        # Overheat
        parent._loop_iteration()
        parent._loop_iteration()

        assert parent.is_cooling_down
        assert parent.effective_iter_min_period == 5, (
            "Launchpad did not use default pacing during cooldown"
        )
        assert not parent.is_boosted


class TestBoostCycleDetection:
    def test_cycle_detection_in_set_boost_parent(self):
        a = TestBoostService(iter_min_period=1, iter_pause=0)
        b = TestBoostService(iter_min_period=1, iter_pause=0)
        a.set_boost_parent(b)
        with pytest.raises(ValueError, match="cycle"):
            b.set_boost_parent(a)

    def test_self_parent_rejected(self):
        service = TestBoostService(iter_min_period=1, iter_pause=0)
        with pytest.raises(ValueError):
            service.set_boost_parent(service)
