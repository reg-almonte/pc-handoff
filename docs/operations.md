# PC Handoff operations guide

PC Handoff runs on the shared Windows PC as one Python process. It does three things:

- It polls the Windows session list and correlates active sessions with the reservation queue.
- It derives the PC status: `Available`, `In Use`, or `Unknown` when detection fails.
- It writes four local JSON files (`config.json`, `status.json`, `activity_log.json`, `reservations.json`) and serves a status, reservation and handoff dashboard to the LAN.

This guide is for the person who installs, runs and looks after it. The file formats are in
[data-files.md](data-files.md). The checks that must be done by hand on the shared PC are in
[manual-acceptance.md](manual-acceptance.md).

Design decisions are cited as ADR-00n. These are the architecture decision records in the
factory repository under `.sdlc/specs/REQ-001-pc-status-monitoring/adrs/`. Entries in that
repository's `.sdlc/docs/decisions.log` are cited as "decisions.log".

## 1. Requirements

- **Operating system:** Windows 10 or 11 (Pro or Enterprise), or Windows Server, with Remote
  Desktop enabled. Session information comes from the Windows Terminal Services API (ADR-001).
- **Python:** 3.11 or later. The shared PC is expected to have 3.14.6 (assumption A12). The
  launcher checks the version and exits with code 5 on an older interpreter.
- **Account rights:** the account must be able to query **all** sessions and their client
  information (assumption A3). Without these rights, client fields show `Unknown` (REQ AC-9),
  or the status shows `Unknown` (REQ AC-11). The process does not crash.
- **A local disk for the data directory.** The single-instance lock is an OS byte-range lock,
  and those are unreliable on network or redirected drives (ADR-004, assumption A9).
- **A free TCP port for the dashboard,** with an inbound firewall rule for the LAN
  (assumption A4). See section 5.

## 2. Install

