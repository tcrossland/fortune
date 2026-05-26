# Documentation map

Each document has one job:

| Document | Role |
| --- | --- |
| [../README.md](../README.md) | **User guide** — install, run, the CLI surface, the UK-tax workflow. The authoritative user-facing reference. |
| [../CLAUDE.md](../CLAUDE.md) | **Contributor / agent guide** — architecture, conventions, the non-obvious context for working in the repo. |
| [../CHANGELOG.md](../CHANGELOG.md) | **What shipped** — notable features, newest first. |
| [backlog.md](backlog.md) | **What's next** — open, unimplemented ideas only. |
| [design-decisions.md](design-decisions.md) | **Why it's built this way** — the durable rationale behind the load-bearing choices. |
| [archive/](archive/) | **Historical record** — the implementation briefs / plans that built shipped features (kept for provenance; not current guidance). |

If you're adding a doc, place it by *role*, not by topic: a how-to goes
in the README, a convention in CLAUDE.md, a rationale in
design-decisions.md, a future idea in the backlog. When a backlog item
ships, move its line to the CHANGELOG rather than annotating it in
place.
