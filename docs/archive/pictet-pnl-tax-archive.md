# Plan: import, name, and prune Pictet P&L tax reports

*Shipped 2026-07-02 — all three stages complete and verified on the real
archive.* Account numbers below are anonymised per the repo's PII rules
(placeholder body `123456`; the real number lives only in the gitignored
archive / `data/`). No real balances, holdings, or the NIF appear here.

## Status: shipped

All stages done, verified, lints/types/tests green, PII guard clean. Key
deviations from the original sketch, and why:

- **Accent-tolerant classifier anchors.** The PDF extractor renders `É`
  differently across report generations (`É` on 2023+, `…` on 2022-era), so
  the rules key on `P.RDIDAS` (single-char wildcard), the same trick the
  filing regexes already use for `transacci.n`. Realised vs unrealised split
  on `INFORME FISCAL` vs `SIMULACI.N FISCAL` (+ `NO REALIZADAS` / `latentes`
  / the date-shape).
- **`source_globs` list, not a single extra glob.** `[import]` grew a
  `source_globs` list (appended to the primary `source_glob`) so the loose
  tax-report glob composes with — rather than replaces — the `files-*.zip`
  glob. Reports may arrive loose *or* zipped; zipped copies were already
  handled.
- **The prune sweep is content-based, not filename-based.** The legacy
  URL-encoded filenames encode the *download* date, not the report's as-of —
  e.g. 31 files named `…-2023100N.pdf` are all re-downloads of the one
  10.09.2023 report. So the duplicate sweep reuses `archive.file_documents`
  in dry-run (its `"skip"` status = "canonical already exists" = a redundant
  copy). A filename-based twin check would have missed all 62 of 2023's dups.
- **Three legacy name variants, all handled by content.** Besides
  `Tax - … PL report-` and the URL-encoded `0173837-Tax+-+…+P%2FL+report-`,
  the year-end realised report also arrived as
  `Tax - Statement Capital gains losses other income %26 tax info-`. Content
  classification files all three canonically; no per-variant logic.
- **Filing by content year created a `2021/tax/`.** The 2021 year-end
  realised report physically sat in `2022/tax/` but has a 2021 as-of, so it
  filed to a new `2021/tax/Realised PL 20211231.pdf` — correct (file by
  as-of, not source folder).

Real-archive result (verified, 948 files conserved — nothing lost): 2021
keep 1; 2022 keep 25 (12 realised incl. year-final + 13 unrealised incl. the
5-Apr anchor), 466 superseded; 2023 keep 21, 432 superseded; ETE/720 and the
2024 zip untouched. Re-running prune is a no-op.

## Goal

Pictet issues two Spanish-locale IRPF tax reports on most booking-event
days — **Realised** ("Ganancias y pérdidas patrimoniales") and
**Unrealised** ("…no realizadas"). Today they are neither auto-filed nor
consumed: the `[import]` step leaves them `no-match` (they carry no
`N° de cuenta:` header, and are neither an advice nor a periodic valuation
statement), so they've been dropped into `<year>/tax/` **by hand under
three inconsistent names** — and only for some years (2022–23 hold ~490
files *each*; 2021, 2024, 2025, 2026 hold none).

Three outcomes:

1. **Auto-file** every downloaded Realised/Unrealised P&L report into
   `<as-of-year>/tax/` under one canonical name, as part of `rebuild`.
2. **A retention policy** that keeps a principled subset (month-end +
   year-end anchors) and moves the daily noise aside — applied on demand.
3. **Normalise + prune the existing archive** (the ~950 legacy files)
   with the same two mechanisms, no bespoke migration logic.

