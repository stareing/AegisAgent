# Agent Team Protocol Specification

> Status: Draft
> Scope: Defines the normative protocol and acceptance baseline for product-grade agent teams in this repository.
> Relationship:
> - [agent_team_alignment_plan.md](/home/jiojio/my-agent/docs/agent_team_alignment_plan.md): implementation roadmap
> - This document: protocol, lifecycle, compatibility, and compliance rules
> Authority:
> - This document defines what is compliant
> - The alignment plan defines how compliance is reached
> - If the two documents diverge, this specification wins

---

## 1. Purpose

This document defines the standard protocol for agent teams.

Its purpose is to prevent three recurring failure modes:

1. Prompt-only simulation without real runtime entities
2. UI-only behavior without a protocol-level source of truth
3. Partial implementations that appear to work but violate lifecycle or routing rules

This specification is normative unless a section is explicitly marked as non-normative.

---

## 2. Terminology

### 2.1 Roles

- `Lead`: the session that owns the team and coordinates work
- `Teammate`: an independent worker session belonging to the team
- `User`: the human interacting with the lead or a focused teammate

### 2.2 Identity

- `team_id`: unique identifier of a team
- `member_id`: unique identifier of a teammate inside a team; the only valid mailbox and task identity
- `session_id`: identifier of a long-lived teammate session
- `run_id`: identifier of a single execution turn within a session
- `task_id`: identifier of a shared team task
- `event_id`: identifier of a mailbox event
- `request_id`: identifier for question/approval workflows
- `correlation_id`: identifier linking a reply to an original event

### 2.3 Truth Sources

- `TeamConfigStore`: authoritative store for team membership and metadata
- `TeamTaskStore`: authoritative store for shared tasks and dependencies
- `TeamSessionStore`: authoritative store for teammate session state
- `Mailbox`: transport channel for team messages; not the source of truth for tasks or sessions

### 2.4 Status Terms

- `IDLE`: teammate is alive and available for new work
- `WORKING`: teammate is actively performing work
- `WAITING_ANSWER`: teammate is blocked waiting for lead input
- `WAITING_APPROVAL`: teammate is blocked waiting for plan approval
- `NOTIFYING`: final result exists and is being delivered to the lead
- `FAILED`: teammate or task reached a terminal error

---

## 3. Scope of Compliance

An implementation is compliant only if it provides:

1. Real team entities: team, members, tasks, sessions
2. A shared task list with dependencies and atomic claim
3. Direct teammate communication by `member_id`
4. Long-lived teammate session semantics
5. Automatic idle/completion notifications
6. Cleanup semantics with active-member refusal
7. Hook interception on idle and task completion

The following do not count as compliance:

1. Prompt instructions without backing state
2. Natural-language summaries acting as task state
3. UI rendering without protocol events
4. One-shot runs pretending to be teammate sessions

---

## 3.1 How to use this document with the alignment plan

Use this specification together with:

- [agent_team_alignment_plan.md](/home/jiojio/my-agent/docs/agent_team_alignment_plan.md)

Division of responsibility:

1. This document defines:
   - terms
   - identities
   - state machines
   - actions
   - event envelopes
   - error model
   - acceptance matrix

2. The alignment plan defines:
   - delivery phases
   - file/module placement
   - migration order
   - implementation examples

Review rule:

1. Protocol review MUST cite this document
2. Implementation review MUST cite both this document and the alignment plan

---

## 4. Core Entities

### 4.1 TeamConfig

Required fields:

```json
{
  "team_id": "team_auth_refactor",
  "lead_id": "lead_main",
  "members": [
    {
      "member_id": "tm_architect",
      "role": "architect",
      "session_id": "sess_architect_001"
    }
  ],
  "created_at": "2026-03-22T00:00:00Z",
  "updated_at": "2026-03-22T00:00:00Z"
}
```

Rules:

1. `member_id` MUST be unique within a team
2. `lead_id` MUST remain fixed for the lifetime of the team
3. `members` MUST be persisted independently of UI state

