# Plan: classify Pictet's annual fiscal statement

*Shipped 2026-07-02 — all stages complete and verified on the real archive.*
Account numbers / figures below are anonymised per the repo's
PII rules (placeholder body `123456`; real numbers live only in the
gitignored archive / `data/`). No real balances, holdings, or the NIF appear
here.

## Status: shipped

All stages done, verified, lints/types/tests green (972 tests), PII clean,
code-reviewer clean (no Critical/Major). Runtime findings worth recording:

- **The `VALORACIÓN DE CARTERA` anchor had to be case-SENSITIVE.** The
  lowercase `valoración de cartera` appears in a legal-note sentence in the
  *daily* reports too, so an `re.I` match wrongly classified a daily as a
  statement. Keyed on the all-caps *section header* only (the `.` still
  absorbs the accent). The fees anchor stayed `re.I` (dailies don't mention
  fees at all).
- **The audit found two misfiled statements, not one** — `2021/tax/Realised
  PL 20211231.pdf` *and* `2023/tax/Realised PL 20231231.pdf`; both re-filed to
  `Fiscal statement <date>.pdf`. The genuine `Realised PL 20241231.pdf` (fees
  concept absent) correctly stayed.
- **`tax_label` became the full filename stem** (`Realised PL` /
  `Unrealised PL` / `Fiscal statement`); `destination_for` dropped its
  hardcoded `" PL "`.

Real-archive result: three statements filed (`2021/2023/2024 …/Fiscal
statement <YYYYMMDD>.pdf`); the 4 `~/Downloads` zips imported (2025 + early-
2026 P&L dailies) and pruned to policy; prune leaves the statements untouched
and is idempotent (0 to move). A pair of identical `20260101` duplicates
(a Dropbox-race artifact from an earlier import cycle) were removed by hand.

## Goal

Pictet issues a comprehensive **annual Spanish tax pack** — titled
"Informe fiscal personas físicas" (newer generation) or "INFORME FISCAL"
(older) — downloaded from the portal as
`Tax - Statement Capital gains/losses + other income & tax info-<YYYYMMDD>.pdf`.
It is a **superset** of the Realised P/L report: it covers realised capital
gains **plus** investment income (`RENDIMIENTOS DEL CAPITAL MOBILIARIO`),
**admin/custody fees** (`Gastos de administración y depósito de valores
negociables`), **bank-account interest**, and portfolio valuation. Issued at
**year-end** (as-of 31 Dec).

Today the classifier can't tell it apart from the daily
[`Realised PL`](../archive/pictet-pnl-tax-archive.md) report — both carry the
`GANANCIAS Y PÉRDIDAS PATRIMONIALES` title and the `Del 01.01 … al …` range —
so it classifies as `tax_realised_pl` and would file as `Realised PL
<date>.pdf`, indistinguishable from (and colliding with) the narrower daily
report. At least one is already misfiled: `2021/tax/Realised PL 20211231.pdf`
is in fact a fiscal statement (it carries the fees concept).

Give it its own doctype, classify it by content, and file it under a
**distinct** name. **Archiving only** — like the P&L reports it is never
ingested into beancount and never fed to the UK-tax pipeline (its Spanish
FIFO/EUR figures must never reach the UK math).

## Decisions (locked)

- **Doctype:** `TAX_FISCAL_STATEMENT` (English, a deliberate exception to the
  issuer-vocabulary rule, consistent with `TAX_REALISED_PL` /
  `TAX_UNREALISED_PL`). Added to `NO_OUTPUT_DOCTYPES`.
- **Archive name:** `Fiscal statement <YYYYMMDD>.pdf` under `<as-of-year>/tax/`
  — e.g. `2024/tax/Fiscal statement 20241231.pdf`. Distinct from `Realised PL
  <date>.pdf`; title-case + `YYYYMMDD` mirrors the existing convention (sorts
  chronologically, globs as `Fiscal statement*`, idempotent skip-if-exists).
  ASCII only.
