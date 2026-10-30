"""Unit tests for KnownUsersStore (TASK-026, REQ-004)."""

from datetime import datetime, timedelta, timezone

from pc_handoff.persistence.known_users_store import KnownUsersStore
from pc_handoff.persistence.paths import DataPaths

TZ = timezone(timedelta(hours=9))
T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=TZ)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def test_empty_load_when_file_does_not_exist(tmp_path):
    paths = DataPaths(tmp_path)
    store = KnownUsersStore(paths, FakeClock(T0))
    assert store.load() == {}
    assert store.getName("192.168.1.50") is None
    assert store.getKnownUsers() == {}


def test_corrupt_files_return_empty_dict(tmp_path):
    paths = DataPaths(tmp_path)
    store = KnownUsersStore(paths, FakeClock(T0))
    paths.knownUsers.write_bytes(b"not json")
    assert store.load() == {}

    paths.knownUsers.write_text('{"users": "not a dict"}', encoding="utf-8")
    assert store.load() == {}

    paths.knownUsers.write_text('{"users": {"192.168.1.50": "bad user doc"}}', encoding="utf-8")
    assert store.load() == {}


def test_record_user_persists_atomically_and_roundtrips(tmp_path):
    clock = FakeClock(T0)
    paths = DataPaths(tmp_path)
    store = KnownUsersStore(paths, clock)

    store.recordUser("192.168.1.50", "Alice Smith")
    assert store.getName("192.168.1.50") == "Alice Smith"
    assert store.getName("  192.168.1.50  ") == "Alice Smith"
    assert store.getName("192.168.1.99") is None

    users = store.getKnownUsers()
    assert users == {"192.168.1.50": "Alice Smith"}

    records = store.load()
    rec = records["192.168.1.50"]
    assert rec.clientAddress == "192.168.1.50"
    assert rec.displayName == "Alice Smith"
    assert rec.lastSeenAt == T0


def test_record_user_ignores_empty_or_whitespace_arguments(tmp_path):
    paths = DataPaths(tmp_path)
    store = KnownUsersStore(paths, FakeClock(T0))

    store.recordUser(None, "Bob")
    store.recordUser("192.168.1.51", None)
    store.recordUser("   ", "Bob")
    store.recordUser("192.168.1.51", "   ")
    assert store.load() == {}
    assert store.getName(None) is None


def test_record_user_updates_existing_entry(tmp_path):
    clock = FakeClock(T0)
    paths = DataPaths(tmp_path)
    store = KnownUsersStore(paths, clock)

    store.recordUser("192.168.1.50", "Alice")
    clock.advance(3600)
    store.recordUser("192.168.1.50", "Alice Smith")
    store.recordUser("192.168.1.52", "Bob Jones")

    assert store.getKnownUsers() == {
        "192.168.1.50": "Alice Smith",
        "192.168.1.52": "Bob Jones",
    }
    assert store.load()["192.168.1.50"].lastSeenAt == T0 + timedelta(seconds=3600)
