"""Generic context-management principles, shared by all dialects.

This text is embedded by each database's specific prompt, which adds its own syntax and
idioms on top of it.
"""

from __future__ import annotations

CONTEXT_PRINCIPLES = """\
Context is a scarce resource: you must NEVER see raw records unless strictly necessary. Bulk \
data must be treated as references, not as content. Always apply this defense hierarchy, from \
the most important to the most specific principle.

1. Push work into the database, not into the context. Have the database aggregate, filter and \
   project: instead of extracting all rows and aggregating them yourself, ask the database for \
   already-aggregated results (COUNT, SUM, AVG, GROUP BY) and only the columns you need. This \
   alone resolves most cases.

2. Always limit and paginate. The tools impose an automatic LIMIT even if you don't specify \
   one, and return the total count plus a cursor for subsequent pages. Ask for the next page \
   only if truly necessary; prefer keyset pagination (WHERE id > :last_id).

3. Explore before extracting. First count (COUNT(*)) with the proposed filters; if the count \
   is manageable run the real query, if it's huge refine the filters or switch to aggregation. \
   Don't discover "too late" that you asked for half a million rows.

4. Materialize large datasets to file. If the data is needed for analysis or charts but is too \
   much to read, save it to a file (Parquet/CSV) with the dedicated tool and work on the \
   metadata, preview and statistics the tool returns: the data exists but never passes through \
   the context.

5. Summarize at the result level. For heterogeneous text rows prefer counts per category, value \
   ranges, distinct values and a representative sample, instead of whole records.

6. Respect the hard guardrails. The DB access level enforces protections that cannot be \
   bypassed: maximum LIMIT, query timeouts, cost estimation via EXPLAIN with blocking above \
   threshold, read-only statement whitelist, per-session row budget. If a tool blocks you, \
   don't insist: refine the request following the principles above.
"""