Run these from the `external/` directory of a checkout:

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .
```

The only runtime dependency is Streamlit (`streamlit>=1.40,<2`). Developers install
`-e .[dev]` instead; this adds pytest, pytest-cov and ruff.

Use an editable install (`-e`), or always pass `--data-dir`. Without `--data-dir`, the default
data directory is computed from the package location. That location is `external/data/` only
for a source or editable install (ADR-006).

## 3. Run and stop

Always start the application with the launcher:

```powershell
.venv\Scripts\python -m pc_handoff --data-dir C:\pc-handoff\data
```

Do **not** use `streamlit run`. The launcher is what does the following, in order:

1. takes the single-instance lock;
2. starts `app.log`;
3. reads `config.json` once;
4. checks the dashboard port;
5. loads the data files;
6. starts the single background poller;
7. starts the dashboard server.

A plain `streamlit run` skips all of that. The page then shows only "Start PC Handoff with:
python -m pc_handoff" (ADR-002).

**The data directory** is chosen in this order:

1. `--data-dir <path>`
2. the environment variable `PC_HANDOFF_DATA_DIR`
3. `<external>/data`

It is created if it is missing.

**Run it as a boot-time background process** (assumption A3). Installation automation is out
of scope for REQ-001. One way is a scheduled task that starts at boot, runs whether or not
anyone is signed in, and uses an account allowed to query all sessions:

```powershell
schtasks /Create /TN "PC Handoff" /SC ONSTART /RU SYSTEM /TR "\"C:\pc-handoff\external\.venv\Scripts\python.exe\" -m pc_handoff --data-dir C:\pc-handoff\data"
```

The manual acceptance check A3 confirms that the chosen setup survives a user sign-out and
still sees every session with its client information.

**To stop it,** do one of these:

- press Ctrl+C in its console;
- end the scheduled task.

Ending the task kills the process. That is safe:

- every write is atomic (temporary file, then replace);
- the lock is released by the OS;
- the next start reconciles what happened while the app was down (REQ AC-19, AC-20).

Events still queued in memory when the process dies are lost. A lost `session_end` is
closed at the next start; a lost `session_start` is harmless to pairing, but that
period is simply not logged (ADR-003; known limitation L20).

The launcher also has two test-only options: `--check-lock-only` and `--no-server`. Do not
use them in production.

### Exit codes

| Code | Meaning | What to do |
| :--- | :--- | :--- |
| 0 | Stopped normally. | - |
| 1 | Startup error or dashboard server error. This includes a port that was taken after the startup check (see section 5). | Read the last ERROR lines of `app.log` and the console output. |
| 3 | Another PC Handoff instance already holds the lock on this data directory. | Nothing; one instance is already running. Its data files were not touched. |
| 4 | The lock file or the data directory is unavailable: it cannot be created or opened (for example, a permission problem or a path that is a file). | Fix the path or the permissions of the data directory. |
| 5 | The Python version is older than 3.11. | Install Python 3.11 or later. |
| 6 | The dashboard port is already in use. The application stopped after reading `config.json` (the port comes from it, so `config.json` is created with the defaults if it was missing, or backed up and reset if it was corrupt; see L18) and before `status.json` and `activity_log.json` were read, backed up or created. No poll ran. `app.log` gets, among others, the INFO lines `PC Handoff starting` and `PC Handoff stopped`, one ERROR line `dashboard port in use`, and when applicable `config.json was missing and has been created with defaults` or `config.json backed up as config.json.corrupt-...`. | Configure a free `dashboardPort` (section 4), or stop the program that holds the port. |

## 4. Configuration (`config.json`)

`config.json` lives in the data directory and is created with the defaults on first run.

| Setting | Type | Range | Default | When it takes effect |
| :--- | :--- | :--- | :--- | :--- |
| `dashboardRefreshIntervalSeconds` | integer | 5-300 | 10 | Restart required |
| `sessionPollIntervalSeconds` | integer | 2-60 | 5 | Restart required |
| `dashboardPort` | integer | 1024-65535 | 8501 | Restart required |
| `enableHostNotifications` | boolean (optional) | `true` or `false` | `true` | Restart required |
| `hostNotificationMethod` | string (optional) | `"toast"`, `"msg"`, `"none"` | `"toast"` | Restart required |
| `hostNotificationTimeoutSeconds` | integer (optional) | 3-60 | 10 | Restart required |
| `schemaVersion` | integer | must be 1 | 1 | - |

**Read once at startup.** The file is read once, at startup. Editing it while the
application runs changes nothing until the next restart (REQ AC-14). There is no live
reload.

**A setting that is missing, of the wrong type or out of range** uses its default.
Booleans and decimals such as `10.0` count as the wrong type. Unknown keys are ignored. Each
case is shown as a warning on the dashboard, for example "Configuration (config.json):
dashboardPort: 80 out of range 1024-65535; default 8501 applied", and is written to `app.log`.
The file itself is left untouched (REQ AC-15).

**A whole-file problem** means the file cannot be parsed, its root is not a JSON object, or
`schemaVersion` is missing or not 1. In that case the file is renamed to
`config.json.corrupt-<yyyyMMddTHHmmss>` and replaced with the defaults. The dashboard shows
a notice naming the backup (REQ AC-18).

**How long a change takes to show:** an open dashboard shows a session change within
`sessionPollIntervalSeconds` + `dashboardRefreshIntervalSeconds` + 2 seconds. With the
defaults that is 17 s.

## 5. Dashboard, port and network

The dashboard is at `http://<pc-hostname-or-ip>:<dashboardPort>/`. It has no login (REQ
AC-13). Viewers get Streamlit's viewer toolbar (`client.toolbarMode = "viewer"`): there is
no Deploy button and there are no developer options (ADR-002 amendment 2026-10-23).

**Firewall (A4).** Allow inbound TCP on `dashboardPort` for the LAN profile. For example, in
an elevated PowerShell:

```powershell
New-NetFirewallRule -DisplayName "PC Handoff dashboard" -Direction Inbound -Protocol TCP -LocalPort 8501 -Profile Domain,Private -Action Allow
```

Firewall rule creation is not automated (REQ-001 Out of Scope).

**Port in use (exit code 6).**

- Port 8501 is Streamlit's own default, so it is often taken by another Streamlit app. On the
  development PC, for example, another project's Streamlit holds it.
- Before you start, check the port:

  ```powershell
  Get-NetTCPConnection -LocalPort 8501 -State Listen
  ```

- If anything is listed, set a free `dashboardPort` in `config.json` and restart.
- The startup check detects every kind of listener: IPv4 `0.0.0.0` and `127.0.0.1`,
  IPv6-only `[::]`, dual-stack `[::]`, and `[::1]` (ADR-002 amendment).
- **Residual case.** Our server listens on IPv4 only. A dual-stack program started *after*
  PC Handoff can still take the IPv6 side of the same port, because Windows allows it next
  to our IPv4 socket and Streamlit gives no way to stop it. From then on, IPv6 and
  `localhost` clients may reach the other program while IPv4 clients reach PC Handoff. If
  that happens, give one of the two programs a different port (known limitation L19).
