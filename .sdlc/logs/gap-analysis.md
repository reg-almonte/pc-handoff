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
| **Real-time Shared PC status monitoring** | Implemented | Session polling, status derivation (`Available`/`In Use`/`Unknown`), Streamlit UI |
| **Active RDP session detection** | Implemented | Windows Terminal Services (WTS) API provider & session parser |
| **Persistent local data storage** | Implemented | Atomic JSON stores for `config.json`, `status.json`, `activity_log.json`, file locking |
| **Activity & audit logging (Sessions)** | Implemented | Session lifecycle recorded to `activity_log.json` and `app.log` |
| **Reservation queue management** | **Missing** | Not yet designed or implemented |
| **Self-service reservation management** | **Missing** | Not yet designed or implemented |
| **Ownership handoff workflow** | **Missing** | Not yet designed or implemented |
| **User-to-user status messaging** | **Missing** | Not yet designed or implemented |
| **Teams notification integration** | **Missing** | Not yet designed or implemented |
| **Reservation audit logging** | **Missing** | Not yet designed or implemented |
