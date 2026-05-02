"""Jinja templates for doctypes that don't warrant a Python builder.

The templates are loaded via :class:`jinja2.FileSystemLoader` so they
remain diffable / syntax-highlightable / easy to golden-test in
isolation. ``trim_blocks`` / ``lstrip_blocks`` are intentionally left
off — historically those defaults swallowed the newline after a block
tag and collapsed multi-leg postings onto a single line, producing
output ``bean-check`` rejected. None of today's templates use block
tags, but the safe-default stays as a guard against the next template
that adds an inline conditional.
"""
