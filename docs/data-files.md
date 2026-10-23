# PC Handoff data files

The data directory holds three JSON files: `config.json`, `status.json` and
`activity_log.json`. This document describes their formats for people who read them, such
as a team lead reviewing usage. The validators that enforce these rules are in
`src/pc_handoff/persistence/schemas.py` and `config_store.py` (ADR-005). Operating guidance
is in [operations.md](operations.md).

## 1. Rules for every file

**Encoding.**

- Files are written as UTF-8 without a byte-order mark.
- On read, a UTF-8 byte-order mark is tolerated, for example one added by Notepad.
- Bytes that are not valid UTF-8 make the file unparseable.

**`schemaVersion`.** Every file has `"schemaVersion": 1`. No other version exists. A file
with another value, or without the key, is treated as corrupt: it is backed up and replaced.
There is no migration.

**Timestamps.**

- The application writes host-local time with its UTC offset, truncated to whole seconds:
  `2026-10-21T09:15:02+09:00`. Whole seconds keep `durationSeconds` an exact difference.
- On read, any ISO 8601 form that Python's `datetime.fromisoformat` accepts **and** that
  carries an offset is valid. That includes `Z`, a space instead of `T`, the basic format
  `20261021T091502+0900`, and minute precision (Critic TASK-005 S9).
- A timestamp without an offset is invalid.

**Integers are strict.** `true`/`false` and decimals such as `10.0` are not integers.

**UUIDs** are lowercase `8-4-4-4-12` hex, exactly as `str(uuid.uuid4())` writes them.
Pairing compares ids as text, so other spellings are rejected.

**Extra keys.**

- In event objects and at the top level of `activity_log.json`, extra keys are tolerated
  and kept.
- In `status.json` they are tolerated on read but not kept, because the file is rewritten
  from memory.
- In `config.json`, unknown keys are ignored with a warning.

**Atomic writes.** Every write goes to `<name>.tmp-<pid>-<8 hex>` in the same directory and
then replaces the file. A crash never leaves a half-written file. Leftover temp files are
deleted at the next start.

**Backups.**

- A damaged file is renamed to `<name>.corrupt-<yyyyMMddTHHmmss>` before it is replaced.
  The time is host-local, for example `status.json.corrupt-20261021T091502`. When that name
  is already taken in the same second, `-2`, `-3` and so on are appended.
- The bytes are never changed.
- Backups are not read again by the application.
- Which damage leads to a backup is described per file below.

## 2. `config.json`

```json
{
  "schemaVersion": 1,
  "dashboardRefreshIntervalSeconds": 10,
  "sessionPollIntervalSeconds": 5,
  "dashboardPort": 8501
}
```

| Key | Type | Range | Default |
| :--- | :--- | :--- | :--- |
| `schemaVersion` | integer | must be 1 | 1 |
| `dashboardRefreshIntervalSeconds` | integer | 5-300 | 10 |
| `sessionPollIntervalSeconds` | integer | 2-60 | 5 |
| `dashboardPort` | integer | 1024-65535 | 8501 |

- The file is read once at startup; changes need a restart. It is created with the defaults
  when it is missing.
- **Whole-file corrupt** means it cannot be parsed, its root is not an object, or
  `schemaVersion` is missing or not 1. The file is then backed up and replaced with the
  defaults (REQ AC-18).
- **Any other problem is per setting:** missing, wrong type, out of range, or an unknown
  key. The default is used for that setting, a warning is shown and logged, and the file is
  never rewritten (REQ AC-15, AC-16).

## 3. `status.json`

A snapshot of the in-memory state, rewritten after every poll. While the application runs it
is never read back; it is fully validated only at startup. If it fails validation then, it
is backed up and rebuilt, and the dashboard shows "Status data was reset (backup: ...)"
(REQ AC-17).