### 4.2 TeamTask

Required fields:

```json
{
  "task_id": "task_001",
  "team_id": "team_auth_refactor",
  "title": "Review auth flow",
  "description": "Identify edge cases and failure modes",
  "status": "PENDING",
  "assigned_to": "",
  "depends_on": [],
  "created_by": "lead_main",
  "result_summary": "",
  "created_at": "2026-03-22T00:00:00Z",
  "updated_at": "2026-03-22T00:00:00Z"
}
```

Rules:

1. `task_id` MUST be stable and unique
2. `status` MUST be derived from the task state machine, not from model text
3. `depends_on` MUST reference existing task ids or be empty
4. `assigned_to` MUST be a valid `member_id` or empty

### 4.3 TeamSessionState

Required fields:

```json
{
  "session_id": "sess_architect_001",
  "team_id": "team_auth_refactor",
  "member_id": "tm_architect",
  "status": "WORKING",
  "current_task_id": "task_002",
  "last_run_id": "run_abc123",
  "history_ref": "history://sess_architect_001",
  "updated_at": "2026-03-22T00:00:00Z"
}
```

Rules:

1. A session MUST belong to exactly one `member_id`
2. A member MUST have at most one active session
3. `history_ref` MUST refer to a real stored history or equivalent persisted state

---

## 5. State Machines

### 5.1 TeamMemberStatus

Allowed statuses:

```text
SPAWNING
IDLE
WORKING
WAITING_ANSWER
WAITING_APPROVAL
NOTIFYING
FAILED
SHUTDOWN_REQUESTED
SHUTDOWN
```

Allowed transitions:

```text
SPAWNING -> IDLE
IDLE -> WORKING
WORKING -> WAITING_ANSWER
WAITING_ANSWER -> WORKING
WORKING -> WAITING_APPROVAL
WAITING_APPROVAL -> WORKING
WORKING -> NOTIFYING
NOTIFYING -> IDLE
WORKING -> FAILED
WAITING_ANSWER -> FAILED
WAITING_APPROVAL -> FAILED
NOTIFYING -> FAILED
IDLE -> SHUTDOWN_REQUESTED
WORKING -> SHUTDOWN_REQUESTED
SHUTDOWN_REQUESTED -> SHUTDOWN
```

Forbidden:

1. `WORKING -> IDLE` without a delivered final notification
2. `WAITING_ANSWER -> IDLE` without continuation or explicit cancellation
3. `FAILED -> WORKING` without creating a new session or recovery protocol

### 5.2 TaskStatus

Allowed statuses:

```text
PENDING
BLOCKED
IN_PROGRESS
COMPLETED
FAILED
CANCELLED
```

Allowed transitions:

```text
PENDING -> IN_PROGRESS
BLOCKED -> PENDING
IN_PROGRESS -> COMPLETED
IN_PROGRESS -> FAILED
PENDING -> CANCELLED
BLOCKED -> CANCELLED
IN_PROGRESS -> CANCELLED
```

Forbidden:

1. `BLOCKED -> IN_PROGRESS` without all dependencies completed
2. `COMPLETED -> IN_PROGRESS` without a new task version

### 5.3 TeamLifecycleStatus

Allowed statuses:

```text
CREATED
ACTIVE
CLEANUP_PENDING
CLEANED
FAILED
```

Cleanup rule:

1. `ACTIVE -> CLEANED` is forbidden while any member is not terminal or idle according to cleanup policy

---

## 6. Actions

### 6.1 Lead Actions

