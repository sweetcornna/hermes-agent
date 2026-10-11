# Hermes Agent guidance

Use [AGENTS.full.md](AGENTS.full.md) as a task-indexed reference. Preserve the [contribution rubric](AGENTS.full.md#contribution-rubric--what-we-want--what-we-dont) when adding capabilities or reviewing work; it does not authorize closing issues on taste or scope alone.

## Standing constraints

- Preserve prompt-cache stability and role alternation. System prompts, tools and prior messages remain stable except the documented compression/explicit-update paths; slash-command content belongs in the normal user message.
- Resolve state through `get_hermes_home()`, never a hardcoded `~/.hermes`. Profile-multiplexed work must fail closed when its scope is missing, not borrow another profile's environment. Tests must not touch real user state.
- Secrets belong in `.env`; behavioral settings belong in `config.yaml` through the correct loader. Keep telemetry/attribution opt-in and preserve configured model/provider behavior.
- Capabilities belong at the edges of the narrow core. New third-party products and memory-provider plugins stay out of the core tree; follow the existing plugin policy and compatibility promises.
- Surface capability belongs to the session, not process environment flags. Preserve tool-content reading contracts rather than truncating instructions the running agent needs in full.

## Task routes

| Task | Read in `AGENTS.full.md` |
| --- | --- |
| Agent loop, context or gateway | “AIAgent Class”, “Important Policies”, “Profiles: Multi-Instance Support”, and matching “Known Pitfalls” |
| Tools, plugins or new capabilities | “Adding New Tools”, “Plugins”, “Toolsets” and the contribution rubric |
| Config, providers or dependencies | “Adding Configuration”, “Dependency Pinning Policy”, and matching provider-plugin section |
| CLI/TUI/desktop | Matching CLI/TUI section; for desktop also [apps/desktop/AGENTS.md](apps/desktop/AGENTS.md) |
| Skills, delegation, curator, cron or kanban | Only the named feature section and its linked reference; authoring contracts still apply |
| Verification | [Testing](AGENTS.full.md#testing) and the relevant package scripts |

Run Python tests through `scripts/run_tests.sh` with the affected paths; its fresh-process isolation is required. Place JS/TS behavior tests in their native runner, not Python source checks. Pure prose edits use relevant document/link checks. Preserve review and acceptance gates, fix task-caused failures, and report the actual evidence and remaining blockers.
