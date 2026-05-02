"""Wrapper around the ``bean-check`` CLI binary.

We shell out rather than linking against ``beancount`` itself because
``beancount`` is **GPL-2.0** — see the README's licence story for the
"why we don't import beancount" rationale. ``bean-check`` is a normal
program invocation and doesn't bind this codebase to GPL.

The wrapper captures stderr, surfaces failures with the rebuild's own
context (which ledger was checked, exit code, distilled error count),
and falls back gracefully when the ``bean-check`` binary isn't
installed — distinct from "ran and reported errors", since the latter
is a real ledger problem the user should fix while the former is a
setup issue ("``uv tool install beancount``").
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single ``bean-check`` invocation.

    ``returncode`` mirrors the underlying process: ``0`` is a clean run,
    nonzero is an error report. ``stderr`` is the captured combined
    stderr+stdout so the caller can echo it directly to the user; we
    don't try to parse it because beancount's error format is rich and
    re-formatting would lose information. ``binary_missing`` is set
    when ``bean-check`` itself wasn't found on ``PATH`` — the caller
    can branch on this to print an install hint instead of a misleading
    "check failed" message.
    """

    returncode: int
    stderr: str
    binary_missing: bool = False

    @property
    def ok(self) -> bool:
        """True when ``bean-check`` ran cleanly (or wasn't installed).

        The "missing binary" branch returns True on purpose: a missing
        ``bean-check`` means the user opted out of validation by not
        installing it, which we report as a warning rather than a
        rebuild failure. Set the rebuild config's ``[post.check]
        enabled = false`` to turn the warning off entirely.
        """

        return self.returncode == 0 or self.binary_missing


def find_bean_check() -> Path | None:
    """Return the path to the ``bean-check`` binary, or ``None`` if absent.

    Plain ``shutil.which`` lookup against ``PATH`` — same logic ``uv
    run`` uses to resolve installed tools, and the same precedence
    interactive shells apply. Returns ``None`` rather than raising so
    callers can branch on installed-vs-not without a try/except.
    """

    found = shutil.which("bean-check")
    return Path(found) if found else None


def run_bean_check(
    ledger: Path,
    *,
    strict: bool = False,
    extra_args: tuple[str, ...] = (),
) -> CheckResult:
    """Run ``bean-check`` on ``ledger`` and return the combined result.

    ``strict`` adds ``-w`` so ``bean-check`` treats warnings as errors
    (returns nonzero if any warning fires). ``extra_args`` is passed
    through verbatim — useful for callers that want to add
    ``-v`` / ``--auto`` / etc. without us tracking every flag.

    Returns a :class:`CheckResult` rather than raising. The caller
    decides whether a nonzero return is fatal — the rebuild flow
    surfaces the error and exits with the same code; ad-hoc CLI
    invocations may prefer to print and continue. A missing
    ``bean-check`` binary returns ``binary_missing=True`` and a
    helpful stderr message instead of crashing.
    """

    binary = find_bean_check()
    if binary is None:
        return CheckResult(
            returncode=0,
            stderr=(
                "bean-check binary not found on PATH; ledger validation "
                "skipped. Install with `uv tool install beancount` (the "
                "GPL-2.0 licence applies to bean-check itself, not to "
                "this codebase, since we shell out rather than link)."
            ),
            binary_missing=True,
        )

    cmd: list[str] = [str(binary)]
    if strict:
        cmd.append("-w")
    cmd.extend(extra_args)
    cmd.append(str(ledger))

    # Capture combined stdout+stderr — beancount writes most diagnostics
    # to stderr but some to stdout depending on version, so merging is
    # the simplest way to give the user a complete report.
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (proc.stderr or "") + (proc.stdout or "")
    return CheckResult(returncode=proc.returncode, stderr=output)