This is an **archiving** change only. These reports are **not** ingested
into beancount and **not** fed to the tax pipeline — they are a filed
reference source (and the future cross-check target tracked in
[../backlog.md](../backlog.md), "Pictet tax reports as a reconciliation
target"). Spanish FIFO/EUR figures must never reach the UK tax math.

## Decisions (locked)

- **Naming:** `Realised PL <YYYYMMDD>.pdf` / `Unrealised PL <YYYYMMDD>.pdf`
  under `<as-of-year>/tax/`. `YYYYMMDD` is the **as-of date** (unrealised
  `Al DD.MM.YYYY`; realised the `al`-end of `Del … al …`). Title-case +
  `YYYYMMDD` mirrors the existing `Valuation <period> <YYYYMMDD>.pdf`
  convention: sorts chronologically, globs cleanly (`Realised*` /
  `Unrealised*`), and gives idempotent skip-if-exists + same-day-duplicate
  collapse for free. ASCII only — no `&`, no `%2F`.
- **Doctypes:** `TAX_REALISED_PL` / `TAX_UNREALISED_PL` (English — a
  deliberate exception to the issuer-vocabulary rule, since the chosen
  filenames are English and the pair reads clearer than
  `INFORME_FISCAL_(NO_)REALIZADO`). Both added to `NO_OUTPUT_DOCTYPES`.
- **Prune = move, not delete:** pruned files move to
  `<year>/tax/_superseded/`; recoverable, and Dropbox keeps trash/version
  history on top. A later `--purge` (out of scope for v1) can empty it.
- **Prune is a manual command**, dry-run by default — *not* wired into
  `rebuild` (see the idempotency caveat), until trusted.
- **Scope is Realised/Unrealised P&L only.** Other `tax/` docs
  (`ETE`, `Modelo 720`) classify as not-our-doctype and are left untouched.

## Retention policy (what the prune keeps)

Deterministic over the set of dated files, grouped by type + **calendar**
year (the Spanish IRPF year; realised reports run `01.01 → as-of`):

- **Realised** (cumulative within the year): keep the **latest as-of per
  month** (restatement checkpoint) + the **year's final** report. ≤ ~12/yr.
- **Unrealised** (point-in-time snapshot): keep the **latest as-of per
  month**, plus the snapshot **on-or-before 5 Apr** (UK tax-year-end
  anchor; the Dec month-end already covers 31 Dec / calendar-year-end).
  ≤ ~13/yr.
- Everything else → `_superseded/`.

"Latest-per-month" resolves to the last booking-day report of each month —
no exact month-end date is required, since reports only exist on activity
days. Net: ~490/yr → ~25/yr.

*(Deferred, phase-2 idea:* also keep the first report of either type dated
strictly after each disposal, for a tight audit trail. Disposal dates are
available from the realised reports' `FECHA DE VENTA` column or the
sidecars. Left out of v1 to keep selection stateless.)*

## Stage 1 — recognise + file (going forward)

Mirrors the existing advice/statement filing in `archive.py`; data-driven.

- [x] **`models.py`** — add `TAX_REALISED_PL` / `TAX_UNREALISED_PL` to
      `DocumentType`; add both to `NO_OUTPUT_DOCTYPES`. Docstrings record
      the distinguishing PDF markers.
- [x] **`classifiers/rules.py`** — es + Pictet rules: title
      `GANANCIAS Y PÉRDIDAS PATRIMONIALES` gates the pair;
      `NO REALIZADAS` present → unrealised, else realised. Must **not**
      fire on `ETE` / `Modelo 720` (they lack the title).
- [x] **`archive.py`** — a **third filing shape** alongside advice +
      statement. `ParsedFields`/`FilingInfo` gain a tax-report variant
      (no account, no reference, carries `as_of` + a `Realised`/`Unrealised`
      label). A new scraper reads the **numeric** as-of date (`Al DD.MM.YYYY`
      / the `al`-end of `Del DD.MM.YYYY al DD.MM.YYYY`) — distinct from the
      existing prose `_pictet_as_of` (`AL 30 junio 2026`). `filing_info`
      routes the tax variant before the advice/statement branches;
      `destination_for` emits
      `<as-of-year>/tax/<Realised|Unrealised> PL <YYYYMMDD>.pdf`
      (no account segment).
- [x] **`banking-pipeline.toml`** `[import]` — the reports arrive **either
      loose or inside a `files-*.zip`**. Zipped copies are already picked up
      by the existing `source_glob = "~/Downloads/files-*.zip"` + `*.pdf`
      pattern (filing derives everything from content, so the URL-encoded
      member names don't matter). To also catch the **loose** copies, add a
      second source glob (e.g. `~/Downloads/0173837-Tax*P?L report*.pdf`);
      the config grows a `source_globs` list so more than one pattern can be
      configured without dropping the zip glob.
- [x] **Fixtures + goldens** — one scrubbed Realised + one Unrealised
      fixture; a filing test asserts each maps to its canonical
      `<year>/tax/…` path (and same-day duplicates collapse via
      skip-if-exists).

**Done when:** a fresh Realised/Unrealised download is filed to the correct
`<year>/tax/<Type> PL <YYYYMMDD>.pdf` by `rebuild`, existing destinations
are never overwritten, and `ETE`/`720`/advices/statements are unaffected.

## Stage 2 — `prune-tax-reports` command

- [x] New CLI `banking-pipeline prune-tax-reports [--apply]` (module under
      `cli/`), **dry-run by default**: prints the keep / move plan per
      year + type. `--apply` moves pruned files to `<year>/tax/_superseded/`
      (created on demand; never deletes).
- [x] Pure selection function (unit-testable, no I/O): given
      `[(type, as_of, path)]`, return the retained set per the policy above.
      Covers the month-end grouping, the realised year-final, and the
      unrealised 5-Apr / Dec anchors.
- [x] Only touches files matching the P&L naming convention; `_superseded/`
      itself, `ETE`, `Modelo 720`, and any unrecognised file are skipped.
- [x] Tests: selection over a synthetic dated set (dailies across several
      months + a year boundary) keeps exactly the expected subset; the
      command is idempotent (a second run moves nothing).

**Done when:** a dry-run prints an accurate plan, `--apply` converges the
tree to the policy subset, and re-running is a no-op.

## Stage 3 — normalise + prune the existing archive (one-off)

Reuses Stage 1 + 2, no new logic.

- [x] **Rename:** run the filing pass over existing `<year>/tax/*.pdf`.
      Names derive from content, so the three legacy variants
      (`Tax - Realised PL report-…`, URL-encoded
      `0173837-Tax+-+…+P%2FL+report-…`, etc.) normalise to
      `<Type> PL <YYYYMMDD>.pdf`; the duplicate-per-day legacy copies
      collapse via skip-if-exists; `ETE`/`720` are left as-is. Verify a
      legacy sample first — 2022–23 layouts differ slightly from 2026
      (e.g. no "Informe fiscal personas físicas" line), but the title +
      `NO REALIZADAS` + numeric-date anchors hold (checked 2026-07-02).
- [x] **Prune:** `prune-tax-reports --apply` over the normalised tree.
- [x] Spot-check counts (2022/2023 ~490 → ~25 each) and that a retained
      realised year-final + unrealised anchors are present.

**Done when:** every year's `tax/` P&L set is canonically named and pruned
to policy, with the remainder in `_superseded/`.

## Risks / caveats

- **Idempotency churn.** Import over-collects (files every daily); prune
  trims. Re-importing an old batch re-adds dailies the next prune removes —
  convergent end state, but churn. Hence prune stays **manual**, not in
  `rebuild`. (A future `pruned-dates` manifest could make import skip them.)
- **Legacy-format drift.** Older reports vary in header text; key the
  classifier/parser on the stable anchors (title, `NO REALIZADAS`, numeric
  date), not the cover line. Validate against a 2022 and a 2023 sample
  before the bulk rename.
- **Second same-day run.** The portal occasionally re-cuts a report later
  the same day (e.g. a 15:50 realised after an 11:05 one). Same as-of date
  → same name → skip-if-exists keeps the first filed; content is
  equivalent. Acceptable.
- **PII.** Fixtures scrubbed to placeholder body `123456` / the report NIF
  removed; `scripts/check_pii.py --all` must pass. No real figures in
  goldens beyond security names/amounts (allowed).

## Definition of done

Lints/types/tests clean; new filing + selection tests added; goldens
diff clean; `check_pii.py --all` passes; no `Equity:Uncategorized` and no
tax-pipeline coupling (these reports never feed it); this plan's stages
ticked; docs updated (`architecture.md` filing section + the CLI reference;
the backlog reconciliation-target bullet cross-links here).