| Key | Type | Null | Meaning |
| :--- | :--- | :--- | :--- |
| `schemaVersion` | integer | no | Always 1. |
| `status` | string | no | `Available`, `In Use` or `Unknown`. `Unknown` means the last poll could not read the session list. |
| `lastKnownStatus` | string | yes | `Available` or `In Use` (never `Unknown`): the status at the last successful poll. Null before any successful poll. |
| `lastKnownStatusAt` | timestamp | yes | When `lastKnownStatus` was derived. |
| `lastSuccessfulPollAt` | timestamp | yes | The last poll that read the session list (P). It is kept unchanged during an outage. |
| `updatedAt` | timestamp | no | When this snapshot was written. |
| `detectionHealthy` | boolean | no | False while polls fail. |
| `sessions` | array | no | One entry per user session, Active or Disconnected. During an outage, the last known sessions. |

Each `sessions[]` entry:

| Key | Type | Null | Meaning |
| :--- | :--- | :--- | :--- |
| `sessionId` | integer >= 0 | no | The Windows session ID. |
| `userName` | string | no | Not empty. |
| `domain` | string | yes | |
| `state` | string | no | `Active` or `Disconnected`. |
| `connectionType` | string | no | `RDP` or `Console`. |
| `clientName`, `clientAddress` | string | yes | Null when Windows does not report them (REQ AC-9); the dashboard shows `Unknown`. Always null for a console session. |
| `logonTime` | timestamp | yes | Informational; part of the session identity. |
| `firstDetectedAt` | timestamp | yes | When the application first saw this logon. |
| `connectTime` | timestamp | yes | The last connect time Windows reported. |
| `usagePeriodId` | UUID | when Disconnected | The open usage period. Required when Active; null when Disconnected. |
| `usagePeriodStart` | timestamp | when Disconnected | Start of the open period. Required when Active; null when Disconnected. |
| `durationApproximate` | boolean | no | True when the period start is not an OS-reported connect time. |
| `disconnectedSince` | timestamp | when Active | Null when Active. When Disconnected: the OS disconnect time if it falls in the ended period's window, else the detection time (interpretation I2). |

Example (alice in use over RDP, bob logged on but disconnected):

```json
{
  "schemaVersion": 1,
  "status": "In Use",
  "lastKnownStatus": "In Use",
  "lastKnownStatusAt": "2026-10-21T10:00:05+09:00",
  "lastSuccessfulPollAt": "2026-10-21T10:00:05+09:00",
  "updatedAt": "2026-10-21T10:00:05+09:00",
  "detectionHealthy": true,
  "sessions": [
    {
      "sessionId": 2,
      "userName": "alice",
      "domain": "CORP",
      "state": "Active",
      "connectionType": "RDP",
      "clientName": "ALICE-LAPTOP",
      "clientAddress": "192.168.1.20",
      "logonTime": "2026-10-21T08:00:00+09:00",
      "firstDetectedAt": "2026-10-21T09:00:05+09:00",
      "connectTime": "2026-10-21T09:00:02+09:00",
      "usagePeriodId": "1c5d574b-46c1-499b-9dc0-89b99fefa0e9",
      "usagePeriodStart": "2026-10-21T09:00:02+09:00",
      "durationApproximate": false,
      "disconnectedSince": null
    },
    {
      "sessionId": 3,
      "userName": "bob",
      "domain": "CORP",
      "state": "Disconnected",
      "connectionType": "RDP",
      "clientName": "BOB-PC",
      "clientAddress": "192.168.1.21",
      "logonTime": "2026-10-21T07:00:00+09:00",
      "firstDetectedAt": "2026-10-21T09:00:05+09:00",
      "connectTime": "2026-10-21T09:00:03+09:00",
      "usagePeriodId": null,
      "usagePeriodStart": null,
      "durationApproximate": false,
      "disconnectedSince": "2026-10-21T10:00:00+09:00"
    }
  ]
}
```

## 4. `activity_log.json`

The audit trail of **usage periods**.

- A usage period is one contiguous interval during which a session is `Active` over one
  connection.
- It starts when a session becomes Active: at logon, at a reconnect, at a switch between RDP
  and console, or when the session is first seen Active.
- It ends at a disconnect, a logoff, or a detected reconnect.
- Time spent Disconnected is never counted.
- One Windows logon can therefore have many periods.

