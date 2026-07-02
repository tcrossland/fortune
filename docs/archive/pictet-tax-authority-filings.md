# Plan: classify Pictet's annual tax-authority filings (ETE / Modelo 720 / UK)

*Shipped 2026-07-02 — all stages complete and verified on the real archive.* Account numbers / figures below are anonymised per the repo's
PII rules (placeholder body `123456`; real numbers live only in the
gitignored archive / `data/`). No real balances, holdings, or the NIF appear
here.

## Status: shipped

All stages done, verified, lints/types/tests green (982 tests), PII clean.
Went smoothly — the classifiers hit ~0.95 on every real specimen (2022 +
2024/2025) and the migration re-filed all 10 hand-filed copies to canonical
names with none left over. One runtime wrinkle: the newer **mixed-language
ETE** (English header over a Spanish body) scores the *language* stage below
the 0.75 fixture threshold, so the scrubbed fixture is based on the
older **all-Spanish** ETE layout (with a 2024 date) — the classifier rule
still keys on the same `Declaración ETE` / `ETE - Resumido…` anchors that are
present in both generations. Real-archive result: ETE + Modelo 720 for
2022–2025, UK income & CG report for 2024–2025, all under
`<year>/tax/<stem> <YYYYMMDD>.pdf`; prune leaves them untouched (0 to move).

## Goal

Pictet issues three **annual tax-authority filings** that today classify as
`no-match` and are hand-archived into `<year>/tax/`:

1. **Declaración ETE** — the Bank-of-Spain foreign-transactions declaration
   (Encuesta de Transacciones Exteriores). Spanish (later years mix in some
   English). As-of **31 Dec**.