- **Corrupt config.json together with a taken port.** A corrupt `config.json` is reset to
  the defaults before the port is known. If the default port is also taken, the start fails
  with exit 6 and the one-time config-reset notice is used up. The backup file and the
  `app.log` WARNING remain (decisions.log, user decision 2026-10-22/23; known limitation
  L18).

**Network behaviour.**

- The server binds IPv4 `0.0.0.0` only (`server.address = "0.0.0.0"`). Browse by an IPv4
  address, or by a host name that resolves to one.
- The application makes no outbound connections in normal use:
  - Usage statistics are off (`browser.gatherUsageStats = false`).
  - Streamlit's startup welcome banner is off (`logger.hideWelcomeMessage = true`). With the
    banner on, Streamlit looks up the external IP (an HTTP request to
    `checkip.amazonaws.com`) at every start (ADR-002 amendment 2026-10-22).
- **The only remaining outbound path** is Streamlit's cross-origin WebSocket check. A
  WebSocket handshake whose `Origin` is not localhost, the server address or the internal IP
  makes Streamlit look up the external IP. Normal same-origin LAN browsing never does this.
  `server.enableCORS` stays on by design.

**CSV export (N2b).** The session tables have Streamlit's CSV download in their toolbar.
Streamlit 1.66 quotes only cells that contain a comma, a double quote or a line break; it
does **not** neutralise cells that start with `=`, `+`, `-` or `@`. This was checked in its
bundled `DataFrame` export code. An RDP client chooses its own client name, so a crafted
name could become a spreadsheet formula when the CSV is opened in Excel. To stay safe:

- do not open exported CSV files with formulas enabled from an untrusted source;
- import them as text instead (Excel: Data > From Text/CSV).

The dashboard itself always shows such names as plain text. Recorded as known limitation
L10 below.

## 6. Data directory

| Path | What it is |
| :--- | :--- |
| `config.json` | Settings (section 4). |
| `status.json` | The current status snapshot, rewritten from memory after every poll. |
| `activity_log.json` | The audit log of usage periods, reservations, handoffs and status notes. It only grows. |
| `reservations.json` | The current reservation queue, handoff state and active broadcast status notes, rewritten atomically on state changes. |
| `logs/app.log` | The application log (section 8). |
| `pc-handoff.lock` | The single-instance lock file (section 7). Never deleted. |
| `<file>.corrupt-<yyyyMMddTHHmmss>[-n]` | A damaged file, kept with its bytes unchanged before it was replaced. The time is host-local. `-2`, `-3` and so on are added when two backups fall in the same second. |
| `<file>.tmp-<pid>-<8 hex>` | A temporary file of an atomic write. Leftovers from a crash are deleted at the next start. |

**Damaged files** are always backed up before they are replaced; nothing is silently
overwritten. Each backup is reported once on the dashboard (REQ AC-17, AC-18). Backups are
never read again and never deleted; remove old ones by hand if you want to.

**`activity_log.json` at runtime.** If it is damaged or deleted while the application runs,
the next write backs it up (when damaged) and starts a fresh file. A notice is shown.

**`status.json` at runtime** is a snapshot written from memory. It is overwritten without
being read.

Formats and examples are in [data-files.md](data-files.md).

**Keep the data directory local** (section 1), and back it up as a whole with the
application stopped if you need a copy.

**File locking and antivirus** (ADR-003; Critic MINOR-9).

- Do not keep `activity_log.json` or `status.json` open in a program that locks files, such
  as Excel or some editors. Copy the file first and open the copy.
- While another program holds the file, the atomic replace fails. It is tried up to 6
  times, 20 ms apart. If it still fails:
  - Activity events stay queued in memory and are retried at every poll. Each
    `poll completed` line shows them as `pendingEvents` (section 8).
  - A `status.json` that cannot be written is logged at ERROR; the in-memory state stays
    authoritative and the next poll writes it again.
- Nothing is lost while the process keeps running.
- If the measured p95 poll time on the shared PC is marginal (see the `poll completed`
  review in [manual-acceptance.md](manual-acceptance.md)), add a Microsoft Defender
  exclusion for the data directory, in an elevated PowerShell:

  ```powershell
  Add-MpPreference -ExclusionPath "C:\pc-handoff\data"
  ```

## 7. Single-instance lock

