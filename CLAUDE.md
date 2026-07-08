# CLAUDE.md

Guida per lavorare in questo repository. La documentazione estesa è in [GUIDE.md](GUIDE.md);
la panoramica utente in [README.md](README.md).

## Cos'è

`deep-db-agents` è una libreria Python (≥3.11) che fornisce una **factory** per creare
[Deep Agent](https://github.com/langchain-ai/deepagents) / agent LangChain specializzati su
un database. Un solo punto d'ingresso sceglie il *dialect* dallo schema dell'URL, costruisce i
tool adatti a quel DB con le credenziali iniettate nelle closure, concatena un system prompt
generico + uno specifico, e delega a `create_deep_agent` / `create_agent`.

Database supportati: MySQL, MariaDB, Postgres, MongoDB, Neo4j, SQLite, DuckDB, Elasticsearch,
OpenSearch.

## Comandi

Il progetto usa **uv**. Prefissa i comandi con `uv run`.

```bash
uv venv && uv pip install -e ".[all,dev]"   # setup (o solo gli extra che servono)
uv run pytest                                # test
uv run pytest tests/dialects/test_mysql.py   # un singolo file
uv run ruff check src tests                  # lint
uv run ruff format src tests                 # format
```

I test girano **senza un database reale**: la fixture `make_dialect` in [tests/conftest.py](tests/conftest.py)
mocka `_connect` con un `FakeCursor`/`FakeConnection` DB-API programmabile. Nuovi test di
dialect devono seguire questo pattern, non connettersi a un DB.

I comandi per la build degli artefatti:
```bash
rm -rf dist/.              # remove old buold
uv run python -m build.    # build artefacts
uv run twine check dist/*. # check build
```

Per upload su testpypi:
```bash
uv run twine upload --repository testpypi --skip-existing dist/*
```

Per upload su pypi:
```bash
uv run twine upload --repository pypi --skip-existing dist/*
```

## Architettura

Flusso: `db_url` → `parse_db_url` → `registry.resolve(scheme)` → istanza `DbDialect` →
`dialect.build_tools(conn, guardrails, workspace_dir)` + `dialect.system_prompt()` →
`create_deep_agent` / `create_agent`.

File chiave in [src/deep_db_agents/](src/deep_db_agents/):

- [factory.py](src/deep_db_agents/factory.py) — `create_deep_db_agents` (Deep Agent) e
  `create_db_agents` (agent LangChain semplice). Stessa logica, costruttore finale diverso.
- [base.py](src/deep_db_agents/base.py) — interfaccia astratta `DbDialect`: ogni dialect
  implementa `system_prompt()` e `build_tools()`.
- [registry.py](src/deep_db_agents/registry.py) — decorator `@register("scheme")` → mappa
  schema URL → classe dialect. Popolato per side-effect dall'import dei moduli dialect.
- [url.py](src/deep_db_agents/url.py) — parsing URL: DB di rete (`<schema>://host:port`) vs
  DB su file (`<schema>://<path>`, convenzione SQLAlchemy). `FILE_SCHEMES = {sqlite, duckdb}`.
- [guardrails.py](src/deep_db_agents/guardrails.py) — `GuardrailConfig` (LIMIT, timeout,
  soglia EXPLAIN, whitelist statement, budget righe) e `SessionBudget`.
- [workspace.py](src/deep_db_agents/workspace.py) — `materialize_result`: salva i risultati
  grandi su Parquet/CSV e restituisce **solo metadati + anteprima** all'agente.
- [connection.py](src/deep_db_agents/connection.py) — `ConnectionConfig` (frozen): host, port,
  `credential` (dict libero), `path` per i DB su file.

### Dialect

Ogni dialect vive in [dialects/<nome>/](src/deep_db_agents/dialects/) con `dialect.py`
(`@register` + classe), `prompt.py` (system prompt) e di norma `tools.py` (helper driver:
import lazy del driver, connessione, stime).

- I dialect SQL ereditano da `SqlDialect` in [dialects/sql_base.py](src/deep_db_agents/dialects/sql_base.py),
  che implementa **una sola volta** i tool condivisi (`list_tables`, `describe_table`,
  `count_rows`, `sample_rows`, `run_query`, `materialize_query`) sopra l'interfaccia DB-API
  2.0, delegando ai metodi astratti la parte driver-specifica (`_connect`, `_apply_timeout`,
  `_estimate_rows`, `_list_tables_sql`, `_describe_table_sql`, `_quote_ident`).
- SQLite e DuckDB ereditano da `FileSqlDialect`: nessun `statement_timeout` nativo, quindi il
  timeout è imposto da un **watchdog** che chiama `interrupt()` da un thread separato.
- MongoDB e Neo4j non sono DB-API: implementano direttamente `DbDialect` con i propri tool
  (es. [dialects/mongodb/dialect.py](src/deep_db_agents/dialects/mongodb/dialect.py)).
- Elasticsearch e OpenSearch ereditano da `SearchDialect` in
  [dialects/search_base.py](src/deep_db_agents/dialects/search_base.py), che implementa i tool
  condivisi (`list_indices`, `describe_index`, `count_documents`, `sample_documents`,
  `run_query`, `search_query`, `materialize_query`) sopra l'API REST comune ai due driver
  (`cat.indices`, `indices.get_mapping`, `count`, `search`); ogni dialect concreto fornisce solo
  `_connect`. L'accesso è limitato agli indici configurati in `credential["index"]` (nome
  singolo, CSV o pattern con `*`), validati lato codice in ogni tool.

