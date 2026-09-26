# Local app server protocol

Start `capslock --workspace /path/to/project app-server --stdio`. The transport is
newline-delimited JSON-RPC 2.0 on stdin/stdout; diagnostic messages go to stderr.
No HTTP listener, remote service or background daemon is created. EOF cancels
active/queued tasks and closes the application, provider clients and processes.

Send `initialize` with `{"protocol_version":1}` before other methods. Requests
use standard JSON-RPC `id`; notifications omit it. Session methods attach one
session at a time because opening application repositories performs recovery.
Changing the attached session is rejected while a run is active. Multiple runs
within the attached session use the existing AgentSession serialization.

| Method | Parameters | Result |
| --- | --- | --- |
| `session/create` | none | `session_id` |
| `session/resume` | `session_id` | attached `session_id` |
| `run/start` | `session_id`, `request_id`, `question` | `work_item_id`, nullable `run_id`, `duplicate` |
| `run/cancel` | `session_id`, `run_id` or `work_item_id` | `cancelled` |
| `events/subscribe` | `session_id`; optional `run_id`, `after_sequence`, `limit` | `subscribed`, persisted `events`, `has_more`, `next_sequence` |
| `approval/answer` | `session_id`, `request_id`, `choice` | `status`; resumed `run_id` where applicable |
| `input/answer` | `session_id`, `request_id`, `answers` | `status`, same resumed `run_id` |

The `request_id` in `run/start` is an application idempotency key, distinct from
the JSON-RPC envelope ID. A SQLite transaction reserves that key together with
the work item before launching a model. Reusing the key and question returns the
same item/run across process restarts; using a different question is an error.
The initial `run_id` can be null until the queued request begins; subsequent
`run/event` notifications carry the durable ID. Previously interrupted runs are
returned, not implicitly replayed. A new key explicitly starts new work.

Subscribe before starting a run to receive `run/event` notifications. Their
payload uses JSONL event schema 3 (`sequence`, `session_id`, `work_item_id`,
`run_id`, `event`, `terminal`, `data`, and audit identity fields). Reconnect by
resuming the session and subscribing with the last known run and sequence to
replay durable events. Clients should deduplicate events by event ID/sequence.
Replay pages default to 100 events and accept limits from 1 to 1000. When
`has_more` is true, request the next page using `after_sequence=next_sequence`.
Incoming messages are capped at 1 MiB; oversized input closes the connection
after a protocol error and cancels outstanding work.

Action approvals emit `approval/request` with request/session/run IDs, summary
and action type; execution remains suspended in the existing action authorizer.
Choices are `approve_once` (`approve` alias), `approve_session`, `approve_local`,
and `reject`. The existing permission service validates whether a durable grant
is available. Non-Action permission requests are taken from `waiting_approval`
run events and use the same answer method and existing permission service.
Structured questions arrive in `waiting_input`; answer with an object keyed by
the question IDs. These paths preserve normal permission checks and resume the
same paused run. No implicit authorization or automatic retries are added.
Plan requests also use `approval/answer`: entry accepts `enter` or `reject`;
submission accepts `implement`, `feedback` (with optional text `feedback`) or
`reject`. Decisions pass through the existing plan service. Explicitly approved
implementation work is queued exactly once after the planning run finishes,
using the same behavior as the foreground controller.

See `examples/app_server_client.py` for a minimal client that streams events and
stops for user interaction. This protocol v1 attaches one session and does not
support remote connections or multiplexing several workspace applications.

A workspace database has one live recovery owner. A second independent client
receives a clear workspace-busy error (`-32002` over RPC) before opening or
recovering SQLite state. Trusted in-process child agents share the lease without
recovery. The OS releases the lock after a process crash. Close the existing
client before attaching a different CLI/TUI/server process to that workspace.
