# Project Overview: PC Handoff

## Vision & Scope

PC Handoff is an internal web application that helps teams coordinate the use of a shared Windows PC accessed through Remote Desktop (RDP).

The application provides real-time visibility into whether the shared PC is currently in use, who is actively connected, and whether other users are waiting to use the machine. It also introduces a lightweight reservation and ownership handoff workflow to reduce communication overhead and eliminate uncertainty around PC availability.

The application runs locally on the shared Windows PC and is accessible to users within the same local network through a web browser. All data is stored locally and does not require cloud services, internet access, or external databases.

Primary goals:

- Provide visibility of current PC usage status.
- Allow users to reserve access to the shared PC.
- Notify current users when others are waiting.
- Support a structured ownership handoff process.
- Maintain a historical audit trail of usage and reservations.
- Keep deployment and maintenance simple.

Out of Scope (MVP):

- Multi-PC management
- Authentication and authorization
- Cloud hosting
- Role-based permissions
- Enterprise workflow integration beyond basic Teams notifications

## Core Capabilities

- Real-time Shared PC status monitoring
- Active RDP session detection
- Reservation queue management
- Ownership handoff workflow
- User-to-user status messaging
- Teams notification integration
- Activity and audit logging
- Persistent local data storage
- Self-service reservation management

## Development Principles

1. **Spec-Driven**: Every feature begins with an approved requirement specification.
2. **Deterministic Validation**: Automation scripts guard transitions before human/critic review.
3. **Continuous Knowledge**: Architectural decisions and assumptions are preserved in `.sdlc/docs/`.
4. **Test-First Reliability**: Every feature requires automated unit and integration tests.
5. **Offline-First**: Core functionality must remain operational without internet connectivity.
6. **Simplicity Over Complexity**: Favor maintainable solutions and minimal dependencies.
7. **Operational Transparency**: Users should always be able to determine the current status, ownership, and queue position at a glance.
8. **Data Persistence**: Reservations, ownership history, and activity logs must survive application restarts and system reboots.