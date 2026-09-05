#!/usr/bin/env bash
# a2a_wake.sh — A2A handoff helper (接力棒协议 2026-07-19)
#
# Usage:
#   bash scripts/corpus/a2a_wake.sh <next_agent_id> "<message>"
#
# Example:
#   bash scripts/corpus/a2a_wake.sh corpus-curator "Picked PMID 36684514. Run process-raw."
#   bash scripts/corpus/a2a_wake.sh main "Chain complete. 12 articles scored."
#
# What it does:
#   1. exec: openclaw agent --agent <agent_id> --session-key agent:<agent_id>:main \
#                  --message "<message>" --timeout 600
#   2. retry once on failure (sleep 2s)
#   3. final error surfaced to caller
#
# Why `openclaw agent` not `sessions send`:
#   - OpenClaw 2026.6.10 没有 `sessions send` CLI(只有 list/compact/tail/cleanup)
#   - 实际唤醒机制是 `openclaw agent --agent <id> --message` 触发该 agent 的 main turn
#   - `--message` = wake 触发(每个 message 都 trigger agent turn)

set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <next_agent_id> \"<message>\"" >&2
  echo "  next_agent_id: corpus-curator | sci-writing-expert | fact-checker | main | researcher | critic | chinese-writer | visual-agent" >&2
  exit 64
fi

NEXT_AGENT="$1"
MESSAGE="$2"
TIMEOUT="${A2A_WAKE_TIMEOUT:-600}"
SELF_AGENT="${OPENCLAW_AGENT_ID:-$(basename "$(dirname "$0")" 2>/dev/null || echo subagent)}"

SESSION_KEY="agent:${NEXT_AGENT}:main"

run_once() {
  openclaw agent \
    --agent "$NEXT_AGENT" \
    --session-key "$SESSION_KEY" \
    --message "$MESSAGE" \
    --timeout "$TIMEOUT"
}

attempt=1
max_attempts=2
while [[ $attempt -le $max_attempts ]]; do
  echo "[a2a_wake] attempt $attempt -> $SESSION_KEY (self=$SELF_AGENT)" >&2
  if run_once; then
    echo "[a2a_wake] ✓ delivered to $SESSION_KEY" >&2
    exit 0
  fi
  echo "[a2a_wake] attempt $attempt failed for $SESSION_KEY" >&2
  attempt=$((attempt + 1))
  if [[ $attempt -le $max_attempts ]]; then
    sleep 2
  fi
done

echo "[a2a_wake] ✗ FAIL: could not wake $SESSION_KEY after $max_attempts attempts" >&2
exit 1