| Action | Sender | Required Fields | Success Effect | Failure Condition |
| --- | --- | --- | --- | --- |
| `create_team` | Lead | `name` | Creates team config and stores | team already exists |
| `spawn_member` | Lead | `role` | Adds member and session | invalid role / spawn failure |
| `create_task` | Lead | `title` | Adds task to task store | invalid dependency |
| `assign_task` | Lead | `task_id`, `member_id` | Binds task to member | member busy / task unavailable |
| `approve_plan` | Lead | `request_id` | Unblocks teammate continuation | request not found |
| `reject_plan` | Lead | `request_id`, `feedback` | Returns teammate to plan revision | request not found |
| `answer_question` | Lead | `request_id`, `answer` | Resumes waiting teammate | request not found |
| `shutdown_member` | Lead | `member_id` | Starts shutdown handshake | member not found |
| `cleanup_team` | Lead | none | Cleans team resources | active members exist |

### 6.2 Teammate Actions

| Action | Sender | Required Fields | Success Effect | Failure Condition |
| --- | --- | --- | --- | --- |
| `claim_task` | Teammate | optional `task_id` | Claims task atomically | task unavailable |
| `complete_task` | Teammate | `task_id` | Marks task completed | hook denial / task mismatch |
| `fail_task` | Teammate | `task_id`, `error` | Marks task failed | task mismatch |
| `send_message` | Teammate | `to_member_id`, `payload` | Sends point-to-point event | invalid recipient |
| `broadcast` | Teammate | `payload` | Broadcasts to teammates | none |
| `publish` | Teammate | `topic`, `payload` | Publishes to subscribers | invalid topic |
| `subscribe` | Teammate | `topic_pattern` | Adds subscription | invalid pattern |

---

## 7. Mailbox Event Protocol

### 7.1 Event Envelope

Required shape:

```json
{
  "event_id": "evt_123",
  "team_id": "team_auth_refactor",
  "from_agent": "tm_architect",
  "to_agent": "tm_reviewer",
  "event_type": "QUESTION",
  "request_id": "req_789",
  "correlation_id": "",
  "payload": {
    "question": "Should we migrate the token cache?"
  }
}
```

Normative rules:

1. `from_agent` MUST be a `member_id` or `lead_id`, not an internal spawn handle
2. `to_agent` MUST be a valid recipient or `"*"` for broadcast
3. `request_id` MUST be present for `QUESTION` and `PLAN_SUBMISSION`
4. `correlation_id` MUST be present on replies

### 7.2 Standard Event Types

| Event Type | Sender | Receiver | Required Fields |
| --- | --- | --- | --- |
| `TASK_ASSIGNMENT` | Lead | Teammate | `task_id` or `task` |
| `QUESTION` | Teammate | Lead/Teammate | `request_id`, `question` |
| `ANSWER` | Lead/Teammate | Teammate | `request_id`, `answer` |
| `PLAN_SUBMISSION` | Teammate | Lead | `request_id`, `title`, `plan_text` |
| `APPROVAL_RESPONSE` | Lead | Teammate | `request_id`, `approved` |
| `PROGRESS_NOTICE` | Teammate | Lead | progress payload |
| `ERROR_NOTICE` | Any | Lead/Teammate | error payload |
| `BROADCAST_NOTICE` | Any | `*` | message payload |
| `SHUTDOWN_REQUEST` | Lead | Teammate | reason |
| `SHUTDOWN_ACK` | Teammate | Lead | request reference |

### 7.3 Example: Direct Request/Reply

```json
{
  "event_id": "evt_456",
  "team_id": "team_auth_refactor",
  "from_agent": "tm_a",
  "to_agent": "tm_b",
  "event_type": "QUESTION",
  "request_id": "req_123",
  "payload": {
    "question": "Review my approach?"
  }
}
```

Reply:

```json
{
  "event_id": "evt_789",
  "team_id": "team_auth_refactor",
  "from_agent": "tm_b",
  "to_agent": "tm_a",
  "event_type": "ANSWER",
  "request_id": "req_123",
  "correlation_id": "evt_456",
  "payload": {
    "answer": "Approved"
  }
}
```

---

## 8. Notification Protocol

### 8.1 Notification Types

Allowed notification types:

```text
TASK_COMPLETED
TASK_FAILED
QUESTION
PLAN_SUBMISSION
ERROR
BROADCAST
TEAMMATE_IDLE
```