The application holds an exclusive OS byte-range lock on byte 0 of
`<dataDir>\pc-handoff.lock` for as long as it runs (ADR-004).

**A second instance** on the same data directory:

- writes one error line, `another PC Handoff instance holds ...\pc-handoff.lock`, to the
  console and to `app.log`;
- exits with code 3 within 2 seconds of the launcher starting (Python's own start-up,
  usually well under a second, comes on top; tested end to end inside the second
  process);
- changes no data file.

**A crash or kill** never leaves a stale lock. The OS releases the lock when the process
dies, so the next start succeeds without any cleanup. The lock file itself stays; that is
by design. This is tested end to end with real processes.

**While the application runs, the lock file cannot be opened by ordinary tools.** `type`,
`Get-Content` and Notepad fail with "another process has locked a portion of the file",
because byte 0 is locked. The diagnostic line `{"pid": ..., "startedAt": ...}` starts at
offset 1. To read it while the application runs:

```powershell
$fs = [IO.File]::Open("C:\pc-handoff\data\pc-handoff.lock", 'Open', 'Read', 'ReadWrite')
[void]$fs.Seek(1, 'Begin'); (New-Object IO.StreamReader($fs)).ReadToEnd(); $fs.Close()
```

After the application stops, the file can be read normally.

**Two instances pointed at different data directories** both run, and would audit the same
PC twice. Run exactly one instance (known limitation L13).

## 8. Application log (`logs/app.log`)

`app.log` is separate from the audit log. Each line is one JSON object (requirement
section 8). Fields:

- `timestamp`: ISO 8601 with offset, when the line was written;
- `level`: `DEBUG`, `INFO`, `WARNING` or `ERROR`;
- `traceId`: 32 hex characters. There is one per poll cycle, shared by every line of that
  poll, and one per part of a dashboard request. It is `null` outside both;
- `message`;
- `detail`, when there is one: an object or a text, including tracebacks.

```json
{"timestamp": "2026-10-21T09:00:05+09:00", "level": "INFO", "traceId": "4f1c...", "message": "poll completed", "detail": {"durationMs": 12, "outcome": "ok", "eventsInBatch": 1, "pendingEvents": 0}}
```

The file is not rotated (REQ-001 Out of Scope). To archive it, move it away while the
application is stopped.

**Reading `poll completed`** (one INFO line per poll; Critic MINOR-9):

| Field | Meaning |
| :--- | :--- |
| `durationMs` | How long the poll cycle took: enumeration, tracking, the activity-log write and the `status.json` write. The budget is p95 <= 1000 ms (REQ AC-21). |
| `outcome` | `ok`: the session list was read. `enumeration_failed`: the session list could not be read (error, access denied, or no answer within 3 s), so the status is Unknown. `error`: an unexpected exception inside the poll; an ERROR line with the same traceId has the details. |
| `eventsInBatch` | Activity events produced by this poll. |
| `pendingEvents` | Events still queued in memory after this poll. It must be 0. A value that stays above 0 means `activity_log.json` cannot be written (section 6). |

The p95 of `durationMs` over the whole log (Windows PowerShell 5.1 or later):

```powershell
$ms = Get-Content C:\pc-handoff\data\logs\app.log | ForEach-Object { $_ | ConvertFrom-Json } |
  Where-Object { $_.message -eq 'poll completed' } | ForEach-Object { [int]$_.detail.durationMs } | Sort-Object
"polls: $($ms.Count)  p95: $($ms[[math]::Ceiling($ms.Count * 0.95) - 1]) ms"
```

**Other lines worth knowing** (follow the traceId to see the rest of a poll):

| Level | Message (start) | Meaning |
| :--- | :--- | :--- |
| WARNING | `session enumeration failed` | The poll could not read the session list. `detail` has `kind` (`error`, `access_denied`, `timeout`), `message` and `winerror`. |
| INFO | `poll skipped: previous enumeration still running` / `poll skipped: previous poll still running` | A tick came due while an earlier enumeration or poll was still busy. Polls never overlap. |
| ERROR | `poll failed with an unexpected error` | A bug or an environment fault inside a poll. The poller keeps running. |
| WARNING | `session <id> (<user>) has reported transitional state ...` | A session has stayed in a transitional Windows state for 12 polls; this repeats every 12 polls (L6). |
| WARNING | `wall clock moved backwards by <n> s; end clamped ...` | A backward clock step was absorbed (L4). |
| WARNING | `connect time ... treated as not reported (I4)` | Windows reported a connect time older than the session's previous period; the new period starts at the detection time and is approximate (L5). |
| INFO | `identity matched without logonTime for session <id>` | The identity fallback was used (L7). |
| WARNING | `<file> backed up as <backup>` | A damaged file was backed up. |
| WARNING | `open usage periods in backups cannot be closed (accepted limitation)` | Both data files were unusable at startup (L12). |
| ERROR | `dashboard port in use` | Startup refused, exit 6. |
| ERROR | `dashboard server failed` | The dashboard server stopped with an error, exit 1. |
| WARNING | `test session provider active` | The test hooks are set. This must never appear in production (section 9). |
| ERROR | `dashboard render failed` | A dashboard rendering error. Viewers see "Dashboard error; see app.log trace <id>" with the same traceId. |

