# Gap Analysis & Implementation Log

This document compares the goals and core capabilities outlined in [docs/project-overview.md](docs/project-overview.md) against the current codebase implementation.

---

## Currently Missing Functionalities

### 1. Reservation Queue Management
- **Description**: Managing a queue of users waiting to use the shared PC.
- **Missing Elements**:
  - Queue data model, validation schemas, and persistence mechanism.
  - UI components for viewing who is waiting and their queue position.
  - Queue transition logic (position tracking, expiration, queue advance when the PC becomes available).

### 2. Self-Service Reservation Management
- **Description**: Enabling users on the LAN to manage their own reservations.
- **Missing Elements**:
  - UI controls for joining the waiting list / reserving an upcoming slot.
  - Functionality for users to cancel, modify, or release their reservation or queue slot.

### 3. Structured Ownership Handoff Workflow
- **Description**: A formal process guiding transitions between the current active user and the next user in line.
- **Missing Elements**:
  - Handover request / release state machine.
  - Ready-to-yield prompts, acknowledgment workflows, and timeout/grace-period handling.

### 4. User-to-User Status Messaging
- **Description**: Direct lightweight messaging between current connected users and waiting users.
- **Missing Elements**:
  - Broadcast or direct status messages (e.g., "Finishing up tests in 10 minutes", "Please log off when ready").
  - UI display for active messages and announcements on the dashboard.

### 5. Microsoft Teams Notification Integration
- **Description**: Automated webhook / chat alerts to notify team members about status and queue events.
- **Missing Elements**:
  - Configurable Teams webhook URL settings in `config.json`.
  - Notification dispatch triggers:
    - Alerting the active user when someone joins the queue.
    - Alerting the next user in the queue when the PC becomes `Available`.
    - Alerting users upon handoff requests or cancellations.

### 6. Reservation & Handoff Audit Trail
- **Description**: Historical logging of reservations and handoff events.
- **Missing Elements**:
  - While RDP session connects/disconnects are logged in `activity_log.json`, events for reservations created/cancelled and handoff events are not yet captured or audited.

---

## Current Implementation Status Summary

| Capability from `project-overview.md` | Implementation Status | Notes |
| :--- | :---: | :--- |
| **Real-time Shared PC status monitoring** | Implemented | Session polling, status derivation (`Available`/`In Use`/`Unknown`), Streamlit UI (`REQ-001`) |
| **Active RDP session detection** | Implemented | Windows Terminal Services (WTS) API provider & session parser (`REQ-001`) |
| **Persistent local data storage** | Implemented | Atomic JSON stores for `config.json`, `status.json`, `activity_log.json`, `reservations.json` (`REQ-001`, `REQ-002`) |
| **Activity & audit logging (Sessions)** | Implemented | Session lifecycle recorded to `activity_log.json` and `app.log` (`REQ-001`) |
| **Reservation queue management** | Implemented | FIFO queue state machine, wait-time estimation, and auto-advance (`REQ-002`, `REQ-003`) |
| **Self-service reservation management** | Implemented | Dedicated Manage View with 3 semantic tabs, queue slot editing & cancellation (`REQ-002`, `REQ-003`) |
| **Ownership handoff workflow** | Implemented | 5-min grace period countdown, RDP auto-claim correlation, and `.rdp` file generator (`REQ-002`, `REQ-003`) |
| **User-to-user status messaging** | Implemented | TTL-bounded broadcast status notes with semantic category tags (`REQ-002`, `REQ-003`) |
| **Host PC notification integration** | Implemented | Zero-dependency Windows Toast (WinRT) & `msg.exe` terminal broadcast alerts (`REQ-003`, `DEC-011`) |
| **Reservation audit logging** | Implemented | Full lifecycle & slot-update events recorded in `activity_log.json` (`REQ-002`, `REQ-003`) |
