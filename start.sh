#!/usr/bin/env bash
set -euo pipefail

PORT="${OPENCLAW_PORT:-18789}"
STATE_DIR="$HOME/.openclaw"
TOKEN_FILE="$STATE_DIR/gateway.token"
CFG_FILE="$STATE_DIR/openclaw.json"
LOG_FILE="$STATE_DIR/gateway.log"
PID_FILE="$STATE_DIR/gateway.pid"
REPO_HINT_FILE="$STATE_DIR/repo.path"

mkdir -p "$STATE_DIR"

# ---- Find the OpenClaw repo ----
pick_repo() {
  # 1) user hint file
  if [[ -f "$REPO_HINT_FILE" ]]; then
    local p
    p="$(cat "$REPO_HINT_FILE" | tr -d '\r\n')"
    if [[ -n "$p" && -f "$p/package.json" ]]; then
      echo "$p"
      return 0
    fi
  fi

  # 2) common locations (your actual one is first)
  for p in "$HOME/src/openclaw" "$HOME/src/openclaw/openclaw" "$HOME/openclaw" "$HOME/openclaw/openclaw"; do
    if [[ -f "$p/package.json" ]]; then
      echo "$p"
      return 0
    fi
  done

  # 3) maybe we are currently inside it
  if [[ -f "./package.json" ]]; then
    pwd
    return 0
  fi

  return 1
}

REPO_DIR="$(pick_repo || true)"
if [[ -z "${REPO_DIR:-}" ]]; then
  echo "ERROR: OpenClaw repo not found." >&2
  echo "Fix: create $REPO_HINT_FILE containing the repo path, e.g.:" >&2
  echo "  echo \"$HOME/src/openclaw\" > $REPO_HINT_FILE" >&2
  exit 1
fi

# Persist discovered repo for next time
echo "$REPO_DIR" > "$REPO_HINT_FILE"

# ---- Stable token (persisted) ----
if [[ -f "$TOKEN_FILE" ]]; then
  TOKEN="$(cat "$TOKEN_FILE" | tr -d ' \t\r\n')"
else
  if command -v openssl >/dev/null 2>&1; then
    TOKEN="dev-$(openssl rand -hex 16)"
  else
    TOKEN="dev-$(date +%s)-$RANDOM-$RANDOM"
  fi
  echo "$TOKEN" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
fi

if [[ -z "${TOKEN:-}" ]]; then
  echo "ERROR: token is empty (bad $TOKEN_FILE)" >&2
  exit 1
fi

# ---- Write config to disable pairing for WebUI behind cloudflared ----
cat > "$CFG_FILE" <<JSON
{
  "gateway": {
    "mode": "local",
    "port": ${PORT},
    "trustedProxies": ["127.0.0.1", "::1"],
    "auth": { "mode": "token", "token": "${TOKEN}" },
    "controlUi": { "allowInsecureAuth": true }
  }
}
JSON

# ---- Clear port ----
if command -v lsof >/dev/null 2>&1; then
  PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN || true)"
  if [[ -n "${PIDS:-}" ]]; then
    echo "[*] Clearing port $PORT (killing listener PIDs: $PIDS)"
    kill $PIDS 2>/dev/null || true
    sleep 0.4
    kill -9 $PIDS 2>/dev/null || true
  fi
elif command -v fuser >/dev/null 2>&1; then
  fuser -k "${PORT}/tcp" 2>/dev/null || true
fi

# ---- Stop previous gateway by PID file (best-effort) ----
if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[*] Stopping previous gateway PID $OLD_PID"
    kill "$OLD_PID" 2>/dev/null || true
    sleep 0.4
  fi
fi

cd "$REPO_DIR"
mkdir -p "$(dirname "$LOG_FILE")"
touch "$LOG_FILE"

echo
echo "============================================================"
echo "OpenClaw Gateway starting"
echo "Repo:   $REPO_DIR"
echo "Config: $CFG_FILE"
echo "Port:   $PORT"
echo "TOKEN:  $TOKEN"
echo
echo "LOCAL WebUI:"
echo "  http://127.0.0.1:${PORT}/?token=${TOKEN}"
echo
echo "CLOUDFLARED WebUI (after you start cloudflared elsewhere):"
echo "  https://<your-trycloudflare-host>/?token=${TOKEN}"
echo "============================================================"
echo

(
  echo "=== $(date) starting gateway ==="
  echo "pnpm openclaw gateway --port $PORT --verbose --allow-unconfigured --token <redacted>"
) >> "$LOG_FILE"

# Start gateway and tee logs; keep it running even if you stop tail
(
  OPENCLAW_CONFIG_PATH="$CFG_FILE" \
  OLLAMA_API_KEY="${OLLAMA_API_KEY:-ollama-local}" \
  COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
  CI=1 \
  pnpm openclaw gateway --port "$PORT" --verbose --allow-unconfigured --token "$TOKEN" \
  2>&1 | tee -a "$LOG_FILE"
) &
GW_PID=$!
echo "$GW_PID" > "$PID_FILE"

echo "[*] Gateway PID: $GW_PID"
echo "[*] Logs: $LOG_FILE"
echo "[*] Tailing logs (Ctrl+C stops tail; gateway keeps running)…"
echo
tail -n 200 -f "$LOG_FILE"
