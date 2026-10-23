"""``StateStore``, ``DashboardSnapshot``, ``Notice`` and the runtime registry (TASK-009 step 1)."""

import dataclasses
import threading
import time
from datetime import timedelta

import pytest

from pc_handoff.domain.models import PcStatus
from pc_handoff.persistence.notices import StartupNotice
from pc_handoff.runtime import registry
from pc_handoff.runtime.state_store import (
    MAX_NOTICES,
    DashboardSnapshot,
    Notice,
    NoticeOrigin,
    StateStore,
)
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.builders import trackedActive, trackedDisconnected, trackerState

T0 = FAKE_CLOCK_START


def makeSnapshot(**overrides) -> DashboardSnapshot:
    alice = trackedActive(2, "alice", connectTime=T0, startEventTimestamp=T0)
    bob = trackedDisconnected(1, "bob", disconnectedSince=T0)
    state = trackerState([alice, bob], T0)
    fields = {
        "updatedAt": T0,
        "configWarnings": ("dashboardPort: missing; default 8501 applied",),
        "notices": (),
        "pollIntervalSeconds": 5,
    }
    fields.update(overrides)
    return DashboardSnapshot.fromState(state, **fields)


class TestDashboardSnapshot:
    def test_from_state_copies_the_tracker_fields_in_session_id_order(self):
        snapshot = makeSnapshot()
        assert snapshot.status is PcStatus.IN_USE
        assert snapshot.lastKnownStatus is PcStatus.IN_USE
        assert snapshot.lastKnownStatusAt == T0
        assert snapshot.lastSuccessfulPollAt == T0
        assert snapshot.detectionHealthy is True
        assert [s.userName for s in snapshot.sessions] == ["bob", "alice"]
        assert isinstance(snapshot.sessions, tuple)
        assert snapshot.updatedAt == T0
        assert snapshot.configWarnings == ("dashboardPort: missing; default 8501 applied",)
        assert snapshot.pollIntervalSeconds == 5

    def test_is_immutable(self):
        snapshot = makeSnapshot()
        with pytest.raises(dataclasses.FrozenInstanceError):
            snapshot.status = PcStatus.AVAILABLE  # type: ignore[misc]

    def test_lists_are_frozen_into_tuples(self):
        notice = Notice.fromStoreNotice(
            StartupNotice("status.json", "b", "t"), NoticeOrigin.STARTUP
        )
        snapshot = makeSnapshot(configWarnings=["w"], notices=[notice])
        assert snapshot.configWarnings == ("w",)
        assert snapshot.notices == (notice,)

    def test_only_the_most_recent_notices_are_kept(self):
        # TASK-010 / Critic TASK-009 S7: runtime notices only accumulate, so the snapshot
        # keeps the newest MAX_NOTICES and the page cannot grow without bound.
        notices = [
            Notice.fromStoreNotice(StartupNotice("f", None, f"n{i}"), NoticeOrigin.RUNTIME)
            for i in range(MAX_NOTICES + 5)
        ]
        snapshot = makeSnapshot(notices=notices)
        assert MAX_NOTICES == 20
        assert snapshot.notices == tuple(notices[-MAX_NOTICES:])
        assert snapshot.notices[-1].text == f"n{MAX_NOTICES + 4}"

    def test_a_short_notice_list_is_kept_whole(self):
        notices = [
            Notice.fromStoreNotice(StartupNotice("f", None, f"n{i}"), NoticeOrigin.STARTUP)
            for i in range(MAX_NOTICES)
        ]
        assert makeSnapshot(notices=notices).notices == tuple(notices)


class TestNotice:
    def test_each_notice_gets_a_unique_id(self):
        storeNotice = StartupNotice("activity_log.json", "activity_log.json.corrupt-x", "text")
        first = Notice.fromStoreNotice(storeNotice, NoticeOrigin.RUNTIME)
        second = Notice.fromStoreNotice(storeNotice, NoticeOrigin.RUNTIME)
        assert first.noticeId != second.noticeId
        assert len(first.noticeId) == 32
        assert (first.fileName, first.backupName, first.text, first.origin) == (
            "activity_log.json",
            "activity_log.json.corrupt-x",
            "text",
            NoticeOrigin.RUNTIME,
        )


class TestStateStore:
    def test_current_is_none_until_the_first_publish(self):
        assert StateStore().current() is None

    def test_publish_swaps_the_reference(self):
        store = StateStore()
        first = makeSnapshot()
        second = makeSnapshot(updatedAt=T0 + timedelta(seconds=5))
        store.publish(first)
        assert store.current() is first
        store.publish(second)
        assert store.current() is second

    def test_publish_rejects_anything_but_a_snapshot(self):
        with pytest.raises(TypeError):
            StateStore().publish({"status": "In Use"})  # type: ignore[arg-type]

    def test_readers_never_wait_for_the_publish_lock_holder(self):
        # current() is a plain reference read: even with the publish lock held (as if a
        # publisher were preempted mid-swap), readers return immediately.
        store = StateStore()
        snapshot = makeSnapshot()
        store.publish(snapshot)
        with store.lock:
            started = time.perf_counter()
            assert store.current() is snapshot
            assert time.perf_counter() - started < 0.05

    def test_concurrent_readers_always_see_a_complete_snapshot(self):
        store = StateStore()
        snapshots = [makeSnapshot(updatedAt=T0 + timedelta(seconds=i)) for i in range(50)]
        store.publish(snapshots[0])
        seen: list[DashboardSnapshot] = []
        stop = threading.Event()

        def read() -> None:
            while not stop.is_set():
                seen.append(store.current())

        readers = [threading.Thread(target=read) for _ in range(4)]
        for reader in readers:
            reader.start()
        for snapshot in snapshots:
            store.publish(snapshot)
        stop.set()
        for reader in readers:
            reader.join()
        assert seen
        assert all(any(s is known for known in snapshots) for s in seen)


class TestRegistry:
    @pytest.fixture(autouse=True)
    def cleanRegistry(self):
        registry.clearRuntime()
        yield
        registry.clearRuntime()

    def test_get_runtime_is_none_until_registered(self):
        assert registry.getRuntime() is None

    def test_register_get_and_clear(self):
        runtime = object()
        registry.registerRuntime(runtime)  # type: ignore[arg-type]
        assert registry.getRuntime() is runtime
        registry.clearRuntime()
        assert registry.getRuntime() is None

    def test_registering_the_same_runtime_again_is_harmless(self):
        runtime = object()
        registry.registerRuntime(runtime)  # type: ignore[arg-type]
        registry.registerRuntime(runtime)  # type: ignore[arg-type]
        assert registry.getRuntime() is runtime

    def test_a_second_runtime_is_refused(self):
        # One runtime, so one poller, per process (ADR-002).
        first = object()
        registry.registerRuntime(first)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="already registered"):
            registry.registerRuntime(object())  # type: ignore[arg-type]
        assert registry.getRuntime() is first