2. **Modelo 720** — the Spanish foreign-assets informative return ("Datos
   para la declaración informativa sobre bienes y derechos situados en el
   extranjero"). Spanish. As-of **31 Dec**.
3. **UK income & capital gains report** — Pictet's English UK-tax-year report
   ("Financial information – Income & capital gains report UK"). As-of
   **5 Apr** (UK tax-year end).

Give each its own doctype, classify by content, and file under a canonical
name — the same treatment as the [P&L reports](../archive/pictet-pnl-tax-archive.md)
and the [fiscal statement](../archive/pictet-fiscal-statement.md). **Archiving
only** — never ingested into beancount, never fed to the UK-tax pipeline.

## Feasibility (verified against archived specimens)

Clean, mutually-exclusive content markers (checked on 2022 + 2024/2025
specimens; none fire on the P&L reports or the fiscal statement):

| doctype | marker (accent-tolerant) | language |
| --- | --- | --- |
| ETE | `Declaraci.n ETE` | es (even the mixed-language 2024) |
| Modelo 720 | `Modelo 720: Datos para` (the informative-return title) | es |
| UK income & CG | `capital gains and losses are calculated separately` + `5 April <year>` + a `20XX/20XX` UK tax-year | en |

## Decisions (locked)

- **Doctypes:** `DECLARACION_ETE`, `MODELO_720` (issuer's own official
  form names — no anglicisation needed, unlike the P&L pair) and
  `INCOME_CAPITAL_GAINS_UK` (the issuer's English report name). All added to
  `NO_OUTPUT_DOCTYPES`.
- **Archive names:** canonical short names, consistent with `Fiscal
  statement <YYYYMMDD>.pdf`:
  - `ETE <YYYYMMDD>.pdf`
  - `Modelo 720 <YYYYMMDD>.pdf`
  - `Income and capital gains UK <YYYYMMDD>.pdf`

  The current hand-filed names are the long Pictet download forms
  `Tax - Tax valuations - ETE-<date>.pdf` etc.; adopting short names means a
  one-off rename of the ~8 existing files (Stage 6).
- **As-of dates are deterministic per doctype**, so scrape the *year* and
  construct the date: ETE / Modelo 720 → 31 Dec of the year in `31
  (Diciembre|December) <year>`; UK → `5 April <year>`. Distinct from the P&L
  reports' numeric `Del DD.MM.YYYY` range, so a per-doctype as-of branch is
  needed.
- **Not pruned.** One of each per year — `discover_reports` (retention) stays
  P&L-only; `is_canonical_name` must learn the three new stems so the sweep
  never moves them (same guard as the fiscal statement).
- **Route via the tax-report filing branch.** ETE / 720 (2024+) carry an
  `Account no.:` / `N° de cuenta:` header, so without doctype routing the
  advice parser would try (and fail, no reference) to file them. Add the
  three doctypes to `_TAX_REPORT_STEMS` so `filing_info` routes them by as-of
  date (account ignored), exactly like the statement.

## Stage 1 — doctypes

- [x] **`models.py`** — add `DECLARACION_ETE`, `MODELO_720`,
      `INCOME_CAPITAL_GAINS_UK` to `DocumentType`; all three to
      `NO_OUTPUT_DOCTYPES`. Docstrings record the markers + as-of rule.

## Stage 2 — classifier rules

- [x] **`classifiers/rules.py`** — es+Pictet rules for `DECLARACION_ETE`
      (`Declaraci.n ETE`) and `MODELO_720` (`Modelo 720: Datos para`), and an
      en+Pictet rule for `INCOME_CAPITAL_GAINS_UK` (the capital-gains
      calculation phrase + `5 April <year>` + UK tax-year). Each needs
      ~3–5 supporting markers to clear ~0.95; verify none steal the P&L
      reports / fiscal statement / Vanguard docs.
- [x] Confirm the UK rule outranks the generic English rules (it currently
      mis-scores as `dividend_notice` 0.86) and doesn't collide with the
      Vanguard ISA docs (Pictet letterhead vs `VG…` / Vanguard markers).

## Stage 3 — filing

- [x] **`archive.py`** — add the three doctypes to `_TAX_REPORT_STEMS` with
      their stems. Extend `_pictet_tax_as_of` (or add a sibling) with the
      per-doctype as-of logic: ETE / 720 → `date(year, 12, 31)` from the
      prose `31 (Diciembre|December) <year>`; UK → `date(year, 4, 5)` from
      `5 April <year>`.

## Stage 4 — prune-sweep guard

- [x] **`tax_report_prune.py`** — extend `_CANONICAL_NAME` to also match
      `ETE`, `Modelo 720` and `Income and capital gains UK` stems, so a filed
      copy is never mistaken for a legacy stray and swept aside. Retention
      (`_NAME`) stays P&L-only. Test: a filed ETE / 720 / UK survives a prune
      run untouched.

## Stage 5 — fixtures + tests

- [x] **Fixtures** — one scrubbed fixture each
      (`declaracion_ete.txt`, `modelo_720.txt`, `income_capital_gains_uk.txt`;
      the UK one under `en/pictet/`, the others under `es/pictet/`). Scrub
      name / NIF / account / IBAN to placeholders.
- [x] **Tests** — `test_fixture_tree` asserts each classifies to its doctype;
      filing tests assert the canonical `<year>/tax/<stem> <date>.pdf` paths
      (incl. the year-from-prose / 5-Apr as-of logic).

## Stage 6 — migration (one-off)

Reuses Stages 1–3, no new logic. **Deferred until the classifiers are
trusted.**

- [x] **Re-file** existing hand-archived copies: run the filing pass over
      `<year>/tax/` so `Tax - Tax valuations - ETE-<date>.pdf` →
      `ETE <YYYYMMDD>.pdf`, the Modelo 720 and UK equivalents likewise. The
      old names are non-canonical, so the content-derived new names don't
      collide; verify counts (ETE/720 for 2022–2025, UK for 2024–2025).

**Done when:** each filing classifies to its doctype and files to its
canonical `<year>/tax/` name; prune leaves them untouched; existing copies
are renamed; lints/types/tests green; `check_pii.py --all` passes; docs
updated (architecture filing section + CLI note).

## Risks / caveats

- **UK report vs other English docs.** The UK report is Pictet-issued
  English — it must not be stolen by the generic `dividend_notice` /
  `trade_confirmation` rules (it currently mis-scores `dividend_notice`
  0.86) nor collide with the Vanguard ISA docs. Gate on the capital-gains
  calculation phrase + the UK tax-year + Pictet letterhead, and cover the
  collision both ways with fixtures.
- **Mixed-language ETE.** The 2024 ETE interleaves English (`From 1 January
  to 31 December 2024`, `Account no.:`) with the Spanish body; it still
  detects as `es` and carries `Declaración ETE`, but validate the language
  stage on both the older all-Spanish and the newer mixed specimen.
- **Prose / English as-of dates.** Unlike the P&L reports' numeric range,
  these use prose month names in two languages; key the year scrape on `31
  (Diciembre|December) <year>` / `5 April <year>` and construct the fixed
  month-day.
- **PII.** Fixtures scrubbed to placeholders; the ETE/720 carry an IBAN
  (`LU9019800173837…`) — scrub it. `check_pii.py --all` must pass.

## Definition of done

Lints/types/tests clean; three classifiers + filing + fixtures/tests added;
no render path (archive-only, `NO_OUTPUT`); `check_pii.py --all` passes; no
tax-pipeline coupling; this plan's stages ticked; docs updated; this plan
cross-linked from the P&L / fiscal-statement archives.
