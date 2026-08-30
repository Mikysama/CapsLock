# Architecture boundaries

CapsLock keeps stable façade classes while composing smaller services behind them. This
allows refactors to remain compatible with saved state, CLI integrations, and internal
extensions.

## Dependency direction

- CLI command handlers depend on `cli.factory` and application services. The factory
  does not import command dispatch or input-request handlers.
- `AgentSession` is the runtime entry point. Session administration, permission requests,
  planning requests, and run-stream coordination live in focused runtime services.
- Tool execution depends on typed service ports in `tooling.service_ports`; optional
  services are represented by `None`, not discovered through a service locator.
- Permission models, specs, pure rule selection, persistence ports, and middleware live
  under `tooling/permission_policy/`; callers import each concern from its owning module.
- `repositories/journal/` composes permission, tool invocation, input request, and run
  event repositories over one database connection and unchanged transactions.
- Collaboration mailbox, artifact publication, and audit components are independent of
  scheduling. Persistent Agent teams use repository-owned atomic claims, attempts,
  checkpoints, approval links, and an append-only budget ledger; session ownership and
  contract digests are revalidated at every control boundary. Memory settings are
  independent of catalog, recall, capture, maintenance, and transfer services.
- Raw transcript and tool artifacts remain authoritative. Episodic FTS, context summary
  segments, and memory extraction segment caches are derived indexes with explicit
  rebuild paths; portable exports must not treat them as source records.
- Memory extraction and verification are separate model calls. Automatic adoption may
  depend only on a calibration bound to the verifier model profile and prompt version;
  missing calibration is review-only.
- Slash-command handlers live under `cli/command_handlers/`; filesystem handlers live
  under `tooling/tools/filesystem/`; external Web and MCP Actions live under
  `application/action_system/external_actions/`.

## Compatibility invariants

- Public entry-point method signatures remain stable unless a full migration explicitly
  removes an obsolete import path.
- Database schema and configuration formats do not change during structural refactors.
- Tool registration order, model-visible schemas, audit payloads, and event ordering are
  behavioral contracts.
- Composition belongs in bootstrap/factory modules; domain and application services
  receive only the ports they use.
- New internal dependencies must point toward models and ports, never back toward CLI or
  composition roots.
