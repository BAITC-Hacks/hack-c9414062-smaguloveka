#!/usr/bin/env bash
# Запуск MCP-сервера AI Hatshy (stdio) из корня проекта — для Claude Code / Claude Desktop.
# Веб-сервер AI Hatshy должен быть запущен: .venv/bin/python -m uvicorn app.main:app --port 8000
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m app.mcp_server "$@"