### Aggiungere un nuovo dialect

1. Crea `dialects/<nome>/` con `dialect.py`, `prompt.py`, eventuale `tools.py`.
2. Eredita da `SqlDialect`/`FileSqlDialect` se DB-API, altrimenti da `DbDialect`.
3. Decora la classe con `@register("<schema>")`.
4. Aggiungi l'import in [dialects/__init__.py](src/deep_db_agents/dialects/__init__.py)
   (l'import ha side-effect: registra il dialect).
5. Aggiungi l'eventuale extra del driver in `pyproject.toml` con **import lazy** nei tool.
6. Aggiungi `tests/dialects/test_<nome>.py` usando la fixture `make_dialect`.

## Convenzioni

- **Guardrail nel wrapper dei tool, non nel prompt**: limiti, timeout, whitelist SELECT e
  budget sono imposti dal codice e non aggirabili dall'agente. La gerarchia di difesa è:
  *aggrega nel DB → limita e pagina → esplora prima di estrarre → materializza su file →
  riassumi → guardrail hard*.
- **Le credenziali restano nelle closure dei tool, mai nel system prompt.**
- **Import dei driver lazy** dentro i moduli `tools.py` / il metodo `_connect`, così il
  pacchetto si importa senza tutti i driver installati (sono extra opzionali).
- **Lingua**: l'intera libreria (`src/deep_db_agents/`) è in **inglese** — system prompt,
  docstring dei tool `@tool`, docstring per sviluppatori, commenti nel codice e messaggi di
  runtime/errore. Le docstring seguono lo stile **Google** (`Args:`/`Returns:`/`Raises:`) con
  tutti i parametri documentati. `CLAUDE.md`/`GUIDE.md` restano in italiano (istruzioni di
  progetto, non parte della libreria pubblicata).
- **Stile**: ruff, `line-length = 100`, regole `E,F,I,UP,B`. `from __future__ import annotations`
  in cima ai moduli; type hint moderni (`str | None`).
- Le eccezioni della libreria derivano da `DeepDbAgentError` in
  [exceptions.py](src/deep_db_agents/exceptions.py) e sono ri-esportate da `__init__.py`.
- **Dubbi e chiarimenti**: utilizza il tool AskUserQuestion per chiedere chiarimenti sulle parti ambigue o che richiedono maggiori informazioni.

## Knowledge Graph & Navigation

We use [Graphify](https://graphify.net/) to create a persistent, structured map of this codebase. 
Before executing broad searches or analyzing unfamiliar modules, use the `graphify` query to understand project interconnections.

### Graphify Workflow Guidelines

- **Project Mapping:** Run `/graphify .` in Claude Code to build or update the knowledge graph.
- **Pre-Search Context:** Always check `graphify-out/GRAPH_REPORT.md` first to understand the module map before requesting full file reads.
- **Navigation Shortcuts:** Use commands like `/graphify query "explain the auth flow"` to map out interdependencies rather than using traditional text searches.
- **Auto-Updates:** Keep the graph fresh after major structural refactors using `graphify . --update`.

## Note

- `snippets/` (script di prova manuali con DB reali) e `workspace/` (output materializzato)
  sono gitignored: non sono parte della libreria.
- `examples/` contiene quickstart eseguibili e versionati.
- Il modello Claude più capace di default per gli esempi: vedi README; quando scrivi codice
  che istanzia un modello, preferisci gli ID Claude più recenti.