### Document

```
{"schemaVersion": 1, "events": [ ...one event object per line... ]}
```

### Event fields

| Key | Type | Null | Meaning |
| :--- | :--- | :--- | :--- |
| `eventId` | UUID | no | Unique per event. |
| `eventType` | string | no | `session_start` or `session_end`. |
| `usagePeriodId` | UUID | no | The usage period this event belongs to. |
| `timestamp` | timestamp | no | When the event was recorded (the poll's detection time). |
| `sessionId` | integer >= 0 | no | |
| `userName` | string | no | Not empty. |
| `domain` | string | yes | |
| `logonTime` | timestamp | yes | |
| `clientName`, `clientAddress` | string | yes | Null when not reported, and for console sessions. |
| `connectionType` | string | no | `RDP` or `Console`, the connection of this period. |
| `connectTime` | timestamp | yes | The OS connect time the period started from. Null when not reported, or when treated as not reported (interpretations I1 and I4). |
| `usagePeriodStart` | timestamp | no | Start of the period. It is repeated in `session_end`, so an end is self-contained even when its start is only in a backup file. |
| `durationApproximate` | boolean | no | True when the start or the end is not an exact observation. |
| `reason` | string | no | `observed`, `detected_after_outage` (found after failed polls) or `detected_at_startup` (found when the application started). |

`session_end` events add three fields:

| Key | Type | Null | Meaning |
| :--- | :--- | :--- | :--- |
| `periodEnd` | timestamp | yes | When the period ended. See "End times" below. |
| `durationSeconds` | integer >= 0 | yes | `periodEnd - usagePeriodStart` in whole seconds. |
| `endCause` | string | no | `disconnect`, `logoff`, `reconnect_detected`, or `unknown` (defensive; not produced in practice). |

**End times.**

- **A transition the application saw directly**, Active at one poll and Disconnected or gone
  at the next, ends at the OS disconnect time if Windows reports one inside the period.
  Otherwise it ends at the detection time.
- **A transition it did not see directly** has these cases:
  - an outage;
  - an application restart;
  - a disconnect and reconnect between two polls.

  The OS disconnect time is used if it falls inside the window. Otherwise the end is the
  later of the last successful poll (P) and the period's start-event timestamp. Such ends
  are marked `durationApproximate: true` and are lower bounds (assumption A7).
- `periodEnd` and `durationSeconds` would be null only if neither P nor the start
  timestamp were known. The schema allows it; the application does not produce it.

**Pairing rule.**

- Each `usagePeriodId` has at most one `session_start` and at most one `session_end`. This
  holds across `activity_log.json` and all of its `.corrupt-*` backups.
- A `session_end` never has an earlier `timestamp` than its `session_start`. The
  application clamps its own ends to guarantee this, even across a backward clock step
  (ADR-007 R11).
- An end whose start is not in the file is valid; the start can be in a backup (REQ AC-18).

**Canonical layout (ADR-003).** The application always writes the file in one fixed layout,
so that appending is a cheap byte operation:

- `schemaVersion` comes first and `events` comes last. Any extra top-level keys sit between
  them, in their original order.
- There is one event per line, written as compact JSON with `", "` and `": "` separators
  and non-ASCII characters kept as is.
- Events are separated by `,` and a line break.
- The file ends with a line break, `]}` and a line break. An empty log is exactly
  `{"schemaVersion": 1, "events": []}` followed by a line break.

Example:

```
{"schemaVersion": 1, "events": [
{"eventId": "2d18581b-7dfd-4c72-a4e8-7e503801280a", "eventType": "session_start", "usagePeriodId": "1c5d574b-46c1-499b-9dc0-89b99fefa0e9", "timestamp": "2026-10-21T09:00:05+09:00", "sessionId": 2, "userName": "alice", "domain": "CORP", "logonTime": "2026-10-21T08:00:00+09:00", "clientName": "ALICE-LAPTOP", "clientAddress": "192.168.1.20", "connectionType": "RDP", "connectTime": "2026-10-21T09:00:02+09:00", "usagePeriodStart": "2026-10-21T09:00:02+09:00", "durationApproximate": false, "reason": "observed"},
{"eventId": "2ee99c78-f730-402d-9920-53d56338f23b", "eventType": "session_start", "usagePeriodId": "bffbb2ec-02aa-412a-8f39-9ddee430c797", "timestamp": "2026-10-21T09:00:05+09:00", "sessionId": 3, "userName": "bob", "domain": "CORP", "logonTime": "2026-10-21T07:00:00+09:00", "clientName": "BOB-PC", "clientAddress": "192.168.1.21", "connectionType": "RDP", "connectTime": "2026-10-21T09:00:03+09:00", "usagePeriodStart": "2026-10-21T09:00:03+09:00", "durationApproximate": false, "reason": "observed"},
{"eventId": "4ea1350b-09c2-471c-af2e-f1bd926df049", "eventType": "session_end", "usagePeriodId": "bffbb2ec-02aa-412a-8f39-9ddee430c797", "timestamp": "2026-10-21T10:00:05+09:00", "sessionId": 3, "userName": "bob", "domain": "CORP", "logonTime": "2026-10-21T07:00:00+09:00", "clientName": "BOB-PC", "clientAddress": "192.168.1.21", "connectionType": "RDP", "connectTime": "2026-10-21T09:00:03+09:00", "usagePeriodStart": "2026-10-21T09:00:03+09:00", "durationApproximate": false, "reason": "observed", "periodEnd": "2026-10-21T10:00:00+09:00", "durationSeconds": 3597, "endCause": "disconnect"}
]}
```

**Startup normalization.** A file that is valid but not in the canonical layout is rewritten
into it at startup, without a backup. Examples are a file re-indented by an editor, one with
a byte-order mark, or one with a top-level key after `events`. The line
`activity_log.json normalized to canonical layout` is logged.

**"Existing events are never modified" refers to event values, not bytes** (ADR-003; Critic
iteration-2 MINOR-3).

- Normalization, and the slow path after an external edit, re-serialize existing events.
- That can change the bytes: whitespace, the position of top-level keys, a dropped
  byte-order mark, re-encoded escapes: a JSON escape such as backslash-`u00e9` (six ASCII characters
  in the file) becomes the character `é` itself (UTF-8).
- It never changes an event's values, keys or order, and never removes an event.
- Appends on the normal path never touch the bytes of existing events.

**Checks.**

- **At startup, the whole file is validated:**
  - every event's fields, types and allowed values (`session_end` needs its three extra
    fields);
  - the pairing rule inside the file (a duplicate start or end, or an end earlier than its
    start).

  A failure means the file is corrupt. It is backed up as
  `activity_log.json.corrupt-<timestamp>` and a fresh file is started. Open usage periods
  are then rebuilt from `status.json` (REQ AC-18).
- **While the application runs, only the outer structure is checked** before each write: an
  object with `schemaVersion` 1 and an `events` array.
  - If that fails, the file is backed up and a fresh file is started with the new events.
  - If the file was deleted, it is recreated without a backup.
  - Damaged individual events that pass this check are caught at the next start.

**Not checked by design** (Critic TASK-005 S10; decisions.log TASK-005 open items). These
pass validation:

- a `periodEnd` earlier than `usagePeriodStart`;
- a `durationSeconds` that does not match `periodEnd - usagePeriodStart`;
- a `durationSeconds` that is null while `periodEnd` is not, or the reverse;
- the same `eventId` used by events of different periods.

The application never writes them. Rejecting them would reset the whole log, and with it
the open periods, over fields that reconciliation never reads.

## 5. `pc-handoff.lock`

The single-instance lock file (ADR-004). Byte 0 is locked by the running instance and never
written. From offset 1 it holds one diagnostic line about the holder:
`{"pid": 42804, "startedAt": "2026-10-23T13:15:00+09:00"}`. The file is never deleted. How
to read it while the application runs is in [operations.md](operations.md) section 7.
