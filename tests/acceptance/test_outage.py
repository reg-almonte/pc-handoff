"""REQ-001 end to end: enumeration outages and recovery (TASK-011; REQ AC-11, AC-12).

The timeout case blocks the fake enumeration for real inside the real ``TimedSessionSource``
with a short timeout (0.3 s) to keep the suite fast; the production 3 s constant is exercised
with a blocked enumeration in ``test_concurrency_and_timing.py`` (REQ AC-21).
"""

import threading
import time
from datetime import timedelta

import pytest

from pc_handoff.clock import parseIso
from pc_handoff.detection.timed_source import ENUMERATION_TIMEOUT_SECONDS
from pc_handoff.runtime.app_runtime import POLL_ENUMERATION_FAILED, POLL_OK
from tests.acceptance.conftest import T0, hms, iso, local
from tests.fakes.builders import osSession

UNAVAILABLE = "Session information is currently unavailable"
CONNECT_A = T0 + timedelta(seconds=2)  # C1


def alice(state="Active", **overrides):
    overrides.setdefault("connectTime", CONNECT_A)
    overrides.setdefault("clientName", "ALICE-LAPTOP")
    return osSession(2, "alice", state, **overrides)


def aliceActiveAtP(app):
    """alice logs on after the first poll; returns (her session_start, P)."""
    app.start()
    app.poll()
    app.setSessions(alice())
    app.tick()
    [start] = app.starts()
    app.tick()
    return start, app.now()


def failOnePoll(app, kind: str, traceId: str) -> str:
    """One scheduled poll that fails with ``kind``; returns its outcome."""
    app.clock.advance(app.interval)
    if kind != "timeout":
        app.provider.failNext(kind)
        return app.poll(traceId)
    gate = threading.Event()
    app.provider.blockNext(gate)  # the call does not return in time
    try:
        return app.poll(traceId)
    finally:
        gate.set()
        deadline = time.monotonic() + 5
        while app.runtime.source.isBusy() and time.monotonic() < deadline:
            time.sleep(0.01)


@pytest.mark.parametrize("kind", ["error", "access_denied", "timeout"])
def test_ac11_enumeration_failure_shows_unknown_without_false_events(app, kind):
    assert ENUMERATION_TIMEOUT_SECONDS == 3  # production value; 0.3 s here keeps it fast
    app.enumerationTimeoutSeconds = 0.3
    _, lastPoll = aliceActiveAtP(app)  # P, status In Use
    before = app.statusJson()
    assert before["status"] == "In Use"
    eventsBefore = app.events()

    traceId = "ac11" + "0" * 28
    assert failOnePoll(app, kind, traceId) == POLL_ENUMERATION_FAILED
    for _ in range(2):  # the outage lasts a few polls
        app.clock.advance(app.interval)
        app.provider.failNext("error")
        assert app.poll() == POLL_ENUMERATION_FAILED

    doc = app.statusJson()
    assert (doc["status"], doc["detectionHealthy"], doc["lastKnownStatus"]) == (
        "Unknown",
        False,
        "In Use",
    )
    assert doc["lastSuccessfulPollAt"] == before["lastSuccessfulPollAt"] == iso(lastPoll)
    assert doc["sessions"] == before["sessions"]  # the last known sessions are kept
    assert app.events() == eventsBefore  # no session_start / session_end during the failure

    app.clock.advance(600)
    page = app.render()
    assert (page.badge, page.badgeKind) == ("Unknown", "info")
    lastKnownAt = parseIso(doc["lastKnownStatusAt"])
    assert page.warnings == [UNAVAILABLE, f"Last known status: In Use at {local(lastKnownAt)}"]
    assert page.subheaders == [f"Last known sessions (as of {local(lastPoll)})"]
    frozen = hms((lastPoll - CONNECT_A).total_seconds())
    assert page.activeRow("alice")[5] == frozen
    app.clock.advance(300)
    assert app.rerender().activeRow("alice")[5] == frozen  # does not tick

    lines = app.appLog()
    [failure] = [
        x for x in lines if x["message"] == "session enumeration failed" and x["traceId"] == traceId
    ]
    assert failure["level"] == "WARNING"
    assert failure["detail"]["kind"] == kind and failure["detail"]["message"]
    [completed] = [x for x in lines if x["message"] == "poll completed" and x["traceId"] == traceId]
    assert completed["detail"]["outcome"] == "enumeration_failed"
    assert not [x for x in lines if "activity" in x["message"] and x["traceId"] == traceId]

    # The process did not exit: the next poll runs normally.
    app.clock.advance(app.interval)
    assert app.poll() == POLL_OK
    assert app.statusJson()["status"] == "In Use"