Normative rule:

1. `PROGRESS_NOTICE` MUST NOT be auto-promoted to a completion notification by default

### 8.2 Completion Notification Example

```json
{
  "team_id": "team_auth_refactor",
  "member_id": "tm_reviewer",
  "notification_type": "TASK_COMPLETED",
  "task_id": "task_004",
  "summary": "Added 3 regression tests for token refresh path"
}
```

### 8.3 Idle Notification Example

```json
{
  "team_id": "team_auth_refactor",
  "member_id": "tm_reviewer",
  "notification_type": "TEAMMATE_IDLE",
  "summary": "No more assigned tasks; ready to claim another task"
}
```

---

## 9. Hook Semantics

### 9.1 Required Hook Points

The implementation MUST expose:

1. `teammate.idle`
2. `teammate.task_completed`

### 9.2 Denial Semantics

If a hook returns denial:

1. The blocked transition MUST NOT commit
2. Feedback MUST be routed back to the responsible teammate or lead
3. State rollback or prevention MUST be explicit in storage

Example:

```json
{
  "prevented": true,
  "feedback": "Need regression tests before completion"
}
```

---

## 10. Cleanup Semantics

### 10.1 Cleanup Preconditions

`cleanup_team` MUST fail if any member is in:

```text
SPAWNING
WORKING
WAITING_ANSWER
WAITING_APPROVAL
NOTIFYING
SHUTDOWN_REQUESTED
```

### 10.2 Cleanup Success Conditions

On success, the implementation MUST remove or finalize:

1. team config
2. task store
3. session store
4. runtime handles
5. UI focus or pane resources

### 10.3 Cleanup Example

Failure:

```json
{
  "ok": false,
  "error_code": "TEAM_CLEANUP_ACTIVE_MEMBERS",
  "message": "Cannot clean up while active members exist",
  "active_members": ["tm_architect", "tm_reviewer"]
}
```

Success:

```json
{
  "ok": true,
  "team_id": "team_auth_refactor",
  "cleaned": true
}
```

---

## 11. Error Model

All protocol-level failures MUST use a structured error object.

Shape:

```json
{
  "ok": false,
  "error_code": "TEAM_MEMBER_BUSY",
  "message": "Teammate 'tm_architect' is busy",
  "retryable": false
}
```

Required fields:

1. `ok`
2. `error_code`
3. `message`
4. `retryable`

Recommended standard error codes:

- `TEAM_NOT_INITIALIZED`
- `TEAM_MEMBER_NOT_FOUND`
- `TEAM_MEMBER_BUSY`
- `TEAM_TASK_NOT_FOUND`
- `TEAM_TASK_BLOCKED`
- `TEAM_TASK_ALREADY_CLAIMED`
- `TEAM_REQUEST_NOT_FOUND`
- `TEAM_SPAWN_FAILED`
- `TEAM_CLEANUP_ACTIVE_MEMBERS`
- `TEAM_HOOK_DENIED`
- `TEAM_SESSION_NOT_FOUND`

---

## 12. Compatibility Rules

Legacy actions may coexist during migration, but MUST be explicitly labeled.

### 12.1 Allowed Legacy Interfaces

- `team(action="assign")`
- `team(action="collect")`
- mailbox debug views

### 12.2 Requirements

1. Legacy paths MUST be documented as compatibility-only where applicable
2. Legacy actions MUST NOT silently bypass task store or teammate session rules
3. A legacy action MUST still preserve the same source-of-truth invariants

Example:

If `assign` remains available, it MUST either:

1. create/update a real `TeamTask`, or
2. be documented as a temporary compatibility path with a planned removal boundary

---

## 13. Compliance Matrix

Each item below is mandatory for final acceptance.

