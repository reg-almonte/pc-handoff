# PC Handoff manual acceptance checklist (shared PC)

The automated suite proves 26 of the 27 REQ-001 acceptance criteria end to end. It is in
`tests/acceptance/`; the traceability meta-test is `tests/acceptance/test_ac_traceability.py`.
This checklist covers what automation cannot:

- **REQ AC-13**, reaching the dashboard from another LAN machine;
- **the p95 timing of REQ AC-21** on the real hardware;
- the deployment assumptions **A3, A4, A9, A10, A11 and A12**;
- the behaviour of **real** RDP and console sessions, which the tests can only fake.

**Run it on the shared PC, not on a development PC.** The development PC (PC545) is not the
shared PC, so its findings do not count here. Run it once at deployment, and again after a
Windows feature update or a Python or Streamlit upgrade.

How to use it:

- Each check lists what it validates, the steps, and the expected result.
- Write the observed values in the check's **Record** lines.
- Mark the check **PASS** or **FAIL** in the results table at the end.
- A FAIL or an unexpected observation must be reported back to the team, with the
  `app.log` lines around it (same traceId).

Conventions used below:

- `<ext>` is the `external/` directory on the shared PC, for example `C:\pc-handoff\external`.
- `<data>` is the data directory, for example `C:\pc-handoff\data`.
- `<port>` is the configured `dashboardPort`.
- "the raw session dump" means this command. It lists what Windows reports (protocol type,
  client address, connect and disconnect times) and is read-only:

  ```powershell
  <ext>\.venv\Scripts\python -c "from pc_handoff.detection.wts_provider import createDefaultProvider; [print(s) for s in createDefaultProvider().enumerateSessions()]"
  ```

## Preparation

1. Install as described in [operations.md](operations.md) sections 2 and 3, with
   `--data-dir <data>` and a scheduled task, or whatever setup will be used in production.
2. Check that neither `PC_HANDOFF_ALLOW_TEST_HOOKS` nor `PC_HANDOFF_TEST_SESSIONS_FILE` is
   set:

   ```powershell
   Get-ChildItem Env:PC_HANDOFF*
   ```

   Only `PC_HANDOFF_DATA_DIR` may appear. Also check that `app.log` never contains
   `test session provider active`.
3. Have ready:
   - two other LAN machines with an RDP client, for testers "alice" and "bob";
   - one user account that can sign in at the physical console ("erin").

---

## M1. Python version (A12)

- **Validates:** A12; the launcher's exit-5 guard.
- **Steps:**
  1. Run `<ext>\.venv\Scripts\python --version`.
  2. Start the application once by hand and check that it does not exit with code 5.
- **Expected:** Python 3.11 or later. 3.14.6 is expected.
- **Record:** version: ________

## M2. Data directory on a local disk (A9)

- **Validates:** A9 (the ADR-004 lock needs a local disk).
- **Steps:**
  1. Find the drive of `<data>`.
  2. Run `Get-Volume -DriveLetter <letter>` (or `fsutil fsinfo drivetype <letter>:`).
- **Expected:**
  - The drive is fixed or local.
  - It is not a network or redirected drive, and not a mapped share.
  - `<data>` is not inside a OneDrive-synced folder.
- **Record:** drive / type: ________

## M3. Firewall rule (A4)

- **Validates:** A4.
- **Steps:**
  1. Create the rule from [operations.md](operations.md) section 5 for `<port>`.
  2. Check it with:

     ```powershell
     Get-NetFirewallRule -DisplayName "PC Handoff dashboard" | Get-NetFirewallPortFilter
     ```

- **Expected:** inbound TCP `<port>` is allowed for the profile of the LAN the testers use
  (Domain or Private).
- **Record:** rule name / profiles: ________

## M4. Dashboard port in use

- **Validates:** TASK-009 AC-12 (pending requirement amendment 8); the operations.md
  port guidance.
- **Steps:**
  1. With the application stopped, check `Get-NetTCPConnection -LocalPort <port> -State Listen`.
     It must list nothing; if something is listed, choose another `dashboardPort`.
  2. Temporarily occupy the port, for example:

     ```powershell
     $l = [Net.Sockets.TcpListener]::new([Net.IPAddress]::IPv6Any, <port>); $l.Server.DualMode = $true; $l.Start()
     ```

  3. Start the application by hand.
  4. Stop the listener with `$l.Stop()` and start the application again.
