"""REQ-001 end to end: missing OS information falls back safely (TASK-011; REQ AC-9, AC-10)."""

from datetime import timedelta

import pytest

from pc_handoff.persistence.schemas import validateActivityDocument, validateStatusDocument
from tests.acceptance.conftest import T0, iso, local
from tests.fakes.builders import osSession


@pytest.mark.parametrize(
    ("clientName", "clientAddress"),
    [(None, None), (None, "192.168.1.20"), ("ALICE-LAPTOP", None)],
    ids=["both-missing", "name-missing", "address-missing"],
)
def test_ac09_missing_client_information_shows_unknown(app, clientName, clientAddress):
    connectA = T0 - timedelta(minutes=5)
    app.start()
    app.poll()
    app.setSessions(
        osSession(
            2, "alice", clientName=clientName, clientAddress=clientAddress, connectTime=connectA
        )
    )
    app.tick()

    page = app.render()
    assert page.badge == f"In Use — alice ({clientAddress or 'Unknown'})"
    row = page.activeRow("alice")
    assert row[2:4] == [clientName or "Unknown", clientAddress or "Unknown"]
    assert "" not in row

    doc = app.statusJson()
    [entry] = doc["sessions"]
    assert (entry["clientName"], entry["clientAddress"]) == (clientName, clientAddress)
    [start] = app.events()
    assert (start["clientName"], start["clientAddress"]) == (clientName, clientAddress)
    assert validateStatusDocument(doc) == []
    assert validateActivityDocument(app.activityJson()) == []


def test_ac10_missing_connect_time_gives_an_approximate_duration(app):
    app.start()
    app.poll()
    app.setSessions(osSession(2, "alice", connectTime=None))  # the OS gives no connect time
    app.tick()
    firstSeenActive = app.now()  # T1
    app.tick()
    app.clock.advance(80)  # the dashboard is rendered later

    page = app.render()
    assert page.activeRow("alice")[4:] == [local(firstSeenActive), "00:01:25 (approx.)"]
    [entry] = app.statusJson()["sessions"]
    assert entry["usagePeriodStart"] == iso(firstSeenActive)
    assert (entry["connectTime"], entry["durationApproximate"]) == (None, True)

    app.setSessions()  # she logs off: the period ends
    app.tick()
    start, end = app.events()
    assert (start["eventType"], start["durationApproximate"]) == ("session_start", True)
    assert (end["eventType"], end["durationApproximate"]) == ("session_end", True)
    assert end["usagePeriodStart"] == iso(firstSeenActive)
