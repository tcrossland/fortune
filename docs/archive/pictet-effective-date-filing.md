# Plan: date P&L tax reports by their effective date (filename), not the content label

*Shipped 2026-07-03 — implemented, tested, audited.* Account numbers / figures below are anonymised per the repo's
PII rules. No real balances, holdings, or the NIF appear here.

## Status: shipped

All stages done, lints/types/tests green (985 tests), PII clean. `filing_info`
now takes an optional `source_name` and dates a tax report by the filename's
effective date (`_effective_date_from_filename`), falling back to the content
scraper for dateless names and logging `archive.tax_report_date_mismatch` on a
disagreement; `file_documents` passes `pdf.name`. The prune legacy-sweep goes
through `file_documents`, so it's effective-date-keyed too (its test was
updated to model a duplicate by matching filename date).

**Audit result:** scanned 1,941 canonical tax files; **31** have a content
fiscal date ≠ filename date — and all 31 are the Oct–Nov 2023 unrealised batch
already re-filed under their correct effective dates (their content still
carries the frozen `Al 10.09.2023`). **Zero other stale-label reports**, so
nothing to re-file, and the annual-filing filename assumption held (no ETE /
720 / UK / statement showed a mismatch). Going forward, imports date by the
effective date automatically.

## Goal

The archive currently dates each Spanish tax report by an as-of date **scraped
from its content** (`_pictet_tax_as_of`). That content "fiscal reference"
date can be **stale**: Pictet froze the `Al 10.09.2023` label on a run of
Oct–Nov 2023 unrealised reports while continuing to re-value them live, so the
pipeline collapsed 31 distinct daily valuations onto one canonical name
(`Unrealised PL 20230910.pdf`) — a silent data loss we had to unpick by hand.

The **effective date** (= Publication date GVA on Pictet's portal, the
`-<YYYYMMDD>` suffix on every download filename) is authoritative and never
drifted. Key the filing date on it, with the content date as a fallback.

## Why this is safe (and why it supersedes the "stale-label guard")

- For a normal report the effective date **equals** the content fiscal date
  (`Realised PL 20231229` ↔ `Del 01.01 al 29.12.2023`). So the change is a
  **no-op everywhere except the stale-label anomaly**, where it produces the
  correct date. It only ever changes a currently-wrong result.
- It removes reliance on the fragile, doctype-specific content-date scrapers
  (numeric `Del … al …`, `Al …`, prose `31 diciembre`, `5 April`) — those
  become fallback-only.
- It would have preserved the 31 unrealised valuations automatically; last
  turn's proposed stale-label *guard* is unnecessary once the effective date
  is the primary key.

## Design

- **Filename-primary, content-fallback.** In the tax-report branch of
  `filing_info`, take the as-of date from the **source filename's** trailing
  `-<YYYYMMDD>` (tolerating a `-(N)` re-cut suffix, and the canonical
  `<stem> <YYYYMMDD>.pdf` form on re-imports). Fall back to
  `_pictet_tax_as_of(text, doc_type)` only when the filename carries no
  parseable date (some legacy names are dateless, e.g.
  `Tax - Realised PL report-.pdf`).
- **Cross-check + warn.** When both the filename date and the content date are
  present and **disagree**, use the filename (effective) date and log a WARN
  (that disagreement is the stale-label signal — worth surfacing, not hiding).
- **Thread the source name.** `file_documents` already holds the source path
  at the `filing_info` call site (`archive.py:499`); pass `pdf.name` through a
  new `source_name` parameter on `filing_info` (kept optional so existing
  callers/tests that pass only text fall back to content).
- **Validate the extracted date** (real `date(YYYY, MM, DD)`), so a stray
  8-digit token can't produce a bogus filing date.

## Scope (decided): all tax reports

Filename-primary dating applies to **every** tax report — the P&L pair plus
the fiscal statement, ETE, Modelo 720 and UK income & CG. Uniform, and a
no-op for the annuals (their filename `-<YYYYMMDD>` already equals their
content 31-Dec / 5-Apr date), so it simply drops the reliance on the
prose/`31 diciembre`/`5 April` content scrapers there too.

**Load-bearing assumption to verify in the audit:** the annual filenames
encode the *fiscal reference* date (31 Dec / 5 Apr), not the *publication*
date (which for the ETE is ~10 Jan of the following year). Specimens support
it (`ETE-20221231`, `Income and capital gains UK-20250405`); if the audit
finds any annual named by its publication date, fall those back to content
(the scrapers stay in place as the fallback, so this degrades safely).

## Stages

- [x] **`archive.py`** — add `_effective_date_from_filename(name)` (trailing
      `-?(\d{8})(?:[-\s]\(\d+\))?\.pdf$`, validated to a real date). Add an
      optional `source_name` param to `filing_info`; in the tax-report branch
      prefer the filename date, fall back to `_pictet_tax_as_of`, and WARN on a
      filename/content mismatch. Pass `pdf.name` from `file_documents`.
- [x] **Tests** — `filing_info` with a `source_name` whose date differs from
      the content date files by the filename date (the stale-label case); a
      dateless `source_name` falls back to content; a re-import of a canonical
      `Unrealised PL 20231005.pdf` keeps its date. Keep the existing
      content-fallback tests green.
- [x] **Audit (one-off)** — scan the archive for tax reports whose content
      fiscal date ≠ their (filename-derived) effective date, to find any other
      stale-label batches beyond the Oct–Nov 2023 unrealised one already
      fixed. Re-file any found. (Expected: few — the anomaly looked isolated.)

**Done when:** P&L (and, if scope (b), all tax) reports file under their
effective date; normal reports are unchanged; a filename/content mismatch is
logged; dateless legacy names still fall back to content; lints/types/tests
green; `check_pii.py --all` passes.

## Risks / caveats

- **Filename must reach `filing_info` intact.** Zip members and loose
  downloads carry the `-<YYYYMMDD>` name; `source_pdfs` preserves member
  basenames; canonical re-imports carry the date in the stem. Only genuinely
  dateless names hit the fallback. Confirm no source path strips the name.
- **Annual-filing filename semantics (scope b).** Assumes the ETE / 720 / UK /
  statement filenames encode the fiscal reference date (31 Dec / 5 Apr), not
  the *publication* date (which for the ETE is ~10 Jan of the next year).
  Specimens support this (`ETE-20221231`), but the audit must confirm before
  trusting filename-primary for the annuals; otherwise keep them content-first
  (scope a).
- **Realised content-dedup is orthogonal** — identical-content realised
  reports across effective dates stay redundant (realised gains are fixed);
  that's handled by prune's month-end retention / content dedup, not by this
  dating change. Unrealised is where effective-date dating earns its keep.
- **Doesn't touch the classifier** — doctype detection is unchanged; this is
  purely `filing_info` / `_pictet_tax_as_of`.

## Definition of done

Lints/types/tests clean; filename-primary dating + content fallback + mismatch
warning added; audit run and any stale-label reports re-filed; no render-path
change (archive-only); `check_pii.py --all` passes; docs updated (architecture
filing section notes the effective-date rule); this plan's stages ticked.