- **Expected:**
  - Step 3: exit code 6, and the console shows `PC Handoff: dashboard port <port> is in use`.
    `app.log` gets one ERROR `dashboard port in use`. `status.json` and `activity_log.json`
    are untouched: compare their timestamps or hashes before and after.
  - Step 4: the application starts normally.
- **Record:** exit code: ____ files untouched: yes / no

## M5. Boot-time background process with session-query rights (A3)

- **Validates:** A3.
- **Steps:**
  1. Reboot the shared PC and do not sign in.
  2. From another machine, open the dashboard.
  3. Sign in over RDP as alice, then sign out.
  4. Check that the process is still running: `Get-Process python*`, or the scheduled task's
     state.
  5. With alice connected over RDP, open `<data>\status.json`.
- **Expected:**
  - Step 2: the dashboard is served without anyone signed in.
  - Step 4: the process survives the sign-out.
  - Step 5: alice's entry has a non-null `clientName` and `clientAddress`, so the account
    can query other sessions' client information.
  - No `access_denied` in `app.log`.
- **Record:** account used: ________ client fields present: yes / no

## M6. AC-13: dashboard from another LAN machine, viewer toolbar

- **Validates:** REQ AC-13; A4; ADR-002 `client.toolbarMode = "viewer"` (Critic TASK-010
  N2a).
- **Steps:**
  1. On a different LAN machine, open `http://<shared-pc-hostname-or-ip>:<port>/` in a
     browser.
  2. Time how long the page takes to show the status.
  3. Open the page's top-right menu.
- **Expected:**
  - The status badge (Available / In Use) appears within 3 s.
  - There is no login or authentication prompt.
  - There is no "Deploy" button, and the menu offers no developer options.
- **Record:** seconds to status: ____ login prompt: yes / no Deploy visible: yes / no

## M7. Real RDP and console sessions (AC-1, AC-5, AC-6, AC-25, AC-26; A11; Critic TASK-002 S2)

Keep the dashboard open on a LAN machine, and keep `<data>\activity_log.json` and
`status.json` at hand. Do not open them in Excel; copy them first, see operations.md
section 6. Each "within 7 s" means `sessionPollIntervalSeconds` + 2 s with the defaults.

1. **RDP logon (AC-1).** alice connects over RDP from her machine.
   - **Expected:**
     - Within 7 s, `status.json` shows `In Use`.
     - After the next refresh, the dashboard shows alice, `RDP`, her machine's name and IP,
       and a ticking duration.
     - One `session_start` with `reason: "observed"`.
   - **Client address decoding (A11):** compare the IP shown with `ipconfig` on alice's
     machine, and with `clientAddress` in the raw session dump.
   - **Record:** IP shown ________ actual ________ match: yes / no
2. **Disconnect (AC-5).** alice closes the RDP window without signing out.
   - **Expected:**
     - Within 7 s the status is `Available`.
     - alice is listed under "Logged on but disconnected (not in use)".
     - One `session_end` with `endCause: "disconnect"` and `durationApproximate: false`.
   - **Disconnected RDP shown as Console? (Critic TASK-002 S2):** in the raw session dump and
     in `status.json`, read alice's `protocolType` and `connectionType` while she is
     Disconnected.
   - **Record:** protocolType ____ connectionType ____. If it is `0` / `Console`, report it:
     the tracker would then keep the previous type for Disconnected sessions (decisions.log
     TASK-002 S2 follow-up).
3. **Reconnect (AC-6).** alice reconnects from bob's machine, or from her own.
   - **Expected:**
     - Within 7 s, `In Use`.
     - A new `session_start` with a new `usagePeriodId` and the same `sessionId`.
     - The duration counts from the reconnect.
   - **ConnectTime on reconnect (A11):** compare `connectTime` in `status.json` before (step
     1) and after.
   - **Record:** connectTime changed: yes / no. Was there a WARNING `... (I4)` in `app.log`?
     yes / no
4. **Console sign-in (AC-25).** With nobody connected, erin signs in at the PC's own
   keyboard.
   - **Expected:**
     - `In Use — erin (N/A)`, with a row for erin showing `Console`, `N/A`, `N/A`.
     - In `status.json`, `connectionType: "Console"` with null client fields.
     - One `session_start` with `connectionType: "Console"`.
   - Then erin signs out (see M8).