- **Discriminator:** gate on **two** statement-exclusive markers, both
  verified present in the 2024 (new) *and* 2021 (old) statements and absent
  from every Realised/Unrealised P/L report sampled:
  1. the **portfolio-valuation section** `VALORACIÓN DE CARTERA`
     (accent-tolerant `VALORACI.N DE CARTERA`), and
  2. the **admin/custody-fees concept** `Gastos de administración y depósito
     de valores negociables`.
  Two independent anchors keep the rule robust if either phrase drifts.
  **Do not** use the IP/ISGF-qualified header `VALORACIÓN DE CARTERA A EFECTOS
  DEL IP Y DEL ISGF` — the `ISGF` (and even the `A EFECTOS DEL IP` qualifier)
  is a 2022+ addition absent from the 2021 statement, so it misses older
  statements; and bare `ISGF` leaks into some daily P/L reports' boilerplate.
  Bank-interest is likewise unreliable (a daily Realised report carried
  `Intereses de cuentas bancarias`). Combine the two anchors with the shared
  `GANANCIAS Y PÉRDIDAS PATRIMONIALES` title.
- **Rule ordering:** the fiscal-statement rule must be listed **before**
  `TAX_REALISED_PL` in `PICTET_ES_RULES` — both fire the realised markers, so
  the statement rule (with the extra fees pattern) must reach the higher
  score / win the `>`-tie. Confirm the plain Realised report never reaches
  the statement rule's threshold.
- **Accent tolerance:** key on `P.RDIDAS` and tolerate the extractor's
  accent rendering, same as the P&L rules (`É` → `É`/`…` across generations).
- **Not pruned.** One statement per year, so retention is trivial — keep them
  all. `prune-tax-reports`'s `discover_reports` only recognises `Realised PL`
  / `Unrealised PL`, so `Fiscal statement` files are never in the retention
  set. The one change needed is the sweep guard (Stage 4).

## Stage 1 — doctype

- [x] **`models.py`** — add `TAX_FISCAL_STATEMENT` to `DocumentType`; add it to
      `NO_OUTPUT_DOCTYPES` (fourth family — archive-only tax reports).
      Docstring records the distinguishing markers (fees concept + the
      annual "Informe fiscal personas físicas" / "INFORME FISCAL" cover).

## Stage 2 — classifier rule

- [x] **`classifiers/rules.py`** — a new es+Pictet rule in `PICTET_ES_RULES`,
      **before** the `TAX_REALISED_PL` rule. Anchors: the portfolio-valuation
      section (`VALORACI.N DE CARTERA`) and the fees concept
      (`Gastos de administraci.n y dep.sito`) — the two statement-exclusive
      markers — plus the `GANANCIAS Y P.RDIDAS PATRIMONIALES` title, the
      `RENDIMIENTOS DEL CAPITAL MOBILIARIO` income section, and the
      `Informe fiscal` cover — tuned so a full match clears ~0.95 and beats
      the Realised rule on a statement while the Realised rule still wins on a
      daily report.
- [x] Verify against fixtures: the statement classifies as
      `TAX_FISCAL_STATEMENT`; a daily Realised report still classifies as
      `TAX_REALISED_PL`; ETE / Modelo 720 unaffected.

## Stage 3 — filing

- [x] **`archive.py`** — register `TAX_FISCAL_STATEMENT` in the tax-report
      label map so it files as `Fiscal statement <YYYYMMDD>.pdf` by its
      numeric as-of date. Reuse `_pictet_tax_as_of` (the realised branch:
      the `al`-end of `Del … al …` = 31 Dec). `filing_info` routes it
      through the existing tax-report branch; `destination_for` emits
      `<as-of-year>/tax/Fiscal statement <YYYYMMDD>.pdf`.

## Stage 4 — prune-sweep guard