## 9. Test hooks: never in production

Two environment variables exist only for the automated tests: `PC_HANDOFF_ALLOW_TEST_HOOKS`
and `PC_HANDOFF_TEST_SESSIONS_FILE`. **Never set them on the shared PC.** When both are set,
the application stops reading sessions from Windows. It reads them from the named JSON file
instead, so the dashboard and the audit log show test data. A WARNING `test session
provider active` naming the file is logged at startup.

## 10. Sessions without a user name

A session that reports no user name, such as the logon screen, the services session 0 or the
RDP listener, is never shown and never makes the PC In Use (REQ AC-2).

If a tracked session's user name becomes empty, its usage period ends with `endCause:
"logoff"`. If the user then reappears on that session, a new period starts (ADR-007 R1). The
manual console sign-out check records which of the two things Windows does on the shared PC:
remove the session, or keep it for a logon screen with an empty user. Both count as gone.

## 11. Known limitations

Each limitation lists the decision that accepted it.

**L1. Extra copy of events after a rare double fault.**

- When it happens: an exception right after the activity log's atomic replace, then
  external damage to the file before the next write.
- Result: the next write backs the file up and starts a fresh one. The same events can then
  exist both in the new file and in its `.corrupt-*` backup.
- Nothing reads backups, and dropping the events would be worse (TASK-006 accepted
  limitation 11; decisions.log TASK-006 entry). A cross-file pairing check would report
  such a period twice (Critic TASK-008 S5).

**L2. Small overlap with several open periods on one session.**

- When it happens: a crash between the activity-log write and the `status.json` write, and
  at the next start several open periods on one session ID. The application cannot produce
  that state itself.
- Result: the older periods are closed with an end that may overlap the next period
  slightly. The pairing rule still holds (Critic TASK-008 S4; decisions.log TASK-008
  interpretation 4).

**L3. Healthy status.json for one poll after an internal error.**

- When it happens: an unexpected error late in a poll.
- Result: `status.json` can keep the previous poll's content, which may say healthy, while
  the dashboard already shows Unknown. The next poll corrects it, so this lasts at most one
  poll interval (Critic TASK-009 S6).

**L4. Wall-clock steps.**

- **Backward step:** an end that would come before its start is clamped, so the log always
  stays valid. A WARNING `wall clock moved backwards` is logged, and durations come out too
  short (ADR-007 R11).
- **Forward step:** not detected. The durations of periods that span it come out too long
  (ADR-007 R11).
- Steps can come from a time-sync correction, a manual change, a VM resume or a correction
  after a reboot.

**L5. Interpretations of the requirement.** Four rules go beyond the literal requirement
text (ADR-007; decisions.log I1 to I4). In plain language:

- **I1 (ADR-007 R4).** A session that switches between RDP and console while Windows
  reports the same connect time, or none, counts as a reconnect. The new period starts at
  the moment the switch was detected and is marked approximate, so no time is counted twice.
- **I2 (ADR-007 R8).** "Disconnected since" uses Windows' disconnect time only if it falls
  inside the period that just ended. Otherwise it uses the detection time. That is why it
  always equals the end of that period.
- **I3 (TASK-008).** After a restart, an open period continues only if the session also
  still has the same connection type (RDP or console). Otherwise it is closed and a new one
  starts.
- **I4 (decisions.log TASK-004 M1, N1, N5).** A reported connect time that is earlier than
  the end of the session's previous period counts as not reported. The new period starts at
  the detection time and is approximate. A WARNING `... (I4)` is logged.

**L6. Transitional states are never forced (ADR-007 R3; open question Q2).**

- A known session that Windows keeps reporting in a transitional state (`Connected`,
  `ConnectQuery`, `Shadow`, `Idle`, `Init`, `Reset`, `Down`) keeps its previous state.
- After 12 consecutive polls a WARNING is logged, repeated every 12 polls. The dashboard
  does not show it.
- A session stuck that way keeps its last status.

**L7. Identity-fallback blind spot (ADR-007 R10).** This applies only when Windows gives no
logon time for a session:

- a sign-out followed by a sign-in of the same user on the same session ID while PC Handoff
  was not running cannot be told apart;
- the old period is then wrongly continued;
- the INFO line `identity matched without logonTime` marks the case.

**L8. Durations of unseen transitions are lower bounds (A7).** A change that happened during
an outage or while the application was down is closed at the last moment the session was
known to be active, unless Windows reports an acceptable disconnect time. These periods are
marked `durationApproximate: true` and may be too short, never too long.

**L9. Some text is displayed differently (decisions.log TASK-010 N1).** The dashboard
removes invisible control and format characters from user and client names, so that a
crafted client name cannot pose as another user or address. Cosmetic side effects, on the
display only (stored data is unchanged):

- an emoji sequence joined with ZWJ shows as its separate emoji;
- a ZWNJ inside a Persian or Indic word is dropped, so the letters join;
- subdivision flags show as a plain black flag.

**L10. CSV formulas (Critic TASK-010 N2b).** A CSV exported from the dashboard tables can
contain a client name that a spreadsheet treats as a formula (section 5). Checked against
Streamlit 1.66: it does not neutralise such cells.

**L11. Off-Windows behaviour not verified (Critic TASK-007 S6; Critic TASK-009 N3, N4).**

- The target is Windows. The non-Windows lock branch (`fcntl.flock`) and the port check's
  POSIX behaviour have never been run on a real non-Windows machine.
- On POSIX a port in TIME_WAIT could give a false exit 6.
- Some launcher tests use Windows-only error constants.

**L12. Periods orphaned in backups (requirement section 6, source 3).** If `status.json` and
`activity_log.json` are both unusable at startup, periods that were open in their backups
can never be closed. A WARNING is logged. Repairing them is out of scope.

**L13. Two instances on different data directories (ADR-004).** The lock is per data
directory. Two instances pointed at different directories both run and audit the PC twice.

**L14. One process for detection and the dashboard (ADR-002).** A crash of the Python
interpreter itself (not an exception) stops both. Restart recovery is covered by startup
reconciliation and the lock design.

**L15. The activity log grows without retention (ADR-003; REQ-001 Out of Scope).**

- Every event write rewrites the whole file atomically.
- At 10,000 events (about 6 MB) a poll stays well within the 1 s budget.
- At about 100,000 events (about 60 MB) each event write takes roughly 0.3 to 0.8 s.
- `app.log` is not rotated either.
- A later retention REQ must address both.

**L16. Notices are dismissed per browser session (ADR-008).**

- A dismissed reset notice reappears in a new browser tab until the application restarts.
- At most the 20 most recent notices are kept.
- While the status is Unknown, the "Logged on but disconnected" list keeps its normal
  heading, although it is last-known data too (Critic TASK-010 N4).

**L17. Disconnected RDP sessions may show as Console (Critic TASK-002 S2; pending check).**
Windows may report protocol type 0 ("console") for an RDP session with no client attached.
A Disconnected RDP session could then be listed with connection type Console. This is to be
recorded in the manual RDP check.

**L18. A corrupt config.json together with a taken port uses up the reset notice
(decisions.log, user decision 2026-10-22/23; ADR-002 amendment).**

- `config.json` is reset to the defaults before the port is known.
- If the port is then found taken, the start fails with exit 6, and the next successful
  start no longer shows the config-reset notice.
- The `config.json.corrupt-*` backup and the `app.log` WARNING remain (section 5).

**L19. A dual-stack program started later can share the port (ADR-002 amendment
2026-10-23; decisions.log TASK-009 residual port case).**

- The startup check only sees programs that are already running.
- A dual-stack program started after PC Handoff can bind the IPv6 side of the same port.
- IPv6 and `localhost` clients then reach that program (section 5).

**L20. Queued events are lost if the process dies (ADR-003).** Events waiting for a blocked
`activity_log.json` (`pendingEvents` > 0) live in memory only.

- A lost `session_end` is closed at the next start with `detected_at_startup`.
- A lost `session_start` is harmless to pairing, but that period is simply not logged.
