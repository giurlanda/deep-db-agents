"""Esempio: uso asincrono dell'agente (``ainvoke``).

Gli agenti creati dalla factory si invocano anche in modo asincrono con ``await
agent.ainvoke(...)``. I tool del dialect sono sincroni: sotto ``ainvoke`` LangChain li esegue
in un thread pool executor, quindi le tool call di un turno vengono dispatchate in modo
concorrente senza bloccare l'event loop. Combinato col riuso dei client driver thread-safe
(MongoClient, Driver Neo4j, client ES/OS), questo dà esecuzione parallela reale delle
letture verso il database, senza bisogno di driver asincroni nativi.

Prerequisiti:
    uv pip install -e ".[postgres,analysis]"
    export ANTHROPIC_API_KEY=...
    un Postgres raggiungibile su localhost:5432.
"""

from __future__ import annotations

import asyncio

from deep_db_agents import GuardrailConfig, SessionMetrics, create_deep_db_agents


async def main() -> None:
    metrics = SessionMetrics()
    agent = create_deep_db_agents(
        db_url="postgres://localhost:5432",
        credential={"user": "user", "password": "my_password", "database": "shop"},
        system="Il database `shop` contiene `ordini` (milioni di righe) e `clienti`.",
        model="claude-sonnet-4-5-20250929",
        guardrails=GuardrailConfig(default_rows=100, hard_max_rows=1000, query_timeout_s=30),
        # I contatori vengono aggiornati dai tool durante l'esecuzione.
        metrics=metrics,
    )

    # Invocazione asincrona: i tool sincroni girano in un thread executor.
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Quanti ordini per regione nel 2025?"}]},
        config={"configurable": {"thread_id": "async-demo"}},
    )
    print(result["messages"][-1].content)
    print("Metriche sessione:", metrics.summary())


if __name__ == "__main__":
    asyncio.run(main())
