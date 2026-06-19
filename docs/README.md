# Documentation map

Each document has one job:

| Document | Role |
| --- | --- |
| [../README.md](../README.md) | **User guide** — install, run, the CLI usage, the UK-tax workflow. The authoritative user-facing reference. |
| [../CLAUDE.md](../CLAUDE.md) | **Always-loaded agent contract** — invariants, conventions, the working agreement, and pointers. Kept lean. |
| [architecture.md](architecture.md) | **Contributor internals** — module map, CLI reference, configuration reference, the UK-tax pipeline, and the recipe for adding a bank. Read on demand. |
| [../CHANGELOG.md](../CHANGELOG.md) | **What shipped** — notable features, newest first. |
| [backlog.md](backlog.md) | **What's next** — open, unimplemented ideas only. |
| [design-decisions.md](design-decisions.md) | **Why it's built this way** — the durable rationale behind the load-bearing choices. |
| [reporting-audit.md](reporting-audit.md) | **Point-in-time audit** — what's solid / missing in the reporting subsystems (tax reporting-status + the analytical reports), prioritised. A dated snapshot; adopted items move to the backlog. |
| [plans/](plans/) | **In-flight plans** — implementation briefs for features not yet built. When a plan ships, move it to `archive/`. |
| [archive/](archive/) | **Historical record** — the implementation briefs / plans that built shipped features (kept for provenance; not current guidance). |

If you're adding a doc, place it by *role*, not by topic: a how-to (for
users) goes in the README, an internals reference (modules, CLI, config) in
architecture.md, a must-obey constraint in CLAUDE.md, a rationale in
design-decisions.md, a future idea in the backlog. When a backlog item
ships, move its line to the CHANGELOG rather than annotating it in
place.

The agent-operating config lives outside `docs/`: the project Definition of
Done is `.claude/rules/definition-of-done.md` and the review subagent is
`.claude/agents/code-reviewer.md`.