5. **RDP resumed at the console (AC-26).** alice connects over RDP, disconnects, and then
   signs in at the physical console as alice, so that Windows resumes the same session.
   - **Expected:**
     - `In Use` with `connectionType: "Console"`.
     - The same `sessionId` and `logonTime` as her RDP session.
     - One new `session_start` (Console) with a new `usagePeriodId`.
     - No second `session_end` for the RDP period.
   - **ConnectTime on console resume (A11):** did `connectTime` change on the resume? If it
     did not, the new period is `durationApproximate: true` and `app.log` has the I4
     WARNING. That is correct behaviour; record it.
   - **Record:** connectTime updated on resume: yes / no

## M8. Console sign-out (ADR-007 R1)

- **Validates:** R1 and the risk raised in Critic arch/tasks iteration 2.
- **Steps:**
  1. erin (or alice at the console) signs out of Windows at the console.
  2. Run the raw session dump and `qwinsta` within 10 s.
- **Expected:**
  - Within 7 s, a `session_end` with `endCause: "logoff"`.
  - The status does not stay `In Use`.
- **Record:** which did Windows do?
  - the session ID disappeared, or
  - the ID was kept for the logon screen with an empty user name (session ____ state ____).

  Both are handled as "gone"; record which one was observed.

## M9. AC-14: 17 s real-time refresh

- **Validates:** REQ AC-14's end-to-end bound. The automated test proves it by composition:
  `tests/acceptance/test_config.py::test_ac14_*`.
- **Steps:**
  1. With the default settings (refresh 10 s, poll 5 s), open the dashboard showing
     `Available` on a LAN machine and do not touch it.
  2. Have alice connect over RDP, and note the time she reaches her desktop.
  3. Watch the dashboard.
- **Expected:**
  - The dashboard shows `In Use` without a reload, within 17 s.
  - "Last updated" advances at least every 12 s.
- **Record:** seconds until `In Use` was shown: ____

## M10. AC-21 p95 benchmark on the shared PC (A10)

- **Validates:** the p95 part of REQ AC-21; A10.
- **Steps:**
  1. Stop the application, so that it does not compete for the disk.
  2. In `<ext>`, install the dev extras once: `.venv\Scripts\pip install -e .[dev]`.
  3. Run `.venv\Scripts\pytest -m perf --no-cov -s`.
- **Expected:**
  - The test passes, printing p50 and p95 over 100 polls at 10,000 events.
  - p95 <= 1.0 s.
  - If p95 is above about 0.5 s, add the Defender exclusion (operations.md section 6) and
    run it again.
- **Record:** p50 ____ ms p95 ____ ms Defender exclusion: yes / no

## M11. `poll completed` review after a day of operation (A10; Critic MINOR-9)

- **Steps:**
  1. After at least one working day, run the p95 snippet from operations.md section 8 on
     `<data>\logs\app.log`.
  2. Search the log for lines with `"pendingEvents"` other than 0.
  3. Count the `outcome` values.
- **Expected:**
  - p95 of `durationMs` <= 1000 ms.
  - `pendingEvents` stays at 0.
  - `enumeration_failed` and `error` outcomes are rare and explained.
- **Record:** polls ____ p95 ____ ms max pendingEvents ____ failed/error outcomes ____

## M12. CSV export with a formula-like client name (Critic TASK-010 N2b)

- **Validates:** the N2b note in operations.md (known limitation L10). Code inspection of
  Streamlit 1.66 says formulas are **not** neutralised.
- **Steps:**
  1. Connect with an RDP client whose client name starts with `=`, `+`, `-` or `@`. For
     example, use FreeRDP with `/client-hostname:=1+2`; mstsc always sends the computer
     name.
  2. On the dashboard, hover over the Active table and use its download (CSV) button.
  3. Open the file in Excel with default settings.
- **Expected:**
  - The dashboard shows the name literally.
  - Record what Excel does with the cell: shows `3` (formula evaluated), shows `=1+2`, or
    warns.
  - Whatever the result, operators follow the operations.md guidance (import as text).
- **Record:** Excel showed ________

## M13. Second instance across sessions (ADR-004)

- **Validates:** the lock across logon sessions. A boot-time task and an interactive start
  run in different Windows sessions.
- **Steps:** while the boot-time instance runs, start
  `<ext>\.venv\Scripts\python -m pc_handoff --data-dir <data>` from an interactive console.
- **Expected:**
  - Exit code 3, with the message `another PC Handoff instance holds ...pc-handoff.lock`.
  - The data files are unchanged.
