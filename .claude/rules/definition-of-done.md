# Definition of Done

A change is not done until ALL of these hold. Do not report a task complete
until you have checked each one and can say which command or evidence
confirms it.

- [ ] Lints clean: `uv run ruff check .`
- [ ] Type-checks clean: `uv run mypy src`
- [ ] Tests pass, including new tests for the changed behaviour:
      `uv run pytest`
- [ ] If the change touches extraction/rendering, the affected beancount
      goldens still diff clean (or were regenerated deliberately, with the
      diff reviewed).
- [ ] If the ledger is affected, `uv run banking-pipeline check` (or
      `rebuild --strict`) passes — no new `bean-check` errors, no new
      reconcile drift.
- [ ] `code-reviewer` subagent run on the diff, with no Critical findings
      outstanding.
- [ ] **Project invariants in `CLAUDE.md` still hold** — in particular: no
      `import beancount` / no `PyMuPDF`; no `Equity:Uncategorized` in
      generated output; tax math reads the JSONL sidecars, not the ledger;
      `gross_income − withholding_tax == amount`; generated ledgers not
      hand-edited.
- [ ] **PII guard clean:** `python3 scripts/check_pii.py --all` passes, and
      no real amounts/balances/holdings from `data/` or `reports/` appear in
      any committed doc, commit message, or backlog/changelog entry.
- [ ] Active plan in `docs/plans/` updated: current stage advanced, items
      ticked, deviations/decisions recorded.
- [ ] Docs updated if the public surface changed — user-facing → `README.md`;
      internals (modules, CLI, config) → `docs/architecture.md`; a constraint
      → `CLAUDE.md`; a rationale → `docs/design-decisions.md`.

If any item cannot be met, stop and say so rather than working around it.
