#!/usr/bin/env bash
# Convenience wrapper for the rebuild pipeline. The list of source PDFs
# and the post-processing toggles live in ``banking-pipeline.toml``
# (gitignored — copy from ``banking-pipeline.example.toml`` and edit
# for your local folder layout). Pass ``--dry-run`` to preview without
# touching the filesystem.
set -euo pipefail
exec uv run banking-pipeline rebuild "$@"
