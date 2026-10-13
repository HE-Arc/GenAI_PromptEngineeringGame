#!/usr/bin/env bash
# Streaming test against OpenCode Go: the game's model, same protocol and
# reasoning settings as the server (server/src/llm/openai.rs).
set -euo pipefail
KEY=$(grep '^LLM_API_KEY=' server/.env | cut -d= -f2- | tr -d ' \r')
curl -sN --max-time 90 \
  https://opencode.ai/zen/go/v1/responses \
  -H "Authorization: Bearer $KEY" \
  -H "x-opencode-session: ses_$(openssl rand -hex 16)" \
  -H "User-Agent: genai-game/1.0" \
  -H 'Content-Type: application/json' \
  -d '{"model":"muse-spark-1.3-contributor","input":"Décris une montagne en une phrase.","stream":true,"max_output_tokens":512,"reasoning":{"effort":"low"}}'
