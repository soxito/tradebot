"""TradingAgents sidecar service.

Isolated FastAPI microservice that wraps the `tradingagents` package
(multi-agent LLM trading framework) behind a small HTTP + SSE API so the
main tradebot backend never has to import its heavy dependency stack
(langchain, langgraph, pandas 3.x) in-process.

Run with the dedicated venv created under integrations/TradingAgents/.venv:

    integrations/TradingAgents/.venv/bin/python -m tradingagents_service.main

Default port: 8010 (configurable via TRADINGAGENTS_SERVICE_PORT).
"""
