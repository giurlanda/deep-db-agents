# deep-db-agents

Simplified creation of **Deep Agents** ([LangChain](https://github.com/langchain-ai/deepagents))
—generic or specialized on a specific database (MySQL, MariaDB, Postgres, MongoDB, Neo4j,
SQLite, DuckDB, Elasticsearch, OpenSearch)—through a single factory function.

📖 Full API reference: **[giurlanda.github.io/deep-db-agents](https://giurlanda.github.io/deep-db-agents/)**

## Idea

```python
from deep_db_agents import create_deep_db_agents

agent = create_deep_db_agents(
    db_url="mysql://localhost:3306",
    credential={"user": "user", "password": "my_password", "database": "shop"},
    system="The `shop` database contains orders and customers. The orders table has millions of rows.",
    model="claude-sonnet-4-5-20250929",
)

result = agent.invoke({
    "messages": [{"role": "user", "content": "How many orders in 2025, by region?"}]
})
```

The factory:

1. reads the **scheme** of the URL (`mysql`, `postgres`, `mongodb`, `neo4j`, `sqlite`,
   `duckdb`…) to pick the dialect the agent should specialize on;
2. builds the **tools** suited to that database, injecting the credentials (which stay in the
   tools' closures, never in the prompt);
3. concatenates the dialect's **generic system prompt** — which encodes context-management
   principles — with the `system` prompt passed by the user;
4. delegates to `create_deep_agent(tools=..., system_prompt=..., **kwargs)`.

All extra arguments (`model`, `subagents`, `checkpointer`, …) are forwarded as-is to
`create_deep_agent`.

The library also lets you query **several databases at once**, through
`create_deep_db_multi_agents` (see [Multi-database agents](#multi-database-agents)), and offers a
lighter, non-Deep-Agent alternative through `create_db_agents` (see
[`create_db_agents`: a lighter alternative](#create_db_agents-a-lighter-alternative)).

## Multi-database agents

`create_deep_db_multi_agents` builds an **orchestrator** agent that never queries a database
directly: it delegates each sub-question to the sub-agent specialized on the relevant database
(through the `task` tool) and combines the results. This is how you answer questions that span
multiple data sources.

```python
from deep_db_agents import create_deep_db_agents, create_deep_db_multi_agents

orders_agent = create_deep_db_agents(
    "postgres://localhost:5432",
    credential={"user": "reader", "password": "secret", "database": "orders"},
)
events_agent = create_deep_db_agents(
    "mongodb://localhost:27017",
    credential={"database": "events"},
)

orchestrator = create_deep_db_multi_agents(
    db_agents={
        "orders": {"description": "Orders and customers (Postgres)", "agent": orders_agent},
        "events": {"description": "Raw event log (MongoDB)", "agent": events_agent},
    },
    system="The two databases share the `customer_id` field; join results in memory.",
    model="claude-sonnet-4-5-20250929",
)

result = orchestrator.invoke(
    {"messages": [{"role": "user", "content": "Compare orders vs. events last week."}]}
)
```

Each sub-agent keeps its own tools and credentials in its own closures — the orchestrator only
ever sees the sub-agents' descriptions and their final answers, never raw rows. `db_agents` values
must be agents already built by `create_deep_db_agents` (compiled, with a working `.invoke`).

If sub-agents should share one materialization backend (see below), pass the same `backend=`
instance to every `create_deep_db_agents` call and to the orchestrator — see the full example in
[Materializing results: the filesystem backend](#materializing-results-the-filesystem-backend).

## `create_db_agents`: a lighter alternative

`create_db_agents` builds a plain LangChain agent (`langchain.agents.create_agent`) instead of a
Deep Agent. It goes through the same dialect resolution and tool/prompt construction as
`create_deep_db_agents`, so credentials, guardrails and error feedback all behave identically —
what differs is the surrounding harness:

- **No `materialize_*` tools.** They require a deepagents filesystem backend to write to; the
  plain agent has none, so large-result materialization (see below) is unavailable — only the
  guardrail-limited `run_query`/`sample_rows`-style tools are exposed.
- **No Deep Agent scaffolding.** No built-in planning/`TodoList`, no subagent delegation, no
  virtual filesystem — just a single agent with tools and a system prompt.
- Same signature otherwise (`db_url`, `credential`, `system`, `guardrails`, `metrics`, `**kwargs`
  forwarded to `create_agent`).

Use it when the questions are simple enough that you don't need multi-step planning or
file-backed results — e.g. quick lookups, dashboards, or a lightweight assistant embedded in
another application.

```python
from deep_db_agents import create_db_agents

agent = create_db_agents(
    "sqlite:///./data/app.db",
    system="Answer briefly, cite the exact table and column names.",
)
result = agent.invoke({"messages": [{"role": "user", "content": "List all tables."}]})
```

## Code interpreter (experimental)

`create_deep_db_agents(..., enable_code_interpreter=True)` attaches a `CodeInterpreterMiddleware`
(from the optional `langchain-quickjs` package, extra `code-interpreter`) to the agent. This gives
the model a sandboxed JavaScript execution tool that can also call the dialect's own DB tools
(`ptc`, "pass-through tools"), so it can fetch data and post-process it — reshape, aggregate,
compute statistics — in one code-execution step instead of many separate tool calls, saving
context.

```bash
pip install "deep-db-agents[code-interpreter]"        # from PyPI
uv pip install -e ".[code-interpreter]"                # from source
```

```python
agent = create_deep_db_agents(
    "postgres://localhost:5432",
    credential={"user": "reader", "password": "secret", "database": "shop"},
    enable_code_interpreter=True,
)
```

This is **experimental**: the middleware and its interaction with the guardrails are newer and
less battle-tested than the rest of the library, and its interface may still change. It does not
bypass the guardrails (the code interpreter can only call the same wrapped tools the agent
already has), but it does give the model a more general execution capability — enable it only if
you need the extra data-manipulation power.

## Context-management principles

The tools and generic prompts enforce this defense hierarchy:

> **aggregate in the DB → limit and paginate → explore before extracting → materialize to file → summarize → hard guardrails**

Guardrails are enforced by the tool wrapper (not by the agent): a non-bypassable maximum
`LIMIT`, query timeouts, row estimation via `EXPLAIN`, a `SELECT`-only whitelist, and a
per-session row/token budget. Large datasets are **materialized to file** (Parquet/CSV), and
only metadata and previews are passed back to the agent.

The `LIMIT`/`hard_max_rows` ceiling bounds the rows returned **into the agent's context**; the
`materialize_*` tools instead write to file, so they are bounded by `max_materialized_bytes` (the
maximum size, in bytes, of a materialized file — 10 MiB by default) rather than by `hard_max_rows`.
Rows are streamed and written only while they fit under that ceiling; if the write stops early the
tool response warns that the file is **incomplete**, so the agent can aggregate or filter to fit it.
This lets an agent materialize datasets far larger than `hard_max_rows` without loading them into
context.

`query_timeout_s` is enforced as a **client-side** timeout in addition to the server-side one,
so a query (or socket) that hangs gets interrupted instead of blocking the agent — especially
when the model issues several tool calls in parallel. For SQLite/DuckDB, which lack a native
`statement_timeout`, the limit is enforced by a watchdog calling `interrupt()`.

### Errors become feedback, not dead ends

When a query fails — bad syntax, a non-existent table/column/field, an incompatible operator or
type — the driver exception is **not** propagated raw to interrupt the agent's turn. It is turned
into a structured message (`query_errors.format_query_error`) that is returned as the tool's
output: error type, driver detail, the offending query (truncated, and with any credentials in
the message redacted), and a hint to fix and retry, exploring the schema first if needed. This
lets the model self-correct within the same conversation instead of failing the whole run.

The same applies to whitelist/scope violations (e.g. a write statement, or an index outside the
configured `credential["index"]` pattern on Elasticsearch/OpenSearch): the operation is blocked
*before* reaching the driver, but reported back as corrective feedback rather than a hard error.
The session-level guardrails behave the same way: an EXPLAIN row-estimate over the threshold
(`format_estimate_block`) and an exhausted per-session row budget (`format_budget_block`) are
returned as feedback too — the query is not executed or its result not returned, and the agent is
told to aggregate, refine its filters, or start a new session instead of interrupting the run.

## Materializing results: the filesystem backend

The `materialize_query` tool (Deep Agent only, see [`create_db_agents`: a lighter
alternative](#create_db_agents-a-lighter-alternative)) writes large results to a file (CSV or
Parquet) and returns only metadata, a preview and numeric statistics to the agent — see
[`MaterializedResult`](src/deep_db_agents/workspace.py). To do that it needs a **deepagents
backend** (a `BackendProtocol` implementation, e.g. `StateBackend` or `FilesystemBackend`) to
write to.

The backend is injected **directly into the tool's closures** — the same instance that is handed
to the agent, so materialized files land in the agent's own filesystem:

1. `create_deep_db_agents` reads the `backend=` kwarg; when you omit it, it defaults to an
   ephemeral `StateBackend()` (files live in the agent's LangGraph state for the thread).
2. That same instance is forwarded both to `create_deep_agent` (giving the agent its filesystem)
   **and** captured in the `materialize_query` closure, so the tool writes exactly where the agent
   reads.
3. When the write goes through a `StateBackend`, the tool returns a `Command` that carries the
   files to apply to the agent's state; external backends (`FilesystemBackend`/`StoreBackend`)
   persist directly, so only the summary message is returned.
4. `create_db_agents` (the plain LangChain agent) has no filesystem, so its tools receive
   `backend=None`; if a file-writing tool is ever reached it simply reports that no backend is
   configured instead of raising.

To share one backend across the sub-agents of a multi-database orchestrator, pass the same
instance to each `create_deep_db_agents` call and to `create_deep_db_multi_agents`.

```python
from deepagents.backends import FilesystemBackend
from deep_db_agents import create_deep_db_agents

# A persistent backend so materialized files survive on disk; omit `backend=` to get an
# ephemeral StateBackend by default.
backend = FilesystemBackend(root_dir="./workspace", virtual_mode=True)

agent = create_deep_db_agents(
    "duckdb:///warehouse/dw.duckdb",
    backend=backend,  # forwarded to the agent and injected into materialize_query's closure
)

result = agent.invoke(
    {"messages": [{"role": "user", "content": "Export all 2025 orders to a file."}]},
    config={"configurable": {"thread_id": "session-1"}},
)
```

## Metrics

Pass a `SessionMetrics` instance (from `deep_db_agents.observability`) to `metrics=` on either
factory function to get thread-safe counters for the whole session, readable after `invoke`:

- `queries_run` / `rows_returned` — successful executions and total rows returned.
- `estimate_blocked` — queries rejected by the `EXPLAIN` row-estimate guardrail before running.
- `budget_exhausted` — times the per-session row budget was hit.

```python
from deep_db_agents import create_deep_db_agents
from deep_db_agents.observability import SessionMetrics

metrics = SessionMetrics()
agent = create_deep_db_agents("postgres://localhost:5432", metrics=metrics, credential={...})
agent.invoke({"messages": [{"role": "user", "content": "How many orders last week?"}]})

print(metrics.summary())  # "queries run=3, rows returned=142, blocked by estimate=0, ..."
```

It's optional and created by the caller (one instance per agent/session) — the tools only update
the counters, they never create or reset the object.

## Installation

### From PyPI

```bash
pip install "deep-db-agents[mysql,postgres,analysis]"   # install only the extras you need
```

### From source (development)

```bash
uv venv
uv pip install -e ".[mysql,postgres,analysis,dev]"   # install only the extras you need
```

Available extras:

| Extra | Installs | Purpose |
|---|---|---|
| `mysql` | `pymysql` | MySQL dialect driver |
| `mariadb` | `pymysql` | MariaDB dialect driver (reuses the MySQL driver) |
| `postgres` | `psycopg[binary]` | Postgres dialect driver |
| `mongodb` | `pymongo` | MongoDB dialect driver |
| `neo4j` | `neo4j` | Neo4j dialect driver |
| `duckdb` | `duckdb` | DuckDB dialect driver |
| `elasticsearch` | `elasticsearch` | Elasticsearch dialect driver |
| `opensearch` | `opensearch-py` | OpenSearch dialect driver |
| `analysis` | `pandas`, `pyarrow` | Parquet support for `materialize_query` (large-result materialization) |
| `code-interpreter` | `langchain-quickjs` | Sandboxed JS execution tool (see [Code interpreter](#code-interpreter-experimental)) |
| `all` | every extra above | Every dialect driver + `analysis` + `code-interpreter` |
| `dev` | `pytest`, `ruff`, `langchain-openai`, `rich` | Test/lint tooling for contributing to the library |
| `docs` | `mkdocs`, `mkdocs-material`, `mkdocstrings[python]` | Build the documentation site locally |

SQLite needs no extra (it uses the stdlib `sqlite3`). Extras can be combined, e.g.
`pip install "deep-db-agents[postgres,mongodb,analysis]"`.

## Dialect status

| Database | URL scheme               | Status    |
|----------|---------------------------|-----------|
| MySQL    | `mysql`                   | Complete  |
| MariaDB  | `mariadb`                 | Complete  |
| Postgres | `postgres`/`postgresql`   | Complete  |
| MongoDB  | `mongodb`                 | Complete  |
| Neo4j    | `neo4j`                   | Complete  |
| SQLite   | `sqlite`                  | Complete  |
| DuckDB   | `duckdb`                  | Complete  |
| Elasticsearch | `elasticsearch`      | Complete  |
| OpenSearch    | `opensearch`         | Complete  |

### Credentials by dialect

`host`/`port` always come from the URL (`<scheme>://host:port`), never from `credential`; file-
based dialects (SQLite, DuckDB) use the URL's `path` instead and read no connection credentials.
Everything else — auth, target database, driver timeouts — is read from the `credential` dict,
whose expected keys depend on the dialect:

| Dialect | `credential` keys | Notes |
|---|---|---|
| MySQL / MariaDB | `user`, `password`, `database` (or `db`), `connect_timeout`, `read_timeout` | MariaDB reuses MySQL's connection logic unchanged. |
| Postgres | `user`, `password`, `database` (or `db`), `connect_timeout` | `database`/`db` maps to `dbname`. |
| MongoDB | `user`, `password`, `authSource` (or `auth_source`), `connect_timeout` | `database` selects the target DB (via `ConnectionConfig.database`). |
| Neo4j | `user`, `password`, `database`, `connect_timeout` | If `user` is omitted, the driver connects **unauthenticated**. |
| SQLite | `connect_timeout` | Path comes from the URL; always opened read-only (except `:memory:`). |
| DuckDB | *(none)* | Path comes from the URL; read-only unless `:memory:`; a folder path enables data-lake mode. |
| Elasticsearch | `use_ssl`, `verify_certs`, `ca_certs`, `api_key` **or** `user`/`password`, `index` | `api_key` takes priority over `user`/`password` if both are set. |
| OpenSearch | `use_ssl`, `verify_certs`, `ca_certs`, `user`/`password`, `index` | No `api_key` option, unlike Elasticsearch. |

`index` (Elasticsearch/OpenSearch only) restricts the agent to a single index name, a CSV list, or
a `*` wildcard pattern; every search tool validates the requested index against it and rejects
out-of-scope access as [corrective feedback](#errors-become-feedback-not-dead-ends), not a crash.

### File-based databases (SQLite, DuckDB)

For file-based DBs, the URL carries a **path** instead of host:port, following the SQLAlchemy
convention (the path is relative to the application's working directory):

```python
create_deep_db_agents(db_url="sqlite:///data/app.db")      # relative:  ./data/app.db
create_deep_db_agents(db_url="sqlite:////var/lib/app.db")  # absolute:  /var/lib/app.db
create_deep_db_agents(db_url="duckdb:///warehouse/dw.duckdb")
create_deep_db_agents(db_url="duckdb:///lake/")            # data lake: see below
```

- **DuckDB data lake**: if the path is a **folder** (trailing slash), the `parquet`/`csv`/`json`
  files inside it are exposed as queryable tables via SQL.
- DuckDB files are opened **read-only**, so multiple parallel tool calls can connect to the
  same file.
- `:memory:` is supported (`sqlite://:memory:`), but with SQLite it does not share state across
  tool calls (each call opens a new connection): suitable only for ephemeral/test usage.

## Fully local example (local database + local LLM)

Nothing in the factory is Anthropic-specific: `model`/`kwargs` are forwarded as-is to
`create_deep_agent`/`create_agent`, so any LangChain chat model works. Pairing a file-based
dialect (no server to run) with a local model server (e.g. [LM Studio](https://lmstudio.ai) or
Ollama exposing an OpenAI-compatible endpoint) gives you a fully offline agent:

```python
from langchain_openai import ChatOpenAI
from deep_db_agents import create_deep_db_agents

local_model = ChatOpenAI(
    model="qwen3-coder-30b",          # whatever model is loaded in LM Studio/Ollama
    base_url="http://127.0.0.1:1234/v1",
    api_key="not-needed",             # required by the client, ignored by the local server
    temperature=0.1,
)

agent = create_deep_db_agents(
    "sqlite:///./chinook.db",
    system="The `chinook` database is a digital music store (artists, albums, tracks, invoices).",
    model=local_model,
)

result = agent.invoke(
    {"messages": [{"role": "user", "content": "Which genre has the most tracks?"}]}
)
```

No `credential` is needed for a local SQLite file, no network egress is required for either the
database or the model, and the same pattern works with DuckDB.

## Development

```bash
ruff check src tests
pytest
```

## Disclaimer

This library grants an LLM agent the ability to connect to and query real databases. While
guardrails (statement whitelisting, timeouts, row limits, EXPLAIN thresholds) are enforced in
code and are not bypassable by the agent's prompt, no safeguard eliminates all risk: model
behavior can be unpredictable, and misconfiguration (e.g. overly broad credentials) is outside
the library's control. Always point it at credentials scoped to the minimum required
privileges, prefer read-only accounts for exploratory use, and test against non-production data
before running it against anything that matters. Use of this library, and any consequences
arising from it, is entirely at the user's own risk and responsibility.
