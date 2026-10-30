# Gap Analysis & Implementation Log

This document compares the goals and core capabilities outlined in [docs/project-overview.md](docs/project-overview.md) against the current codebase implementation.

---

## Currently Missing Functionalities

*(None — All core capabilities specified across REQ-001 through REQ-004 have been fully implemented and verified.)*

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
| **Host PC notification integration** | Implemented | Zero-dependency Windows Toast (WinRT) & `msg.exe` terminal broadcast alerts (`REQ-003`, `REQ-004`, `DEC-011`, `DEC-016`) |
| **Microsoft Teams notification integration** | Implemented | Zero-dependency Power Automate webhook integration sending Adaptive Cards on queue join & handoff ready (`REQ-004`, `DEC-016`) |
| **Configurable Queue Expiration & Re-queueing** | Implemented | `claimGraceMinutes` (1–1440 min) and rotating expired claims to `REQUEUED` status at queue tail (`REQ-004`, `DEC-016`) |
| **Client IP Silent Handoff Correlation** | Implemented | Optional client IP registration, `known_users.json` directory, auto-claim on RDP connect, and Real Name status badge enrichment (`REQ-004`, `DEC-016`) |
| **Reservation audit logging** | Implemented | Full lifecycle & slot-update events recorded in `activity_log.json` (`REQ-002`, `REQ-003`, `REQ-004`) |
