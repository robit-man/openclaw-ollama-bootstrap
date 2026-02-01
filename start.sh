#!/usr/bin/env bash
set -euo pipefail

PORT="${OPENCLAW_PORT:-18789}"
STATE_DIR="$HOME/.openclaw"
TOKEN_FILE="$STATE_DIR/gateway.token"
CFG_FILE="$STATE_DIR/openclaw.json"
LOG_FILE="$STATE_DIR/gateway.log"
PID_FILE="$STATE_DIR/gateway.pid"
REPO_HINT_FILE="$STATE_DIR/repo.path"

# Optional overrides:
#   OPENCLAW_MODEL_PRIMARY="ollama/<exact>"   # e.g. "ollama/nemotron-3-nano:latest"
#   OPENCLAW_OLLAMA_MODEL="<exact>"           # e.g. "nemotron-3-nano:latest" (script prefixes ollama/)
#   OLLAMA_API_KEY="ollama-local"             # any value opts-in; explicit config still uses it for availability checks
#   OLLAMA_BASE="http://127.0.0.1:11434"      # will be normalized to .../v1 in config
OPENCLAW_MODEL_PRIMARY="${OPENCLAW_MODEL_PRIMARY:-}"
OPENCLAW_OLLAMA_MODEL="${OPENCLAW_OLLAMA_MODEL:-}"
OLLAMA_BASE="${OLLAMA_BASE:-http://127.0.0.1:11434}"

mkdir -p "$STATE_DIR"

# ---- Find the OpenClaw repo ----
pick_repo() {
  if [[ -f "$REPO_HINT_FILE" ]]; then
    local p
    p="$(cat "$REPO_HINT_FILE" | tr -d '\r\n')"
    if [[ -n "$p" && -f "$p/package.json" ]]; then
      echo "$p"
      return 0
    fi
  fi

  for p in "$HOME/src/openclaw" "$HOME/src/openclaw/openclaw" "$HOME/openclaw" "$HOME/openclaw/openclaw"; do
    if [[ -f "$p/package.json" ]]; then
      echo "$p"
      return 0
    fi
  done

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
echo "$REPO_DIR" > "$REPO_HINT_FILE"

# ---- Stable token (persisted) ----
if [[ -f "$TOKEN_FILE" ]]; then
  TOKEN="$(tr -d ' \t\r\n' < "$TOKEN_FILE")"
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

# ---- Get REAL local models (ollama list) ----
if ! command -v ollama >/dev/null 2>&1; then
  echo "ERROR: ollama not found in PATH." >&2
  echo "Fix: install Ollama and ensure 'ollama' is available, then rerun." >&2
  exit 1
fi

# NAME column from `ollama list` (includes tags like :latest / :32b)
OLLAMA_MODELS="$(
  ollama list 2>/dev/null | awk 'NR>1 && $1!="" {print $1}'
)"

if [[ -z "${OLLAMA_MODELS:-}" ]]; then
  echo "ERROR: 'ollama list' returned no models." >&2
  echo "Fix: pull at least one model, e.g.: ollama pull nemotron-3-nano" >&2
  exit 1
fi

export PORT TOKEN OPENCLAW_MODEL_PRIMARY OPENCLAW_OLLAMA_MODEL OLLAMA_BASE OLLAMA_MODELS
export OLLAMA_API_KEY="${OLLAMA_API_KEY:-ollama-local}"

# ---- Merge config + write explicit ollama provider with manual models ----
python3 - "$CFG_FILE" <<'PY'
import json, os, sys
from pathlib import Path

path = Path(sys.argv[1])

def is_ollama_ref(s: str) -> bool:
    return isinstance(s, str) and s.strip().lower().startswith("ollama/")

def strip_provider(s: str) -> str:
    s = (s or "").strip()
    if is_ollama_ref(s) and "/" in s:
        return s.split("/", 1)[1]
    return s

def ensure_v1(base: str) -> str:
    base = (base or "").strip().rstrip("/")
    if not base:
        return "http://127.0.0.1:11434/v1"
    return base if base.endswith("/v1") else (base + "/v1")

def parse_models(text: str):
    seen = set()
    out = []
    for line in (text or "").splitlines():
        m = line.strip()
        if not m:
            continue
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out

def resolve_model_id(requested: str, installed: list[str]) -> str | None:
    """
    Resolve a requested id against installed model names.
    Supports:
      - exact match
      - tagless -> tagged (foo -> foo:latest if present, else first foo:* match)
    """
    if not requested:
        return None
    req = requested.strip()

    if req in installed:
        return req

    # If user gives provider-prefixed or other garbage, strip
    req = strip_provider(req)

    if req in installed:
        return req

    # Tagless -> tagged
    # Prefer :latest if present, else first match starting with req + ":"
    latest = f"{req}:latest"
    if latest in installed:
        return latest

    for m in installed:
        if m.startswith(req + ":"):
            return m

    return None

