"""UK tax-reporting computation off the JSONL transaction sidecars.

Self-contained: reads the structured sidecars (and the commodity
metadata TOML), applies UK tax-year boundaries and the section 104 /
same-day / 30-day share-matching rules, and produces SA106 (foreign
income) / SA108 (capital gains) inputs. Never imports ``beancount`` —
all inputs are the JSONL substrate from
:mod:`banking_pipeline.transaction_sidecar`.
"""