@pytest.mark.parametrize(
    "case",
    [
        "still-active",
        "disconnected",
        "disconnected-no-os-time",
        "gone",
        "reconnected",
        "reconnected-os-time-at-c2",
        "reconnected-os-time-after-c2",
    ],
)
def test_ac12_recovery_after_an_outage_applies_the_blind_rule(app, case):
    start, lastPoll = aliceActiveAtP(app)  # S = start["timestamp"], P = lastPoll
    startTimestamp = parseIso(start["timestamp"])
    for _ in range(2):
        app.clock.advance(app.interval)
        app.provider.failNext("error")
        app.poll()
    assert app.statusJson()["status"] == "Unknown"
    assert app.render().warnings[0] == UNAVAILABLE
    eventsBefore = app.events()

    disconnectAt = lastPoll + timedelta(seconds=7)  # D, during the outage
    connectB = lastPoll + timedelta(seconds=12)  # C2
    sessions = {
        "still-active": [alice()],
        "disconnected": [alice("Disconnected", disconnectTime=disconnectAt)],
        "disconnected-no-os-time": [alice("Disconnected", disconnectTime=None)],
        "gone": [],
        "reconnected": [alice(connectTime=connectB, disconnectTime=disconnectAt)],
        # The reconnect window is [C1, C2] (Critic TASK-011 S3): C2 is accepted, C2 + 1 s
        # is rejected although it is before R.
        "reconnected-os-time-at-c2": [alice(connectTime=connectB, disconnectTime=connectB)],
        "reconnected-os-time-after-c2": [
            alice(connectTime=connectB, disconnectTime=connectB + timedelta(seconds=1))
        ],
    }[case]
    app.setSessions(*sessions)
    app.clock.advance(app.interval)
    assert app.poll() == POLL_OK
    recoveredAt = app.now()  # R

    doc = app.statusJson()
    expectedStatus = "Available" if case in ("gone",) or "disconnected" in case else "In Use"
    assert (doc["status"], doc["detectionHealthy"]) == (expectedStatus, True)
    assert doc["lastSuccessfulPollAt"] == iso(recoveredAt)
    page = app.render()
    assert page.badge == expectedStatus and page.warnings == []  # the banner is gone

    newEvents = app.events()[len(eventsBefore) :]
    if case == "still-active":
        assert newEvents == []  # her period continues
        return
    fallback = max(lastPoll, startTimestamp)  # the later of P and S
    expected = {
        "disconnected": ("disconnect", disconnectAt),
        "disconnected-no-os-time": ("disconnect", fallback),
        "gone": ("logoff", fallback),
        "reconnected": ("reconnect_detected", disconnectAt),  # D in [C1, C2]
        "reconnected-os-time-at-c2": ("reconnect_detected", connectB),
        "reconnected-os-time-after-c2": ("reconnect_detected", fallback),
    }[case]
    end = newEvents[0]
    assert end["eventType"] == "session_end"
    assert end["usagePeriodId"] == start["usagePeriodId"]
    assert (end["endCause"], end["periodEnd"]) == (expected[0], iso(expected[1]))
    assert (end["reason"], end["durationApproximate"]) == ("detected_after_outage", True)
    assert end["durationSeconds"] == (expected[1] - CONNECT_A).total_seconds() >= 0
    if case.startswith("reconnected"):
        [_, newStart] = newEvents
        assert newStart["eventType"] == "session_start"
        assert newStart["usagePeriodId"] != start["usagePeriodId"]
        assert newStart["usagePeriodStart"] == iso(connectB)
        assert newStart["reason"] == "detected_after_outage"
    else:
        assert newEvents == [end]