# Load existing config (best-effort)
try:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        cfg = {}
except Exception:
    cfg = {}

port = int(os.environ.get("PORT", "18789"))
token = (os.environ.get("TOKEN") or "").strip()
if not token:
    raise SystemExit("TOKEN env missing/empty")

installed = parse_models(os.environ.get("OLLAMA_MODELS", ""))
if not installed:
    raise SystemExit("No models parsed from OLLAMA_MODELS (ollama list).")

# ---- gateway: set required fields, preserve others ----
gw = cfg.setdefault("gateway", {})
gw["mode"] = "local"
gw["port"] = port
gw["trustedProxies"] = ["127.0.0.1", "::1"]
gw["auth"] = {"mode": "token", "token": token}
gw.setdefault("controlUi", {})["allowInsecureAuth"] = True

# ---- explicit Ollama provider (manual models) ----
base_v1 = ensure_v1(os.environ.get("OLLAMA_BASE", "http://127.0.0.1:11434"))
api_key = os.environ.get("OLLAMA_API_KEY") or "ollama-local"

models_cfg = cfg.setdefault("models", {})
providers = models_cfg.setdefault("providers", {})
providers["ollama"] = {
    # docs: baseUrl should include /v1 for OpenAI-compatible APIs
    "baseUrl": base_v1,
    "apiKey": api_key,
    "api": "openai-completions",
    "models": [
        {
            "id": m,                 # must match agent reference: ollama/<id>
            "name": m,
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 8192,
            "maxTokens": 8192 * 10
        }
        for m in installed
    ],
}

# ---- choose primary from REAL installed models ----
agents = cfg.setdefault("agents", {})
defaults = agents.setdefault("defaults", {})
model = defaults.setdefault("model", {})

primary_env = (os.environ.get("OPENCLAW_MODEL_PRIMARY") or "").strip()
ollama_env = (os.environ.get("OPENCLAW_OLLAMA_MODEL") or "").strip()
existing_primary = (model.get("primary") or "").strip()

chosen_id = None

if primary_env:
    chosen_id = resolve_model_id(primary_env, installed)
    if not chosen_id:
        raise SystemExit(
            f"OPENCLAW_MODEL_PRIMARY='{primary_env}' does not match any local model from `ollama list`.\n"
            f"Local models: {installed}"
        )
elif ollama_env:
    chosen_id = resolve_model_id(ollama_env, installed)
    if not chosen_id:
        raise SystemExit(
            f"OPENCLAW_OLLAMA_MODEL='{ollama_env}' does not match any local model from `ollama list`.\n"
            f"Local models: {installed}"
        )
else:
    # Keep existing if it can be resolved to installed (supports tagless -> tagged)
    chosen_id = resolve_model_id(existing_primary, installed) if existing_primary else None

if not chosen_id:
    chosen_id = installed[0]  # first real model on device

model["primary"] = f"ollama/{chosen_id}"

# Optional: keep fallbacks only if they resolve to installed
fallbacks = model.get("fallbacks")
if isinstance(fallbacks, list):
    kept = []
    for f in fallbacks:
        rid = resolve_model_id(f, installed)
        if rid:
            kept.append(f"ollama/{rid}")
    model["fallbacks"] = kept

# workspace default
if not isinstance(defaults.get("workspace"), str) or not defaults.get("workspace"):
    defaults["workspace"] = str((Path.home() / ".openclaw" / "workspace"))

path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

print(f"[config] wrote: {path}")
print(f"[config] ollama baseUrl: {base_v1}")
print(f"[config] local models: {len(installed)}")
print(f"[config] agents.defaults.model.primary: ollama/{chosen_id}")
PY

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
echo "Ollama: $OLLAMA_BASE (config uses /v1)"
echo
echo "Local models (from \`ollama list\`):"
echo "$OLLAMA_MODELS" | sed 's/^/  - /'
echo
echo "Selected primary:"
jq -r '.agents.defaults.model.primary' "$CFG_FILE" 2>/dev/null || true
echo
echo "LOCAL WebUI:"
echo "  http://127.0.0.1:${PORT}/?token=${TOKEN}"
echo "============================================================"
echo

(
  echo "=== $(date) starting gateway ==="
  echo "pnpm openclaw gateway --port $PORT --verbose --allow-unconfigured --token <redacted>"
) >> "$LOG_FILE"

(
  OPENCLAW_CONFIG_PATH="$CFG_FILE" \
  OLLAMA_API_KEY="$OLLAMA_API_KEY" \
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
