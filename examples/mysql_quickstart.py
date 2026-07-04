"""Esempio: agente specializzato su MySQL.

Prerequisiti:
    uv pip install -e ".[mysql,analysis]"
    export ANTHROPIC_API_KEY=...
    un'istanza MySQL raggiungibile su localhost:3306.
"""

from __future__ import annotations

from deep_db_agents import GuardrailConfig, create_deep_db_agents


def main() -> None:
    agent = create_deep_db_agents(
        db_url="mysql://localhost:3306",
        credential={"user": "user", "password": "my_password", "database": "shop"},
        system=(
            "Il database `shop` contiene le tabelle `ordini` (milioni di righe) e `clienti`. "
            "La colonna `ordini.importo` è in euro e `ordini.data` è una DATE."
        ),
        model="claude-sonnet-4-5-20250929",
        # Guardrail opzionali: tetti di sicurezza non aggirabili dall'agente.
        guardrails=GuardrailConfig(default_rows=100, hard_max_rows=1000, query_timeout_s=30),
    )

    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Quanti ordini nel 2025 per regione e qual è l'importo medio?",
                }
            ]
        },
        config={"configurable": {"thread_id": "demo-1"}},
    )
    print(result["messages"][-1].content)


if __name__ == "__main__":
    main()
