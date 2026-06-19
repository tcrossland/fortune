# Plan: Three-document restructure — CLAUDE.md / docs/architecture.md / README.md

**Status:** complete — all 5 stages done, verification passed.
<!-- Active-plan pointer (CLAUDE.md) names this file. -->

## Context

`CLAUDE.md` was 948 lines and loaded on *every* turn — violating both the
project's `CLAUDE.md.template` ("a good ceiling is roughly one screen") and
`methodology.md` §1 ("keep `CLAUDE.md` lean… if a rule would not apply to
most turns, it does not belong in `CLAUDE.md`").

The deeper problem: **three documents had no clean division of labour**.
Content was duplicated and drifting:

- The exhaustive *Source layout* tree and per-command *CLI surface* existed
  in **both** CLAUDE.md and `README.md` — and the README's "Project layout"
  was **stale** (showed `cli.py`; it is now a `cli/` package), so neither
  was canonical.
- "Adding a new bank template" was in **both** README and CLAUDE.md.
- README's "Why not PyMuPDF / Why not import beancount" rationale
  duplicated `docs/design-decisions.md` §"Licence hygiene".
- The hard **invariants** (no PyMuPDF / no `import beancount`,
  `gross_income − withholding_tax == amount`, never re-parse beancount for
  tax, don't hand-edit generated files) were scattered through prose.
- No `## Active plan`, `## Working agreement`, `.claude/rules/` (DoD), or
  `code-reviewer` subagent (all prescribed by the template + methodology).

**Target — one job per document** (methodology §1 / `docs/README.md`):

| Doc | Role after this change |
|---|---|
| `README.md` | **User guide** — install, configure, run, CLI *usage*, the UK-tax *workflow*, the licence promise. |
| `docs/architecture.md` | **Contributor internals** (NEW) — module map, CLI *reference*, config-knob reference, data flow, extension recipes. |
| `CLAUDE.md` | **Invariants + conventions + working agreement + pointers** (~400 lines). |
| `docs/design-decisions.md` | **Rationale** — unchanged role; absorbs the deep licence rationale. |

User decisions: **Balanced** CLAUDE.md slim-down; add **all four** process
sections; **full review + refactor** of README.

## Out of scope (considered from `methodology.md`, deferred)

Each a separate task, mostly config not docs: hooks §6 (`PreToolUse`/
`PostToolUse`/`PreCompact`); `test-writer` subagent (App. B); skills §10;
slash commands `/plan` `/accept` `/retro` (App. I); migrating
`docs/design-decisions.md` → a `docs/design/*.md` ADR folder.
`docs/backlog.md` already exists — leave it.

## Constraints to preserve

- Keep the exact heading **"Beancount output conventions"** and the
  licence-hygiene invariant discoverable in CLAUDE.md:
  `docs/archive/*.md` (historical), `pyproject.toml:45`, and
  `src/banking_pipeline/archive.py:26` reference them.
- Docs/config-only change — **no `src/` edits**. PII guard must still pass;
  no real identifiers or figures in any prose.

---

## Stage 0 — Persist this plan   [done]
- [x] Write plan to `docs/plans/docs-restructure.md`.
- Acceptance: plan versioned in-repo; subsequent stages tick status here.

## Stage 1 — Write `docs/architecture.md` (fresh, canonical)   [current]

New file, explanatory prose + tables (not a paste of the 948-line tree).
Single current contributor reference. Sections:

- [ ] **Pipeline data flow** — `import` pre-stage + `PDF → extract →
  classify(lang→bank→doctype) → fields → writer → beancount` diagram.
- [ ] **Module map** — full annotated `src/banking_pipeline/` tree, current
  `cli/`-package form (supersedes CLAUDE.md "Source layout" + README
  "Project layout").
- [ ] **CLI reference** — per-command behaviour from CLAUDE.md "CLI surface";
  link README for usage examples.
- [ ] **Configuration reference** — the full knob catalogue.
- [ ] **Extension recipes** — the "add a new bank" 6-step recipe (single home).
- Acceptance: covers everything old CLAUDE.md "Source layout / CLI surface /
  Configuration" + README "Project layout / Adding a new bank" gave;
  `cli/` shown as a package.

## Stage 2 — Slim `CLAUDE.md` to Balanced   [not started]

Rewrite to the template skeleton (~400 lines):

1. Title + summary (1–2 sentences).
2. **## Invariants** (NEW) — licence bans; `gross_income − withholding_tax
   == amount`; never re-parse beancount for tax; ISA tax-exempt single
   choke point; don't hand-edit `data/portfolio.beancount`; root
   `main.beancount` must declare `booking_method`/`operating_currency`; no
   `Equity:Uncategorized`.
3. Architecture pointer → `docs/architecture.md` (plain path, not `@`-import).
4. Tone (keep).
5. **## Commands** — ruff / mypy / pytest; `rebuild`; `extract-text
   --show-rules` loop.
6. Keep inline: Domain conventions; Fixtures & tests; Anonymisation & PII
   guard; Strict-mode dispatch; **Beancount output conventions** (heading
   preserved); UK-tax choke-points; UK residence/FIG (condensed).
7. Move OUT: Source layout, CLI surface, Configuration catalogue.
8. **## Conventions**.
9. **## Active plan** → `docs/plans/docs-restructure.md`.
10. **## Working agreement** — plan-first; one-worktree; explicit `git add`;
    `code-reviewer` before merge; verify against DoD.
11. **## Where to find more**.
- Acceptance: ≤ ~420 lines; every old item retained, relocated, or a
  conscious dedup deletion. Every Invariant survives.

## Stage 3 — `.claude/` scaffolding, tracked   [not started]
- [ ] `.gitignore` — track `.claude/rules/` + `.claude/agents/` (keep
  `settings.local.json` / `worktrees/` ignored).
- [ ] `.claude/rules/definition-of-done.md` (methodology App. E, real commands).
- [ ] `.claude/agents/code-reviewer.md` (methodology App. A, read-only).
- Acceptance: `git check-ignore .claude/rules/definition-of-done.md` empty;
  `.claude/settings.local.json` still ignored.

## Stage 4 — Reconcile cross-references   [not started]
- [ ] `docs/README.md` doc-map — add architecture.md + `.claude/rules/`
  rows; re-scope the CLAUDE.md row; update the placement paragraph.
- [ ] `docs/design-decisions.md` — re-point "how the code is laid out" →
  `docs/architecture.md`; §"Licence hygiene" owns the PyMuPDF/beancount
  rationale.
- Acceptance: every remaining `CLAUDE.md`/`architecture.md` pointer resolves.

## Stage 5 — Full README review + refactor   [not started]
- [ ] Keep user-facing: Design, Supported documents, Quickstart, Batch
  rebuild, Output, UK-tax *workflow*, Validation, Tests.
- [ ] Trim "Libraries and licenses" / "Why not …" to the licence promise +
  pointer to design-decisions §"Licence hygiene".
- [ ] Move "Project layout", "Adding a new bank template", "Authoring
  classifier rules" → architecture.md; leave a one-line internals pointer.
- Acceptance: clean install→use guide; no stale layout; no
  contributor-internal sections; every removed section has a home.

---

## Verification
1. Completeness sweep — diff old vs new CLAUDE.md + README section lists.
2. PII guard — `python3 scripts/check_pii.py --all`.
3. No broken pointers —
   `grep -rn "CLAUDE.md\|architecture.md\|design-decisions" docs/ src/ README.md pyproject.toml`.
4. gitignore — the two `git check-ignore` checks.
5. Code green (docs-only sanity) — `uv run ruff check . && uv run mypy src && uv run pytest`.
6. Self-review — `code-reviewer` subagent on the diff.

## Decisions
- 19/06/2026 — Balanced over template-strict for CLAUDE.md.
- 19/06/2026 — Full README refactor (user choice): three-doc split.
- 19/06/2026 — Track `.claude/rules/` + `.claude/agents/`.
- 19/06/2026 — Plan named `docs-restructure` (scope grew beyond a CLAUDE.md
  slim-down).

## Deviations
- Review (independent reviewer on the diff) caught two dropped facts, both
  restored in `docs/architecture.md`: the `[post.reports]` `statements` →
  `balance_statements` glob fallback, and the `RULESETS_BY_BANK[bank] +
  GENERIC_RULES` routing (incl. the first-to-highest-score tie-break).
- CLAUDE.md landed at 253 lines (leaner than the ~400 Balanced target);
  README 560 → 438; new `docs/architecture.md` 662.
