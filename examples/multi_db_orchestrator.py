"""Esempio: orchestratore che coordina più database differenti.

Un solo agente orchestratore delega a due sotto-agenti specializzati — uno su Postgres
(ordini), uno su MongoDB (profili clienti) — e ricompone i risultati per rispondere a una
domanda che attraversa entrambi i database. I join cross-DB non avvengono lato server: ogni
sotto-agente aggrega e proietta il minimo necessario, l'orchestratore correla i risultati.

Prerequisiti:
    uv pip install -e ".[postgres,mongodb,analysis]"
    export ANTHROPIC_API_KEY=...
    un'istanza Postgres su localhost:5432 e una MongoDB su localhost:27017.
"""

from __future__ import annotations

from deep_db_agents import GuardrailConfig, create_deep_db_agents, create_deep_db_multi_agent


def main() -> None:
    # 1) Un sotto-agente per ciascun database, ognuno con le proprie credenziali e guardrail.
    sales_agent = create_deep_db_agents(
        db_url="postgres://localhost:5432",
        credential={"user": "user", "password": "my_password", "database": "sales"},
        system=(
            "Il database `sales` contiene la tabella `orders` (milioni di righe) con "
            "`customer_id`, `amount` (euro) e `created_at` (DATE)."
        ),
        model="claude-sonnet-4-5-20250929",
        guardrails=GuardrailConfig(default_rows=100, hard_max_rows=1000, query_timeout_s=30),
    )
    crm_agent = create_deep_db_agents(
        db_url="mongodb://localhost:27017",
        credential={"username": "user", "password": "my_password", "database": "crm"},
        system=(
            "Il database `crm` ha la collection `customers` con `_id`, `country` e `tier` "
            "(bronze/silver/gold)."
        ),
        model="claude-sonnet-4-5-20250929",
    )

    # 2) L'orchestratore: riceve i sotto-agenti con nome + descrizione e li coordina.
    orchestrator = create_deep_db_multi_agent(
        db_agents={
            "sales": {
                "description": "Ordini e fatturato (Postgres). Aggrega importi e conteggi "
                "per cliente, periodo, ecc.",
                "agent": sales_agent,
            },
            "crm": {
                "description": "Anagrafica clienti (MongoDB): paese e tier di fedeltà per "
                "customer_id.",
                "agent": crm_agent,
            },
        },
        system=(
            "I due database si correlano su customer_id (= customers._id nel CRM). "
            "Per le domande cross-DB chiedi a ciascun sotto-agente solo le chiavi/valori "
            "aggregati che servono, poi unisci i risultati."
        ),
        model="claude-sonnet-4-5-20250929",
    )

    # 3) Una domanda che richiede entrambi i database.
    result = orchestrator.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Qual è il fatturato totale 2025 dei clienti di tier 'gold', "
                        "suddiviso per paese?"
                    ),
                }
            ]
        },
        config={"configurable": {"thread_id": "demo-multi-1"}},
    )
    print(result["messages"][-1].content)


if __name__ == "__main__":
    main()
