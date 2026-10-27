"""REQ-001 end to end: detection, status and the usage-period audit trail (TASK-011).

REQ AC-1 to AC-8 and AC-22 to AC-26, each with the AC's own Given/When/Then values, through
the composed runtime (``buildRuntime``), real files and the real dashboard page.

Timing: polls are driven by hand on the ``FakeClock``. A change made between two polls is
picked up by the next scheduled poll, which comes at most one ``sessionPollIntervalSeconds``
after it; "within sessionPollIntervalSeconds + 2 s" is asserted on those fake times here, and
in real time with the production poller in ``test_concurrency_and_timing.py`` (REQ AC-21)
and ``test_config.py`` (REQ AC-14).
"""

import json
import re
import uuid
from datetime import timedelta

import pytest

from pc_handoff.clock import parseIso
from tests.acceptance.conftest import T0, hms, iso, local
from tests.fakes.builders import osSession
from tests.fakes.log_factory import makeEvents

C1 = T0 - timedelta(minutes=30)
LOGON = T0 - timedelta(hours=1)
CONSOLE = "Console"


def isUuid(value: object) -> bool:
    return isinstance(value, str) and str(uuid.UUID(value)) == value


def alice(state="Active", **overrides):
    """alice on RDP session 2 from ALICE-LAPTOP (192.168.1.20), connected at C1."""
    fields = {
        "clientName": "ALICE-LAPTOP",
        "clientAddress": "192.168.1.20",
        "logonTime": LOGON,
        "connectTime": C1,
    }
    fields.update(overrides)
    return osSession(2, "alice", state, **fields)


def bob(state="Active", **overrides):
    """bob on RDP session 3 from BOB-PC (192.168.1.21)."""
    fields = {
        "clientName": "BOB-PC",
        "clientAddress": "192.168.1.21",
        "logonTime": LOGON + timedelta(minutes=5),
        "connectTime": C1 + timedelta(minutes=5),
    }
    fields.update(overrides)
    return osSession(3, "bob", state, **fields)


def statusOf(app) -> str:
    return app.statusJson()["status"]


def changeBeforeNextPoll(app, offset: int, *sessions) -> None:
    """The OS state changes ``offset`` s after the last poll; then the next scheduled poll."""
    app.clock.advance(offset)
    app.setSessions(*sessions)
    app.clock.advance(app.interval - offset)
    app.poll()


# --- REQ AC-1 -------------------------------------------------------------------------------


def test_ac01_active_rdp_session_is_shown_in_use(app):
    app.start()
    app.setSessions(alice())
    app.poll()
    app.clock.advance(65)  # the dashboard is opened a little after the poll

    page = app.render()
    assert (page.badge, page.badgeKind) == ("In Use — alice (192.168.1.20)", "error")
    assert page.subheaders == ["Active sessions"]
    # Duration = now - connect time, exactly (the AC allows +-(refresh + 2) s).
    expected = (app.now() - C1).total_seconds()
    assert page.activeRows == [
        ["alice", "RDP", "ALICE-LAPTOP", "192.168.1.20", local(C1), hms(expected)]
    ]
    assert page.activeRows[0][5] == "00:31:05"

    doc = app.statusJson()
    assert (doc["status"], doc["detectionHealthy"]) == ("In Use", True)
    assert doc["lastSuccessfulPollAt"] == iso(T0)
    [entry] = doc["sessions"]
    assert entry["state"] == "Active" and isUuid(entry["usagePeriodId"])
    shownFields = ("userName", "connectionType", "clientName", "clientAddress")
    assert [entry[k] for k in shownFields] == ["alice", "RDP", "ALICE-LAPTOP", "192.168.1.20"]
    assert entry["usagePeriodStart"] == entry["connectTime"] == iso(C1)
    assert entry["durationApproximate"] is False


# --- REQ AC-2 -------------------------------------------------------------------------------