- **Record:** exit code ____

## M14. Reservation queue, ownership handoff, and status notes (REQ-002)

- **Validates:** REQ-002 self-service reservation queue, ownership handoff countdown and RDP
  session correlation, and broadcast status notes (`SD001`, `DEC-010`).
- **Steps:**
  1. While alice is connected over RDP (`In Use`), open the dashboard on bob's LAN machine,
     click **⚙️ Manage Queue & Notes**, and join the queue as `bob` (duration `60`
     min, notes `Integration test`).
  2. Have alice post a status note (`Wrapping up in 5 minutes`) and then click **Ready to
     Handoff** (`offeredByUserName`: `alice`).
  3. Have bob connect over RDP (or click **Claim Now (bob)**) within the 5-minute grace window.
  4. Inspect `<data>\reservations.json` and `<data>\activity_log.json`.
- **Expected:**
  - Step 1: bob appears in the **Reservation queue** table as `QUEUED`.
  - Step 2: alice's note appears under **Status notes**, bob transitions to `READY` with a
    5-minute countdown banner (`Handoff offered by alice to bob`), and `handoff_initiated` is
    recorded in `activity_log.json`.
  - Step 3: bob's reservation transitions to `ACTIVE` automatically upon RDP logon (or manual
    claim), and `handoff_claimed` is recorded in `activity_log.json`.
- **Record:** queue & handoff verified: yes / no

## M15. Dedicated Manage View, slot editing, .rdp download, and host PC notifications (REQ-003)

- **Validates:** REQ-003 header navigation (`⚙️ Manage Queue & Notes` / `← Back to PC Status`),
  3-tab management interface (`➕ Join Queue`, `🤝 Ready to Handoff`, `📝 Post Status Note`),
  queued reservation editing, `.rdp` file download, and Windows host notifications (`SD002`,
  `DEC-011`).
- **Steps:**
  1. On the main Status View, click **⚙️ Manage Queue & Notes** in the top right.
  2. In **➕ Join Queue**, verify the live wait-time estimate, join as `bob` (`30` min), then edit
     `bob`'s queued duration to `60` min and click **Save Changes (bob)**.
  3. In **🤝 Ready to Handoff**, click **Download .rdp Connection File** and verify `pc-handoff.rdp`
     downloads with `full address:s:<host>:3389`.
  4. In **📝 Post Status Note**, post a note with category `Maintenance` and verify `[Maintenance]`
     is displayed.
  5. Confirm that the shared host PC displays a Windows toast notification (or `msg.exe` modal when
     `hostNotificationMethod` is `"msg"`) upon queue join and handoff offer.
- **Expected:**
  - All navigation transitions, slot edits (`reservation_updated` in `activity_log.json`), `.rdp`
    file downloads, categorized notes, and host notifications succeed without errors.
- **Record:** manage view & host notifications verified: yes / no

---

## Results

| Check | Validates | Date | Tester | Result (PASS / FAIL) | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| M1 Python version | A12 | | | | |
| M2 Data directory on a local disk | A9 | | | | |
| M3 Firewall rule | A4 | | | | |
| M4 Dashboard port in use | TASK-009 AC-12 | | | | |
| M5 Boot-time process, session-query rights | A3 | | | | |
| M6 Dashboard from another LAN machine, viewer toolbar | REQ AC-13, A4 | | | | |
| M7.1 RDP logon, client address decoding | REQ AC-1, A11 | | | | |
| M7.2 Disconnect, disconnected RDP connection type | REQ AC-5, TASK-002 S2 | | | | |
| M7.3 Reconnect, ConnectTime on reconnect | REQ AC-6, A11 | | | | |
| M7.4 Console sign-in | REQ AC-25 | | | | |
| M7.5 RDP resumed at console, ConnectTime on resume | REQ AC-26, A11 | | | | |
| M8 Console sign-out | ADR-007 R1 | | | | |
| M9 17 s real-time refresh | REQ AC-14 | | | | |
| M10 p95 benchmark | REQ AC-21 (p95), A10 | | | | |
| M11 `poll completed` review after a day | A10 | | | | |
| M12 CSV export with a formula-like client name | N2b (L10) | | | | |
| M13 Second instance across sessions | ADR-004 | | | | |
| M14 Reservation queue, ownership handoff, and status notes | REQ-002 | | | | |
| M15 Dedicated Manage View, slot editing, .rdp download, and host PC notifications | REQ-003 | | | | |
