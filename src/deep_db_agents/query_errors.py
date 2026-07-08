"""Turns query execution exceptions into feedback for the agent.

When the LLM generates an invalid query (syntax error, non-existent
table/column/field, incompatible operator or type, ...), the database driver raises
an exception. Instead of propagating it opaquely — interrupting the agent's turn —
the tools convert it into a structured message the agent can read to **fix the query**
on the next attempt: error handling becomes part of the feedback loop, not a
terminal failure.

Whitelist/scope violations (``QueryNotAllowedError``: out-of-scope index/collection,
disallowed stage or write clauses, malformed query) also flow through here and become
feedback: the forbidden operation is **still not executed** — it is blocked *before*
reaching the driver — but the rejection is communicated to the agent as a corrective
message rather than interrupting the turn. The EXPLAIN row-estimate guardrail
(``EstimateExceededError``) is handled the same way by ``format_estimate_block``: the
query is **not executed**, but the agent is asked to refine or aggregate and retry. The
session row budget (``GuardrailError`` from ``SessionBudget``), on the other hand, remains
a hard exception signaling a limit that must not be bypassed.
"""

from __future__ import annotations

import re

from .exceptions import GuardrailError
from .observability import get_logger

_logger = get_logger("query_errors")

# Maximum length of the query reported in the feedback, to avoid flooding the context.
_MAX_QUERY_CHARS = 2000

# Driver exceptions often embed the connection string with plaintext credentials
# (e.g. ``mysql://user:pass@host``) or ``password=...`` pairs: these are masked before
# reflecting the message into the LLM's context or into the logs.
_DSN_CREDENTIAL_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]+@")
_KV_SECRET_RE = re.compile(
    r"(?P<key>\b(?:password|passwd|pwd|secret|secret_key|token|api_key|apikey)\b\s*[=:]\s*)"
    r"(?P<val>\"[^\"]*\"|'[^']*'|\S+)",
    re.IGNORECASE,
)


def _redact_secrets(text: str) -> str:
    """Mask credentials (DSN and key=secret pairs) in a driver exception's text.

    Args:
        text: Raw exception message text that may contain a DSN or key=secret pairs.

    Returns:
        str: The same text with credentials replaced by ``***``.
    """
    text = _DSN_CREDENTIAL_RE.sub(r"\g<scheme>***@", text)
    return _KV_SECRET_RE.sub(r"\g<key>***", text)


def format_query_error(exc: Exception, *, query: str | None = None, what: str = "query") -> str:
    """Format a driver exception as corrective feedback for the agent.

    Args:
        exc: The exception raised during execution (database driver error).
        query: The text of the submitted query/pipeline; included in the feedback if
            present, so the agent sees exactly what it got wrong.
        what: Label for the executed construct (``"query"``, ``"pipeline"``,
            ``"Cypher query"``), used in the message.

    Returns:
        str: A multi-line message describing the failure, suitable for returning to
        the agent as tool output.
    """
    detail = _redact_secrets(str(exc).strip()) or "(no detail provided by the driver)"
    # The error becomes feedback for the agent, but it is also an observable event on
    # the operator side (failed or blocked query): logged with credentials already
    # redacted from the detail.
    _logger.warning("%s not executed: %s: %s", what, type(exc).__name__, detail)
    lines = [
        f"Error: the {what} was NOT executed by the database.",
        f"Error type: {type(exc).__name__}",
        f"Detail: {detail}",
    ]
    if query:
        text = query.strip()
        if len(text) > _MAX_QUERY_CHARS:
            text = text[:_MAX_QUERY_CHARS] + " …[truncated]"
        lines.append(f"{what.capitalize()} submitted:\n{text}")
    lines.append(
        f"The database rejected the request. Fix the {what} based on the error "
        "detail (syntax, table/column/field names, types or operators) and retry; "
        "if needed, inspect the schema first with the exploration tools."
    )
    return "\n".join(lines)


def format_estimate_block(exc: GuardrailError, *, what: str = "query") -> str:
    """Format an EXPLAIN row-estimate guardrail block as corrective feedback.

    Turns the ``EstimateExceededError`` raised by ``GuardrailConfig.check_estimate`` into
    a message the agent can act on: the query was **not executed** because its estimated
    result set is too large, so the agent should refine its filters or aggregate and retry.

    Args:
        exc: The guardrail exception raised when the estimate exceeded the threshold; its
            message already describes the estimate and the threshold.
        what: Label for the blocked construct (``"query"``, ``"search"``, ...), used in
            the message.

    Returns:
        str: A message describing the block, suitable for returning to the agent as tool
        output.
    """
    detail = str(exc).strip() or "(no detail provided)"
    # The block is corrective feedback for the agent, but also an observable event on the
    # operator side: logged alongside the reflected message.
    _logger.warning("%s blocked by the estimate guardrail: %s", what, detail)
    return "\n".join(
        [
            f"Warning: the {what} was NOT executed by the database.",
            f"Detail: {detail}",
            f"Narrow the {what} with more selective filters or aggregate in the database "
            "to reduce the estimated result set, then retry.",
        ]
    )