def test_ac02_no_active_user_session_shows_available(app):
    services = osSession(0, None, "Disconnected", winStationName="Services", protocolType=0)
    listener = osSession(65536, None, "Ignored", winStationName="RDP-Tcp", protocolType=0)
    logonScreen = osSession(1, None, "Active", CONSOLE, logonTime=None, connectTime=None)
    app.start()
    app.setSessions(services, listener, logonScreen)
    app.poll()
    app.tick()
    app.tick()

    page = app.render()
    assert (page.badge, page.badgeKind) == ("Available", "success")
    assert page.texts == ["No active user sessions"]
    assert page.subheaders == ["Active sessions"]  # the disconnected list is hidden
    assert page.activeRows == [] and page.disconnectedRows == []
    doc = app.statusJson()
    assert (doc["status"], doc["sessions"]) == ("Available", [])
    assert app.events() == []


# --- REQ AC-3 -------------------------------------------------------------------------------


def test_ac03_a_usage_period_start_is_logged_exactly_once(app):
    seeded = [event.toJson() for event in makeEvents(4)]
    app.dataDir.mkdir(parents=True)
    log = {"schemaVersion": 1, "events": seeded}
    app.path("activity_log.json").write_text(json.dumps(log, indent=4), encoding="utf-8")
    app.start()
    app.poll()
    assert statusOf(app) == "Available" and app.events() == seeded  # N = 4

    logonB = T0 + timedelta(seconds=1)
    connectB = T0 + timedelta(seconds=2)
    changeBeforeNextPoll(app, 2, bob(logonTime=logonB, connectTime=connectB))
    detectedAt = app.now()
    for _ in range(3):  # Active for 3 or more further poll cycles
        app.tick()

    events = app.events()
    assert len(events) == len(seeded) + 1
    assert events[: len(seeded)] == seeded  # earlier events unchanged
    new = events[-1]
    assert isUuid(new["eventId"]) and isUuid(new["usagePeriodId"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", new["timestamp"])
    assert {k: v for k, v in new.items() if k not in ("eventId", "usagePeriodId")} == {
        "eventType": "session_start",
        "timestamp": iso(detectedAt),
        "sessionId": 3,
        "userName": "bob",
        "domain": "CORP",
        "logonTime": iso(logonB),
        "clientName": "BOB-PC",
        "clientAddress": "192.168.1.21",
        "connectionType": "RDP",
        "connectTime": iso(connectB),
        "usagePeriodStart": iso(connectB),
        "durationApproximate": False,
        "reason": "observed",
    }


# --- REQ AC-4 -------------------------------------------------------------------------------


def test_ac04_logoff_ends_the_period_and_the_pc_becomes_available(app):
    app.start()
    app.poll()
    connectB = T0 + timedelta(seconds=1)
    changeBeforeNextPoll(app, 1, bob(connectTime=connectB))
    [start] = app.starts()
    app.tick()
    app.tick()

    loggedOffAt = app.now() + timedelta(seconds=2)  # bob signs out between two polls
    changeBeforeNextPoll(app, 2)
    detectedAt = app.now()

    assert detectedAt - loggedOffAt <= timedelta(seconds=app.interval + 2)
    doc = app.statusJson()
    assert (doc["status"], doc["sessions"]) == ("Available", [])
    [end] = app.ends()
    assert end["usagePeriodId"] == start["usagePeriodId"]
    assert (end["endCause"], end["reason"], end["durationApproximate"]) == (
        "logoff",
        "observed",
        False,
    )
    assert end["periodEnd"] == iso(detectedAt)
    assert end["durationSeconds"] == (detectedAt - connectB).total_seconds()
    trueLength = (loggedOffAt - connectB).total_seconds()
    assert abs(end["durationSeconds"] - trueLength) <= app.interval + 2


# --- REQ AC-5 -------------------------------------------------------------------------------


@pytest.mark.parametrize("osReportsDisconnectTime", [True, False], ids=["os-time", "no-os-time"])
def test_ac05_disconnect_ends_the_period_and_the_pc_becomes_available(app, osReportsDisconnectTime):
    app.start()
    app.poll()
    connectA = T0 + timedelta(seconds=2)
    changeBeforeNextPoll(app, 2, alice(connectTime=connectA))
    [start] = app.starts()
    app.tick()

    disconnectAt = app.now() + timedelta(seconds=2)  # she closes the RDP client window
    reported = disconnectAt if osReportsDisconnectTime else None
    changeBeforeNextPoll(
        app, 2, alice("Disconnected", connectTime=connectA, disconnectTime=reported)
    )
    detectedAt = app.now()
    assert detectedAt - disconnectAt <= timedelta(seconds=app.interval + 2)
    disconnectedSince = disconnectAt if osReportsDisconnectTime else detectedAt

    page = app.render()
    assert page.badge == "Available"
    assert page.activeRows == [] and page.texts == ["No active user sessions"]
    assert page.subheaders == ["Active sessions", "Logged on but disconnected (not in use)"]
    assert page.disconnectedRows == [["alice", local(disconnectedSince)]]

    [end] = app.ends()
    assert end["usagePeriodId"] == start["usagePeriodId"]
    assert (end["endCause"], end["reason"], end["durationApproximate"]) == (
        "disconnect",
        "observed",
        False,
    )
    assert end["periodEnd"] == iso(disconnectedSince)
    assert end["durationSeconds"] == (disconnectedSince - connectA).total_seconds()
    [entry] = app.statusJson()["sessions"]
    assert (entry["state"], entry["usagePeriodId"], entry["usagePeriodStart"]) == (
        "Disconnected",
        None,
        None,
    )
    assert entry["disconnectedSince"] == end["periodEnd"]


# --- REQ AC-6 -------------------------------------------------------------------------------


@pytest.mark.parametrize("transitional", ["Connected", "ConnectQuery"])
def test_ac06_reconnect_starts_a_new_usage_period(app, transitional):
    app.start()
    app.poll()
    connectA = T0 + timedelta(seconds=2)
    changeBeforeNextPoll(app, 2, alice(connectTime=connectA))
    disconnectAt = app.now() + timedelta(seconds=2)
    changeBeforeNextPoll(
        app, 2, alice("Disconnected", connectTime=connectA, disconnectTime=disconnectAt)
    )
    [first] = app.starts()
    assert len(app.ends()) == 1 and statusOf(app) == "Available"
    app.tick()
    [identityBefore] = app.statusJson()["sessions"]
    eventsBefore = app.events()

    # She reconnects from ALICE-DESKTOP; Windows shows a transitional state for one poll.
    connectB = app.now() + timedelta(seconds=3)
    desktop = {"clientName": "ALICE-DESKTOP", "clientAddress": "192.168.1.30"}
    changeBeforeNextPoll(
        app, 3, alice(transitional, connectTime=connectB, disconnectTime=disconnectAt, **desktop)
    )
    assert app.events() == eventsBefore  # no event for the poll that saw the transitional state
    assert statusOf(app) == "Available"

    activeAt = app.now() + timedelta(seconds=1)
    changeBeforeNextPoll(
        app, 1, alice(connectTime=connectB, disconnectTime=disconnectAt, **desktop)
    )
    assert app.now() - activeAt <= timedelta(seconds=app.interval + 2)
    assert statusOf(app) == "In Use"

    app.clock.advance(30)
    page = app.render()
    assert page.badge == "In Use — alice (192.168.1.30)"
    row = page.activeRow("alice")
    assert row[2:4] == ["ALICE-DESKTOP", "192.168.1.30"]
    assert row[4:] == [local(connectB), hms((app.now() - connectB).total_seconds())]

    [newStart] = app.events()[len(eventsBefore) :]
    assert newStart["eventType"] == "session_start"
    assert newStart["usagePeriodId"] != first["usagePeriodId"]
    assert newStart["sessionId"] == first["sessionId"] == 2
    assert newStart["usagePeriodStart"] == iso(connectB)
    [identityAfter] = app.statusJson()["sessions"]
    for key in ("sessionId", "userName", "domain", "logonTime", "firstDetectedAt"):
        assert identityAfter[key] == identityBefore[key], key


# --- REQ AC-7 -------------------------------------------------------------------------------


def test_ac07_in_use_needs_an_active_session_among_several(app):
    bobDisconnectedAt = T0 - timedelta(minutes=10)
    app.start()
    app.setSessions(alice(), bob("Disconnected", disconnectTime=bobDisconnectedAt))
    app.poll()
    for _ in range(3):
        app.tick()

    page = app.render()
    assert page.badge == "In Use — alice (192.168.1.20)"
    duration = hms((app.now() - C1).total_seconds())
    assert page.activeRows == [
        ["alice", "RDP", "ALICE-LAPTOP", "192.168.1.20", local(C1), duration]
    ]
    assert page.disconnectedRows == [["bob", local(bobDisconnectedAt)]]
    assert [(e["eventType"], e["userName"]) for e in app.events()] == [("session_start", "alice")]


# --- REQ AC-8 -------------------------------------------------------------------------------


def test_ac08_status_does_not_flap_between_several_active_sessions(app):
    app.start()
    app.poll()
    connectA, connectB = T0 + timedelta(seconds=1), T0 + timedelta(seconds=2)
    changeBeforeNextPoll(app, 2, alice(connectTime=connectA), bob(connectTime=connectB))
    starts = {e["userName"]: e["usagePeriodId"] for e in app.starts()}
    assert statusOf(app) == "In Use"

    statuses = []
    disconnectA = app.now() + timedelta(seconds=1)
    aliceOff = alice("Disconnected", connectTime=connectA, disconnectTime=disconnectA)
    changeBeforeNextPoll(app, 1, aliceOff, bob(connectTime=connectB))
    statuses.append(statusOf(app))
    assert app.render().badge == "In Use — bob (192.168.1.21)"
    for _ in range(2):  # bob logs off two or more poll cycles later
        app.tick()
        statuses.append(statusOf(app))
    changeBeforeNextPoll(app, 1, aliceOff)
    statuses.append(statusOf(app))

    assert statuses == ["In Use", "In Use", "In Use", "Available"]
    assert app.render().badge == "Available"
    ends = app.ends()
    assert [(e["userName"], e["endCause"]) for e in ends] == [
        ("alice", "disconnect"),
        ("bob", "logoff"),
    ]
    assert [e["usagePeriodId"] for e in ends] == [starts["alice"], starts["bob"]]


# --- REQ AC-22 ------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["logged-off", "others-active", "taken-over"])
def test_ac22_logoff_of_a_disconnected_session_writes_no_event(app, variant):
    app.start()
    others = [bob()] if variant == "others-active" else []
    app.setSessions(*others)
    app.poll()
    connectA = T0 + timedelta(seconds=1)
    changeBeforeNextPoll(app, 1, alice(connectTime=connectA), *others)
    disconnectA = app.now() + timedelta(seconds=1)
    aliceOff = alice("Disconnected", connectTime=connectA, disconnectTime=disconnectA)
    changeBeforeNextPoll(app, 1, aliceOff, *others)
    aliceEvents = [e["eventType"] for e in app.events() if e["userName"] == "alice"]
    assert aliceEvents == ["session_start", "session_end"]
    assert statusOf(app) == ("In Use" if others else "Available")
    eventsBefore = app.events()

    if variant == "taken-over":
        # bob signs in and Windows logs alice off, reusing her session ID 2.
        signIn = app.now() + timedelta(seconds=1)
        changeBeforeNextPoll(app, 1, osSession(2, "bob", logonTime=signIn, connectTime=signIn))
    else:
        changeBeforeNextPoll(app, 1, *others)

    expectedStatus = "Available" if variant == "logged-off" else "In Use"
    expectedBadge = {
        "logged-off": "Available",
        "others-active": "In Use — bob (192.168.1.21)",
        "taken-over": "In Use — bob (192.168.1.20)",
    }[variant]
    doc = app.statusJson()
    assert "alice" not in [s["userName"] for s in doc["sessions"]]
    assert doc["status"] == expectedStatus
    page = app.render()
    assert page.disconnectedRows == []
    assert page.badge == expectedBadge
    assert app.events()[: len(eventsBefore)] == eventsBefore
    newEvents = app.events()[len(eventsBefore) :]
    assert not [e for e in newEvents if e["userName"] == "alice"]
    if variant == "taken-over":
        assert [(e["eventType"], e["userName"], e["sessionId"]) for e in newEvents] == [
            ("session_start", "bob", 2)
        ]
    else:
        assert newEvents == []


# --- REQ AC-23 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "osDisconnectTime",
    ["in-window", "none", "outside-window", "at-upper-bound", "above-upper-bound"],
)
def test_ac23_disconnect_and_reconnect_within_one_poll_is_detected(app, osDisconnectTime):
    app.start()
    app.poll()
    connectA = T0 + timedelta(seconds=2)
    changeBeforeNextPoll(app, 2, alice(connectTime=connectA))
    [first] = app.starts()
    startTimestamp = parseIso(first["timestamp"])  # S
    app.tick()
    previousPoll = app.now()  # P
    assert statusOf(app) == "In Use"

    disconnectAt = previousPoll + timedelta(seconds=1)
    connectB = previousPoll + timedelta(seconds=3)  # C2 > C1, before the next poll
    reported = {
        "in-window": disconnectAt,
        "none": None,
        "outside-window": connectA - timedelta(minutes=1),  # below C1
        # The window is [C1, C2] (Critic TASK-011 S3): C2 itself is accepted, one second
        # later is rejected even though it is still before the detection time.
        "at-upper-bound": connectB,
        "above-upper-bound": connectB + timedelta(seconds=1),
    }[osDisconnectTime]
    changeBeforeNextPoll(app, 3, alice(connectTime=connectB, disconnectTime=reported))
    assert statusOf(app) == "In Use"

    fallback = max(previousPoll, startTimestamp)  # the later of P and S
    accepted = osDisconnectTime in ("in-window", "at-upper-bound")
    expectedEnd = reported if accepted else fallback
    [end] = app.ends()
    assert end["usagePeriodId"] == first["usagePeriodId"]
    assert (end["endCause"], end["reason"], end["durationApproximate"]) == (
        "reconnect_detected",
        "observed",
        True,
    )
    assert end["periodEnd"] == iso(expectedEnd)
    assert end["durationSeconds"] == (expectedEnd - connectA).total_seconds()
    second = app.starts()[1]
    assert app.events()[-2:] == [end, second]  # the end first, then the new start
    assert second["usagePeriodId"] != first["usagePeriodId"]
    assert second["usagePeriodStart"] == iso(connectB)

    app.clock.advance(20)
    page = app.render()
    assert page.badge == "In Use — alice (192.168.1.20)"
    elapsed = hms((app.now() - connectB).total_seconds())
    assert page.activeRow("alice")[4:] == [local(connectB), elapsed]


# --- REQ AC-24 ------------------------------------------------------------------------------


def test_ac24_last_active_session_disconnecting_makes_the_pc_available(app):
    bobDisconnectedAt = T0 - timedelta(minutes=10)
    bobOff = bob("Disconnected", disconnectTime=bobDisconnectedAt)
    app.start()
    app.setSessions(bobOff)
    app.poll()
    connectA = T0 + timedelta(seconds=1)
    changeBeforeNextPoll(app, 1, alice(connectTime=connectA), bobOff)
    assert statusOf(app) == "In Use"

    disconnectA = app.now() + timedelta(seconds=2)
    aliceOff = alice("Disconnected", connectTime=connectA, disconnectTime=disconnectA)
    changeBeforeNextPoll(app, 2, aliceOff, bobOff)
    assert app.now() - disconnectA <= timedelta(seconds=app.interval + 2)

    assert statusOf(app) == "Available"
    page = app.render()
    assert page.badge == "Available" and page.activeRows == []
    assert page.disconnectedRows == [
        ["alice", local(disconnectA)],
        ["bob", local(bobDisconnectedAt)],
    ]
    assert [(e["eventType"], e["userName"]) for e in app.events()] == [
        ("session_start", "alice"),
        ("session_end", "alice"),
    ]
    assert app.ends()[0]["endCause"] == "disconnect"


# --- REQ AC-25 ------------------------------------------------------------------------------


def test_ac25_an_active_console_logon_shows_in_use_with_type_console(app):
    logonScreen = osSession(1, None, "Active", CONSOLE, logonTime=None, connectTime=None)
    app.start()
    app.setSessions(logonScreen)
    app.poll()
    assert app.statusJson()["sessions"] == [] and statusOf(app) == "Available"

    signedIn = app.now() + timedelta(seconds=3)  # erin signs in at the PC's own keyboard
    erin = osSession(1, "erin", "Active", CONSOLE, logonTime=signedIn, connectTime=signedIn)
    changeBeforeNextPoll(app, 3, erin)

    doc = app.statusJson()
    assert doc["status"] == "In Use"
    [entry] = doc["sessions"]
    assert [entry[k] for k in ("userName", "state", "connectionType")] == [
        "erin",
        "Active",
        CONSOLE,
    ]
    assert (entry["clientName"], entry["clientAddress"]) == (None, None)
    page = app.render()
    assert page.badge == "In Use — erin (N/A)"
    assert page.activeRow("erin")[:4] == ["erin", CONSOLE, "N/A", "N/A"]
    [start] = app.events()
    assert (start["eventType"], start["connectionType"], start["reason"]) == (
        "session_start",
        CONSOLE,
        "observed",
    )


# --- REQ AC-26 ------------------------------------------------------------------------------


@pytest.mark.parametrize("connectTimeOnResume", ["updated", "stale"])
def test_ac26_rdp_session_resumed_at_the_console_starts_a_console_period(app, connectTimeOnResume):
    app.start()
    app.poll()
    connectA = T0 + timedelta(seconds=2)
    changeBeforeNextPoll(app, 2, alice(connectTime=connectA))
    [u1] = app.starts()
    disconnectA = app.now() + timedelta(seconds=2)
    changeBeforeNextPoll(
        app, 2, alice("Disconnected", connectTime=connectA, disconnectTime=disconnectA)
    )
    assert [e["endCause"] for e in app.ends()] == ["disconnect"]
    assert statusOf(app) == "Available"
    app.tick()
    [before] = app.statusJson()["sessions"]

    # Windows resumes the same session (same ID and logon time) at the physical console.
    # Whether ConnectTime is refreshed on a console resume is a shared-PC manual check (A11);
    # if it is not, the stale time counts as not reported (I4) and the start is approximate.
    resumedAt = app.now() + timedelta(seconds=2)
    reportedConnect = resumedAt if connectTimeOnResume == "updated" else connectA
    console = osSession(2, "alice", "Active", CONSOLE, logonTime=LOGON, connectTime=reportedConnect)
    changeBeforeNextPoll(app, 2, console)
    detectedAt = app.now()

    doc = app.statusJson()
    assert doc["status"] == "In Use"
    [entry] = doc["sessions"]
    assert entry["connectionType"] == CONSOLE
    for key in ("sessionId", "userName", "logonTime", "firstDetectedAt"):
        assert entry[key] == before[key], key
    [u1Again, u2] = app.starts()
    assert u1Again == u1
    assert u2["usagePeriodId"] != u1["usagePeriodId"]
    assert (u2["connectionType"], u2["sessionId"]) == (CONSOLE, 2)
    if connectTimeOnResume == "updated":
        assert (u2["usagePeriodStart"], u2["durationApproximate"]) == (iso(resumedAt), False)
    else:
        assert (u2["usagePeriodStart"], u2["durationApproximate"]) == (iso(detectedAt), True)
    assert len(app.ends()) == 1  # no further session_end for U1
    assert app.render().activeRow("alice")[1] == CONSOLE