| ID | Requirement | Pass Condition |
| --- | --- | --- |
| `AT-001` | Real team config exists | Team config persisted and queryable |
| `AT-002` | Real task list exists | Task store returns structured tasks |
| `AT-003` | Atomic claim works | Two claimers cannot both win |
| `AT-004` | Dependency unlock works | Blocked task becomes pending after deps complete |
| `AT-005` | Direct teammate message works | Recipient receives event by `member_id` |
| `AT-006` | Request/reply correlation works | Reply carries original `correlation_id` |
| `AT-007` | Plan approval gates execution | Implementation does not proceed before approval |
| `AT-008` | Long-lived teammate session exists | Same `session_id`, new `run_id` across interactions |
| `AT-009` | Idle notification works | Lead receives structured idle notification |
| `AT-010` | Cleanup refuses dirty state | Cleanup fails with active members |
| `AT-011` | Cleanup removes resources | Stores and runtime handles are cleared |
| `AT-012` | Hook denial blocks completion | State does not advance on denial |
| `AT-013` | Hook denial blocks idle | Idle transition does not commit |
| `AT-014` | User can focus teammate directly | Direct input routes to teammate session |
| `AT-015` | Progress is not treated as completion | No completion summary from progress-only events |

---

## 13.1 Mapping to implementation phases

This section links compliance items to the current implementation plan.

| Spec ID Range | Implementation Phase |
| --- | --- |
| `AT-001` ~ `AT-004` | Phase 1: task board / claim / dependency graph |
| `AT-012` ~ `AT-013` | Phase 2: team hooks |
| `AT-008` ~ `AT-009` | Phase 3: long-lived teammate sessions |
| `AT-005` ~ `AT-006`, `AT-014` | Phase 4: direct teammate interaction |
| `AT-010` ~ `AT-011`, `AT-015` | Phase 5: persistence / cleanup / anti-simulation |

Normative note:

1. Phase completion does not imply protocol compliance unless the mapped `AT-*` items pass.
2. A merged implementation without explicit `AT-*` coverage is non-compliant by default.

---

## 14. Required Scenario Tests

The implementation is incomplete until all scenario tests pass.

### Scenario A: Shared Task Flow

```text
1. Lead creates task_1 and task_2(depends_on=task_1)
2. teammate_a claims task_1
3. task_2 remains blocked
4. teammate_a completes task_1
5. task_2 becomes pending
6. teammate_b claims task_2
```

### Scenario B: Direct Interaction

```text
1. User focuses tm_reviewer
2. User sends "Only review tests"
3. tm_reviewer history is updated
4. lead history is not polluted with teammate-only instruction
```

### Scenario C: Approval Gate

```text
1. tm_architect submits PLAN_SUBMISSION
2. state = WAITING_APPROVAL
3. lead rejects with feedback
4. implementation does not start
5. lead approves revised plan
6. implementation starts
```

### Scenario D: Cleanup

```text
1. tm_reviewer is WORKING
2. lead calls cleanup
3. cleanup fails
4. reviewer shuts down
5. cleanup succeeds
```

### Scenario E: Anti-Simulation Guard

```text
1. progress event is emitted
2. /team-inbox peeks the inbox
3. progress remains pending until explicitly processed
4. no completion summary is generated from progress alone
```

---

## 15. Non-Normative Implementation Guidance

The protocol does not require a specific implementation language or storage engine.

Recommended decomposition:

1. `task_manager.py`
2. `session_manager.py`
3. `hooks_bridge.py`
4. `team_config_store.py`
5. `team_task_store.py`
6. `team_session_store.py`

Recommended UI layers:

1. `in-process` mode first
2. `split-pane` mode second

Recommended migration order:

1. shared task list
2. teammate session manager
3. claim/unblock
4. direct teammate focus
5. cleanup
6. hooks

---

## 16. Acceptance Record Template

Every implementation phase MUST include a compliance note in the following format:

```md
## Acceptance Record

- Spec IDs covered:
- Source of truth:
- Runtime path:
- Success scenarios:
- Failure scenarios:
- Example command:
- Example structured output:
- Remaining non-compliant areas:
```

Without this record, a change is not reviewable against the protocol.
