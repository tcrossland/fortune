#!/usr/bin/env python3
"""Pre-commit PII guard.

Blocks a commit when staged content carries the kinds of personal /
account identifiers this repo must never hold. It works by *shape* and
by an allow-list of the known scrubbed placeholders — so the guard
itself never embeds a real account number (which would just re-leak it).

What it catches
---------------
- Pictet portfolio accounts ``K-NNNNNN.NNN`` / ``KNNNNNNNNN`` (and ``P``)
  whose 6-digit body isn't an allow-listed placeholder.
- Vanguard platform accounts ``VG#######`` that aren't the placeholder.
- UK National Insurance numbers (``AB123456C`` shape) other than the
  fixtures' placeholder.
- Anything matching a regex in an optional, git-ignored ``.pii-deny``
  file at the repo root — the place to hard-block exact literals (a real
  surname, a specific IBAN, a real balance) without committing them.

Usage
-----
- As a hook: invoked by ``scripts/git-hooks/pre-commit`` over the staged
  files (``git config core.hooksPath scripts/git-hooks`` to install).
- Ad hoc: ``python3 scripts/check_pii.py --all`` scans the whole working
  tree (handy to verify a history scrub left nothing behind).

Exit status is non-zero when anything is found, with ``file:line`` and the
rule that fired. Stdlib only — no uv / venv needed at commit time.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# --- Allow-listed placeholders (the scrubbed forms already in the repo) ---
# Both dummy conventions already in the fixtures: 999999 (most EN
# fixtures) and 123456 (ES fixtures, buy/sell-shares). The real one is
# neither.
ALLOWED_PORTFOLIOS = {"123456", "999999", "000000"}
ALLOWED_VG = {"0000000"}                 # VG0000000
ALLOWED_NI = {"AB123456C"}               # the ISA-declaration fixture's NI

# --- Shape patterns (no real values embedded) -----------------------------
_PICTET = re.compile(r"\b[KP]-?(\d{6})[.]?\d{3}\b")
_VANGUARD = re.compile(r"\bVG(\d{7})\b")
_NI = re.compile(r"\b[A-Z]{2}\d{6}[A-D]\b")

_DENY_FILE = ".pii-deny"


def _repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    )
    return Path(out.stdout.strip())


def _staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM", "-z"],
        capture_output=True, text=True, check=True,
    )
    return [p for p in out.stdout.split("\0") if p]


def _staged_content(path: str) -> str | None:
    out = subprocess.run(
        ["git", "show", f":{path}"], capture_output=True
    )
    if out.returncode != 0:
        return None
    try:
        return out.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None  # binary — skip


def _load_deny_patterns(root: Path) -> list[re.Pattern[str]]:
    deny = root / _DENY_FILE
    if not deny.is_file():
        return []
    patterns: list[re.Pattern[str]] = []
    for raw in deny.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line))
        except re.error as exc:  # noqa: PERF203 — startup-time, fine
            print(f"{_DENY_FILE}: bad regex {line!r}: {exc}", file=sys.stderr)
    return patterns


def _scan_line(line: str, deny: list[re.Pattern[str]]) -> list[str]:
    hits: list[str] = []
    for m in _PICTET.finditer(line):
        if m.group(1) not in ALLOWED_PORTFOLIOS:
            hits.append(f"Pictet account {m.group(0)!r}")
    for m in _VANGUARD.finditer(line):
        if m.group(1) not in ALLOWED_VG:
            hits.append(f"Vanguard account {m.group(0)!r}")
    for m in _NI.finditer(line):
        if m.group(0) not in ALLOWED_NI:
            hits.append(f"NI-number shape {m.group(0)!r}")
    for pat in deny:
        if pat.search(line):
            hits.append(f".pii-deny match /{pat.pattern}/")
    return hits


def _iter_targets(root: Path, scan_all: bool) -> list[tuple[str, str]]:
    """Return ``(path, content)`` for each file to scan."""
    if scan_all:
        # Tracked files only — gitignored local data (the real ledger,
        # reports) is intentionally never committed, so it's out of scope.
        listed = subprocess.run(
            ["git", "ls-files", "-z"], capture_output=True, text=True, check=True,
        )
        out: list[tuple[str, str]] = []
        for rel in listed.stdout.split("\0"):
            if not rel:
                continue
            try:
                out.append((rel, (root / rel).read_text("utf-8")))
            except (UnicodeDecodeError, OSError):
                continue
        return out
    targets: list[tuple[str, str]] = []
    for path in _staged_files():
        content = _staged_content(path)
        if content is not None:
            targets.append((path, content))
    return targets


def main(argv: list[str]) -> int:
    scan_all = "--all" in argv
    root = _repo_root()
    deny = _load_deny_patterns(root)

    violations: list[str] = []
    for path, content in _iter_targets(root, scan_all):
        # The guard's own allow-list/placeholder file is exempt.
        if path in {"scripts/check_pii.py", f"{_DENY_FILE}.example"}:
            continue
        for n, line in enumerate(content.splitlines(), start=1):
            for hit in _scan_line(line, deny):
                violations.append(f"  {path}:{n}: {hit}")

    if violations:
        print("PII guard: blocked — possible personal/account data:\n")
        print("\n".join(violations))
        print(
            "\nIf this is a false positive, extend the allow-lists in "
            "scripts/check_pii.py, or bypass once with `git commit "
            "--no-verify`.",
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