- [x] **`tax_report_prune.py`** — teach `is_canonical_name` (or the sweep's
      candidate filter) to also treat `Fiscal statement <YYYYMMDD>.pdf` as a
      canonical tax-report name, so `_superseded_duplicates` doesn't classify
      a filed statement, resolve it back to its own path, see the dest
      exists, and sweep it into `_superseded/`. Add a test that a filed
      `Fiscal statement` survives a prune run untouched.

## Stage 5 — fixtures + tests

- [x] **Fixture** — one scrubbed `tests/fixtures/es/pictet/tax_fiscal_statement.txt`
      (trim to the cover + concept list + section headers; scrub name / NIF /
      account to placeholders; keep security names but drop real
      valuations). Auto-discovered by `conftest.discover_fixtures`.
- [x] **Tests** — `test_fixture_tree` asserts it classifies as
      `TAX_FISCAL_STATEMENT`; a filing test asserts it maps to
      `<year>/tax/Fiscal statement <YYYYMMDD>.pdf`; a regression asserts a
      Realised fixture still classifies as `TAX_REALISED_PL` (the rule
      ordering didn't steal it).

## Stage 6 — migration (one-off)

Reuses Stages 1–3, no new logic. **Deferred until the classifier is trusted.**

- [x] **Audit** — scan `<year>/tax/` (incl. `_superseded/`) for
      `Realised PL <YYYYMMDD>.pdf` files that actually carry the fees concept
      (i.e. are statements). Known: `2021/tax/Realised PL 20211231.pdf`.
      Check every year-end (`*1231`).
- [x] **Re-file** — run the filing pass so each misfiled statement moves to
      `Fiscal statement <YYYYMMDD>.pdf`. Confirm a genuine year-end Realised
      report (e.g. `Realised PL 20241231.pdf`, fees concept absent) is left
      as `Realised PL`.
- [x] **Import** — file the current `~/Downloads` zips' statement(s)
      (`Statement Capital gains…-20241231`) → `2024/tax/Fiscal statement
      20241231.pdf`, then prune the P&L dailies (the statement is untouched).

**Done when:** the annual statement classifies as `TAX_FISCAL_STATEMENT` and
files to `<year>/tax/Fiscal statement <YYYYMMDD>.pdf`; the daily Realised
report is unaffected; prune leaves statements untouched; misfiled statements
are reclassified; lints/types/tests green; `check_pii.py --all` passes.

## Risks / caveats

- **Marker robustness.** Both `VALORACIÓN DE CARTERA` and
  `Gastos de administración…` stayed Statement-exclusive across the sampled
  years (2021 + 2024), but the daily Realised reports' concept coverage has
  grown over generations (a 2025 daily carried the bank-interest concept, and
  bare `ISGF` leaks into some dailies). Validate the final anchor set against
  the fixture and a spread of real dailies before trusting it; keep the gate
  on the two section markers, not bank-interest / `ISGF`.
- **Rule-ordering fragility.** Both rules fire the realised markers; the
  statement must out-score the Realised rule on statements without the
  Realised rule mis-winning on dailies. This is the same tie-break pattern as
  `ESTADO_ANUAL` before `ESTADO_TRIMESTRAL` — lean on it, and cover both
  directions with fixtures.
- **Old-format statements.** Pre-"personas físicas" statements (e.g.
  2021-12-31) carry the all-caps `INFORME FISCAL` banner *and* the fees
  concept — so they look like a Realised report except for the fees line.
  The fees gate handles them; the migration audit catches the ones already
  misfiled.
- **PII.** Fixture scrubbed to placeholder body `123456` / NIF removed; no
  real valuations in the fixture beyond security names. `check_pii.py --all`
  must pass.

## Definition of done

Lints/types/tests clean; new classifier + filing + fixture/tests added;
goldens unaffected (no render path); `check_pii.py --all` passes; no
`Equity:Uncategorized` and no tax-pipeline coupling (archive-only); this
plan's stages ticked; docs updated (`architecture.md` filing section + CLI
reference note; this plan cross-linked from the P&L archive plan).
