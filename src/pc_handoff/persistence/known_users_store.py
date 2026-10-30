"""Atomic persistent store for IP-to-display-name mappings (REQ-004, TASK-026, DEC-016)."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pc_handoff.clock import Clock, toIso
from pc_handoff.domain.models import parseIsoStrict
from pc_handoff.persistence.atomic import atomicWriteBytes, readBytes
from pc_handoff.persistence.paths import DataPaths

KNOWN_USERS_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class KnownUserRecord:
    """One recognized team member and their observed client IP address."""

    clientAddress: str
    displayName: str
    lastSeenAt: datetime

    def toJson(self) -> dict[str, str]:
        return {
            "displayName": self.displayName,
            "lastSeenAt": toIso(self.lastSeenAt),
        }

    @classmethod
    def fromJson(cls, clientAddress: str, doc: dict[str, Any]) -> "KnownUserRecord":
        return cls(
            clientAddress=clientAddress.strip(),
            displayName=str(doc.get("displayName", "")).strip(),
            lastSeenAt=parseIsoStrict(str(doc.get("lastSeenAt"))),
        )


class KnownUsersStore:
    """Thread-safe, atomic persistence for client IP and real user name mappings."""

    def __init__(self, paths: DataPaths, clock: Clock) -> None:
        self.paths = paths
        self.clock = clock

    def load(self) -> dict[str, KnownUserRecord]:
        """Load known users from ``known_users.json``. Returns empty mapping on missing/corrupt."""
        try:
            raw = readBytes(self.paths.knownUsers)
        except OSError:
            return {}

        if raw is None:
            return {}

        try:
            doc = json.loads(raw.decode("utf-8"))
            if not isinstance(doc, dict):
                return {}
            users = doc.get("users", {})
            if not isinstance(users, dict):
                return {}
            records: dict[str, KnownUserRecord] = {}
            for ip, userDoc in users.items():
                if isinstance(userDoc, dict) and isinstance(ip, str) and ip.strip():
                    try:
                        record = KnownUserRecord.fromJson(ip, userDoc)
                        records[record.clientAddress] = record
                    except (ValueError, KeyError):
                        continue
            return records
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def recordUser(self, clientAddress: str | None, displayName: str | None) -> None:
        """Associate ``clientAddress`` with ``displayName`` and update ``lastSeenAt`` atomically."""
        if not clientAddress or not displayName:
            return
        cleanIp = clientAddress.strip()
        cleanName = displayName.strip()
        if not cleanIp or not cleanName:
            return

        current = self.load()
        now = self.clock.now()
        current[cleanIp] = KnownUserRecord(
            clientAddress=cleanIp,
            displayName=cleanName,
            lastSeenAt=now,
        )

        doc = {
            "schemaVersion": KNOWN_USERS_SCHEMA_VERSION,
            "users": {ip: rec.toJson() for ip, rec in sorted(current.items())},
        }
        encoded = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
        atomicWriteBytes(self.paths.knownUsers, encoded)

    def getName(self, clientAddress: str | None) -> str | None:
        """Return the known display name for ``clientAddress``, or None if unknown."""
        if not clientAddress:
            return None
        record = self.load().get(clientAddress.strip())
        return record.displayName if record else None

    def getKnownUsers(self) -> dict[str, str]:
        """Return a mapping of ``clientAddress -> displayName``."""
        return {ip: record.displayName for ip, record in self.load().items()}
