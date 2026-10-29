# gateway

The only module that calls outside services: the LLM, literature sources and Telegram alerts (D-011, D-033). Holds every API key. Owns the `gateway` schema (LLM call records and the article cache); runs as database role `scc_gateway_app`. On the `egress` network.

- **Run:** `python -m scc_gateway` (port 8000). Migrate step: `python -m scc_gateway.migrate`.
- **Endpoints:** `POST /llm/structured` (one schema-constrained Claude call: priced, capped, retried on transient errors and recorded), `POST /literature/search` (PubMed with the open-access filter), `POST /literature/spell` (PubMed's spelling suggestion for a supplement name, NCBI ESpell; null when there is none), `POST /literature/fetch` (Europe PMC licence, metadata and JATS full text; disallowed licences are skipped and counted), `POST /notify` (a Telegram alert: event, code and short text, never user text; quiet codes and repeats within an hour are not sent), plus `/health`, `/health/deps` and `/version`. Spec: `openapi.json`.
- **Budget:** monthly caps per lane, worst-case reservation before each call, fails closed (D-038, RB-09).
- **Config:** price table, caps, licence allowlist and rate limits in `src/scc_gateway/config/gateway-v0.toml`. Secrets: `anthropic_api_key`, `ncbi_api_key`, `telegram_bot_token` and `telegram_chat_id` (an empty file means not configured). When the budget cap refuses a call, the gateway sends a `budget_cap_reached` alert itself. `SCC_BUDGET_CAP_OVERRIDE_EUR` lowers the caps for drills.
- **Tests:** `uv run pytest services/gateway`.
