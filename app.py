#!/usr/bin/env python3
"""
OpenClaw bootstrapper (git clone + config + Ollama + optional Telegram) with curses UI.

What it does:
- Clones/updates https://github.com/openclaw/openclaw into ~/src/openclaw by default
- Ensures pnpm via Corepack (non-interactive download prompt)
- Installs deps + builds OpenClaw; auto-patches a known TS2459 export issue if encountered
- Writes ~/.openclaw/openclaw.json and ~/.openclaw/.env
- Lets you pick an Ollama model via arrow-key list from Ollama /api/tags
- Starts the gateway with: --allow-unconfigured --token <token> (and config sets gateway.mode=local)

Notes:
- Ollama must be running locally for model list: `ollama serve`
- Gateway token is required by default; UI can be opened as:
  http://127.0.0.1:<port>/?token=<token>
"""

from __future__ import annotations

import curses
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError


OPENCLAW_REPO = "https://github.com/openclaw/openclaw.git"


# -------------------------
# Small helpers (non-UI)
# -------------------------

def which(cmd: str) -> Optional[str]:
    from shutil import which as _which
    return _which(cmd)

def run_capture(cmd: List[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    p = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    out, _ = p.communicate()
    return p.returncode, out

def wait_port(host: str, port: int, timeout_s: float = 6.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.6):
                return True
        except OSError:
            time.sleep(0.2)
    return False

def http_json(url: str, timeout_s: float = 2.5) -> dict:
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout_s) as resp:
        data = resp.read().decode("utf-8", errors="replace")
    return json.loads(data)

def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)

def shell_quote(value: str) -> str:
    # safe for .env sourced by bash
    return shlex.quote(value)

def normalize_ollama_model_name(name: str) -> str:
    # OpenClaw config uses "ollama/<id>" (docs show without :latest in examples)
    # but Ollama /api/tags returns names like "llama3.3:latest"
    # We'll keep the full string except strip ":latest" for nicer defaults.
    return name[:-7] if name.endswith(":latest") else name


# -------------------------
# Known TS2459 fix
# -------------------------

TS2459_NEEDLE = "error TS2459"
DISCORD_IDENTITY_NEEDLE = "DiscordSenderIdentity"
PREFLIGHT_TYPES_GLOB = "src/discord/monitor/message-handler.preflight.types"

def patch_ts2459_discord_sender_identity(repo: Path) -> bool:
    """
    Fixes:
      TS2459: Module ...message-handler.preflight.types.js declares 'DiscordSenderIdentity' locally, but it is not exported.

    We patch the *source* file (likely .ts/.mts/.cts) by ensuring the declaration is exported.
    Returns True if we changed something.
    """
    candidates: List[Path] = []
    for ext in (".ts", ".mts", ".cts", ".d.ts"):
        p = repo / f"{PREFLIGHT_TYPES_GLOB}{ext}"
        if p.exists():
            candidates.append(p)

    # As a fallback, search the folder for a matching file prefix.
    if not candidates:
        root = repo / "src" / "discord" / "monitor"
        if root.exists():
            for p in root.glob("message-handler.preflight.types.*"):
                if p.suffix in (".ts", ".mts", ".cts", ".d.ts"):
                    candidates.append(p)

    changed = False
    for p in candidates:
        s = p.read_text(encoding="utf-8", errors="replace")
        original = s

        # export type ...
        s = re.sub(r"(?m)^\s*type\s+(DiscordSenderIdentity)\b", r"export type \1", s)
        # export interface ...
        s = re.sub(r"(?m)^\s*interface\s+(DiscordSenderIdentity)\b", r"export interface \1", s)
        # export class ...
        s = re.sub(r"(?m)^\s*class\s+(DiscordSenderIdentity)\b", r"export class \1", s)

        if s != original:
            atomic_write_text(p, s)
            changed = True

    return changed


# -------------------------
# Curses UI helpers
# -------------------------

def safe_addstr(stdscr, y: int, x: int, s: str, attr: int = 0) -> None:
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h:
        return
    if x < 0:
        s = s[-x:]
        x = 0
    if x >= w:
        return
    s = s[: max(0, w - x - 1)]
    try:
        stdscr.addstr(y, x, s, attr)
    except curses.error:
        # Terminal too small or wide chars; best-effort no-crash.
        pass

def draw_box(stdscr, title: str) -> Tuple[int, int]:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    safe_addstr(stdscr, 0, 2, title, curses.A_BOLD)
    safe_addstr(stdscr, 1, 0, "-" * (w - 1))
    return h, w

def paragraph_lines(text: str, width: int) -> List[str]:
    out: List[str] = []
    for para in text.split("\n"):
        para = para.rstrip()
        if not para:
            out.append("")
            continue
        out.extend(textwrap.wrap(para, width=width, break_long_words=True, replace_whitespace=False))
    return out

def message_box(stdscr, title: str, text: str, footer: str = "Press any key…") -> None:
    h, w = draw_box(stdscr, title)
    body_w = max(20, w - 4)
    lines = paragraph_lines(text, body_w)
    y = 3
    for line in lines[: max(0, h - 6)]:
        safe_addstr(stdscr, y, 2, line)
        y += 1
    safe_addstr(stdscr, h - 2, 2, footer, curses.A_DIM)
    stdscr.refresh()
    stdscr.getch()

def prompt_input(stdscr, title: str, prompt: str, default: str = "", secret: bool = False) -> str:
    curses.curs_set(1)
    buf = list(default)
    pos = len(buf)

    while True:
        h, w = draw_box(stdscr, title)
        safe_addstr(stdscr, 3, 2, prompt)
        safe_addstr(stdscr, 5, 2, "Default: " + (default if default else "(empty)"), curses.A_DIM)

        display = "".join(buf)
        shown = "*" * len(display) if secret and display else display
        safe_addstr(stdscr, 7, 2, "> " + shown)

        # place cursor (best-effort)
        try:
            stdscr.move(7, min(w - 2, 4 + (pos if not secret else len(shown))))
        except curses.error:
            pass

        stdscr.refresh()
        ch = stdscr.getch()

        if ch in (curses.KEY_ENTER, 10, 13):
            curses.curs_set(0)
            return "".join(buf).strip() if "".join(buf).strip() else default.strip()
        if ch in (27,):  # ESC
            curses.curs_set(0)
            return default.strip()
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            if pos > 0:
                buf.pop(pos - 1)
                pos -= 1
            continue
        if ch == curses.KEY_LEFT:
            pos = max(0, pos - 1)
            continue
        if ch == curses.KEY_RIGHT:
            pos = min(len(buf), pos + 1)
            continue
        if ch == curses.KEY_HOME:
            pos = 0
            continue
        if ch == curses.KEY_END:
            pos = len(buf)
            continue
        if 0 <= ch <= 255 and chr(ch).isprintable():
            buf.insert(pos, chr(ch))
            pos += 1

def select_list(stdscr, title: str, items: List[str], help_text: str = "") -> Optional[str]:
    if not items:
        message_box(stdscr, title, "No items available.")
        return None

    idx = 0
    top = 0

    while True:
        h, w = draw_box(stdscr, title)
        if help_text:
            for i, line in enumerate(paragraph_lines(help_text, max(20, w - 4))[:2]):
                safe_addstr(stdscr, 2 + i, 2, line, curses.A_DIM)

        list_y = 5
        list_h = max(1, h - list_y - 3)
        view = items[top: top + list_h]

        for i, it in enumerate(view):
            y = list_y + i
            marker = "➤ " if (top + i) == idx else "  "
            attr = curses.A_REVERSE if (top + i) == idx else 0
            safe_addstr(stdscr, y, 2, (marker + it)[: w - 4], attr)

        safe_addstr(stdscr, h - 2, 2, "↑/↓ move • Enter select • q cancel", curses.A_DIM)
        stdscr.refresh()

        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            return None
        if ch in (curses.KEY_ENTER, 10, 13):
            return items[idx]
        if ch == curses.KEY_UP:
            idx = max(0, idx - 1)
        elif ch == curses.KEY_DOWN:
            idx = min(len(items) - 1, idx + 1)
        elif ch == curses.KEY_PPAGE:  # PageUp
            idx = max(0, idx - list_h)
        elif ch == curses.KEY_NPAGE:  # PageDown
            idx = min(len(items) - 1, idx + list_h)

        if idx < top:
            top = idx
        elif idx >= top + list_h:
            top = idx - list_h + 1


# -------------------------
# Bootstrap state
# -------------------------

@dataclass
class State:
    repo_dir: Path = Path.home() / "src" / "openclaw"
    git_ref: str = "auto"  # tag or branch; "auto" means latest tag
    gateway_port: int = 18789
    gateway_token: str = ""
    ollama_base: str = "http://127.0.0.1:11434"
    ollama_api_key: str = "ollama-local"
    ollama_model: str = ""  # like llama3.3 or llama3.3:latest
    telegram_bot_token: str = ""
    telegram_allow_from: str = ""  # comma-separated
    start_gateway: bool = True

    openclaw_dir: Path = field(default_factory=lambda: Path.home() / ".openclaw")
    config_path: Path = field(default_factory=lambda: Path.home() / ".openclaw" / "openclaw.json")
    env_path: Path = field(default_factory=lambda: Path.home() / ".openclaw" / ".env")
    gateway_log: Path = field(default_factory=lambda: Path.home() / ".openclaw" / "gateway.log")
    setup_log: Path = field(default_factory=lambda: Path.home() / ".openclaw" / "bootstrap.log")
    run_sh: Path = field(default_factory=lambda: Path.home() / ".openclaw" / "run-gateway.sh")


# -------------------------
# Git + pnpm operations
# -------------------------

def ui_stream_cmd(stdscr, title: str, cmd: List[str], cwd: Optional[Path], env: Dict[str, str], log_file: Path) -> Tuple[int, List[str]]:
    ensure_dir(log_file.parent)
    lines: List[str] = []
    with log_file.open("a", encoding="utf-8") as f:
        f.write("\n\n$ " + " ".join(shlex.quote(c) for c in cmd) + "\n")
        f.flush()

        p = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        while True:
            out = p.stdout.readline() if p.stdout else ""
            if out:
                out = out.rstrip("\n")
                lines.append(out)
                f.write(out + "\n")
                f.flush()

            # redraw
            h, w = draw_box(stdscr, title)
            safe_addstr(stdscr, 2, 2, "$ " + " ".join(shlex.quote(c) for c in cmd)[: w - 6], curses.A_DIM)

            tail = lines[-max(1, h - 6):]
            y = 4
            for t in tail:
                safe_addstr(stdscr, y, 2, t[: w - 4])
                y += 1
            safe_addstr(stdscr, h - 2, 2, f"Logging to {str(log_file)}", curses.A_DIM)
            stdscr.refresh()

            if p.poll() is not None:
                # consume remaining
                rest = p.stdout.read() if p.stdout else ""
                if rest:
                    for rline in rest.splitlines():
                        lines.append(rline)
                        f.write(rline + "\n")
                f.flush()
                break

        return p.returncode, lines

def git_latest_tag(repo: Path) -> Optional[str]:
    code, out = run_capture(["git", "tag", "--list", "v*", "--sort=-creatordate"], cwd=repo)
    if code != 0:
        return None
    tags = [t.strip() for t in out.splitlines() if t.strip()]
    return tags[0] if tags else None

def clone_or_update(stdscr, st: State, env: Dict[str, str]) -> None:
    ensure_dir(st.repo_dir.parent)

    if st.repo_dir.exists() and (st.repo_dir / ".git").exists():
        ui_stream_cmd(
            stdscr,
            "Updating OpenClaw repo (git fetch)",
            ["git", "fetch", "--tags", "--force", "--prune"],
            cwd=st.repo_dir,
            env=env,
            log_file=st.setup_log,
        )
    elif st.repo_dir.exists() and any(st.repo_dir.iterdir()):
        message_box(
            stdscr,
            "Repo directory not empty",
            f"{st.repo_dir} exists but is not a git repo.\n\nMove it aside or delete it, then rerun.",
        )
        raise RuntimeError("Repo dir exists but not a git repo")
    else:
        ensure_dir(st.repo_dir)
        ui_stream_cmd(
            stdscr,
            "Cloning OpenClaw repo",
            ["git", "clone", "--filter=blob:none", OPENCLAW_REPO, str(st.repo_dir)],
            cwd=None,
            env=env,
            log_file=st.setup_log,
        )

def checkout_ref(stdscr, st: State, env: Dict[str, str]) -> str:
    if st.git_ref == "auto":
        latest = git_latest_tag(st.repo_dir)
        if latest:
            ref = latest
        else:
            ref = "main"
    else:
        ref = st.git_ref

    code, _ = ui_stream_cmd(
        stdscr,
        f"Checking out {ref}",
        ["git", "checkout", "-f", ref],
        cwd=st.repo_dir,
        env=env,
        log_file=st.setup_log,
    )
    if code != 0:
        raise RuntimeError(f"git checkout failed for ref {ref}")
    return ref

def ensure_pnpm(stdscr, env: Dict[str, str], log: Path) -> None:
    # Make corepack non-interactive
    env = dict(env)
    env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"

    if not which("corepack"):
        raise RuntimeError("corepack not found (install Node.js that includes Corepack).")
    ui_stream_cmd(stdscr, "Corepack enable", ["corepack", "enable"], cwd=None, env=env, log_file=log)

    # Pin a pnpm version that's known to work with OpenClaw current tags.
    ui_stream_cmd(stdscr, "Corepack prepare pnpm", ["corepack", "prepare", "pnpm@10.23.0", "--activate"], cwd=None, env=env, log_file=log)

def pnpm_install_and_build(stdscr, st: State, env: Dict[str, str]) -> None:
    env = dict(env)
    env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
    env["CI"] = "1"

    code, lines = ui_stream_cmd(
        stdscr,
        "pnpm install",
        ["pnpm", "install"],
        cwd=st.repo_dir,
        env=env,
        log_file=st.setup_log,
    )
    if code != 0:
        raise RuntimeError("pnpm install failed (see log).")

    code, lines = ui_stream_cmd(
        stdscr,
        "pnpm build",
        ["pnpm", "build"],
        cwd=st.repo_dir,
        env=env,
        log_file=st.setup_log,
    )
    if code == 0:
        return

    joined = "\n".join(lines)
    if TS2459_NEEDLE in joined and DISCORD_IDENTITY_NEEDLE in joined:
        # Attempt the known patch then rebuild
        patched = patch_ts2459_discord_sender_identity(st.repo_dir)
        if patched:
            ui_stream_cmd(
                stdscr,
                "Applied TS2459 patch; rebuilding…",
                ["pnpm", "build"],
                cwd=st.repo_dir,
                env=env,
                log_file=st.setup_log,
            )
            code2, _ = run_capture(["pnpm", "build"], cwd=st.repo_dir, env=env)
            if code2 == 0:
                return

    raise RuntimeError("pnpm build failed (see ~/.openclaw/bootstrap.log).")


# -------------------------
# Config writing
# -------------------------

def build_openclaw_config(st: State) -> dict:
    cfg: dict = {
        "gateway": {
            "mode": "local",
            "port": st.gateway_port,
            "auth": {
                "mode": "token",
                "token": st.gateway_token,
            },
            # Helps when running through local reverse proxies / tunnels that add X-Forwarded-* headers.
            "trustedProxies": ["127.0.0.1", "::1"],
            "http": {
                "endpoints": {
                    "chatCompletions": {"enabled": True},
                }
            },
        },
        "agents": {
            "defaults": {
                "workspace": str((st.openclaw_dir / "workspace").expanduser()),
                "model": {
                    "primary": f"ollama/{normalize_ollama_model_name(st.ollama_model)}" if st.ollama_model else "ollama/llama3.3"
                },
            }
        },
    }

    if st.telegram_bot_token.strip():
        allow = [a.strip() for a in st.telegram_allow_from.split(",") if a.strip()]
        cfg["channels"] = {
            "telegram": {
                "botToken": st.telegram_bot_token.strip(),
            }
        }
        if allow:
            cfg["channels"]["telegram"]["allowFrom"] = allow

    return cfg

def write_config_and_env(st: State) -> None:
    ensure_dir(st.openclaw_dir)

    cfg = build_openclaw_config(st)
    atomic_write_text(st.config_path, json.dumps(cfg, indent=2) + "\n")

    env_lines = [
        f'OPENCLAW_CONFIG_PATH={shell_quote(str(st.config_path))}',
        f'OPENCLAW_GATEWAY_PORT={st.gateway_port}',
        f'OPENCLAW_GATEWAY_TOKEN={shell_quote(st.gateway_token)}',
        f'OLLAMA_API_KEY={shell_quote(st.ollama_api_key)}',
        # used by this script; OpenClaw auto-discovery assumes 127.0.0.1:11434 when implicit
        f'OLLAMA_BASE={shell_quote(st.ollama_base)}',
        "",
    ]
    atomic_write_text(st.env_path, "\n".join(env_lines))

    run_sh = f"""#!/usr/bin/env bash
set -euo pipefail

cd {shlex.quote(str(st.repo_dir))}
set -a
source {shlex.quote(str(st.env_path))}
set +a

export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export CI=1

exec pnpm openclaw gateway --port "$OPENCLAW_GATEWAY_PORT" --verbose --allow-unconfigured --token "$OPENCLAW_GATEWAY_TOKEN"
"""
    atomic_write_text(st.run_sh, run_sh)
    st.run_sh.chmod(0o755)


# -------------------------
# Ollama model discovery UI
# -------------------------

def fetch_ollama_models(base: str) -> List[str]:
    # Ollama API base is usually http://localhost:11434, tags endpoint at /api/tags
    url = base.rstrip("/") + "/api/tags"
    data = http_json(url)
    models = data.get("models", [])
    names = []
    for m in models:
        name = m.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    # stable sort
    names = sorted(set(names))
    return names


# -------------------------
# Gateway runner
# -------------------------

def start_gateway_background(st: State) -> None:
    ensure_dir(st.gateway_log.parent)
    env = os.environ.copy()
    # load .env values (simple KEY=VALUE, already shell-quoted)
    # We'll parse via a tiny safe parser (handles quotes from shlex.quote)
    for line in st.env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        # remove surrounding single quotes if present
        if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
            v = v[1:-1].replace("'\"'\"'", "'")
        env[k] = v

    env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
    env["CI"] = "1"

    with st.gateway_log.open("a", encoding="utf-8") as f:
        f.write("\n\n=== Starting gateway ===\n")
        f.flush()
        p = subprocess.Popen(
            ["pnpm", "openclaw", "gateway",
             "--port", str(st.gateway_port),
             "--verbose",
             "--allow-unconfigured",
             "--token", st.gateway_token],
            cwd=str(st.repo_dir),
            env=env,
            stdout=f,
            stderr=subprocess.STDOUT,
        )
    # Write pid file
    pid_path = st.openclaw_dir / "gateway.pid"
    atomic_write_text(pid_path, str(p.pid) + "\n")


# -------------------------
# Main curses flow
# -------------------------

def wizard(stdscr) -> None:
    curses.curs_set(0)
    curses.use_default_colors()
    stdscr.keypad(True)

    st = State()

    message_box(
        stdscr,
        "OpenClaw + Ollama Bootstrap",
        "This will clone OpenClaw, install deps, generate a config, and optionally start the gateway.\n\n"
        "You’ll pick an Ollama model using arrow keys (from Ollama /api/tags).",
        footer="Press any key to begin…",
    )

    # Basic inputs
    repo_dir = prompt_input(stdscr, "Repo location", "Install/clone OpenClaw into:", str(st.repo_dir))
    st.repo_dir = Path(repo_dir).expanduser()

    # Token: generate a good default
    default_token = "dev-" + secrets.token_urlsafe(18)
    token = prompt_input(stdscr, "Gateway token", "Gateway auth token (leave blank to auto-generate):", "", secret=False).strip()
    st.gateway_token = token if token else default_token

    port_s = prompt_input(stdscr, "Gateway port", "Gateway port:", str(st.gateway_port))
    try:
        st.gateway_port = int(port_s.strip())
    except ValueError:
        st.gateway_port = 18789

    st.ollama_base = prompt_input(stdscr, "Ollama base URL", "Ollama base URL:", st.ollama_base).strip() or st.ollama_base
    st.ollama_api_key = prompt_input(stdscr, "OLLAMA_API_KEY", "Value for OLLAMA_API_KEY (any value works):", st.ollama_api_key).strip() or st.ollama_api_key

    # Optional Telegram
    st.telegram_bot_token = prompt_input(stdscr, "Telegram (optional)", "Telegram bot token (BotFather). Leave blank to skip:", "", secret=True)
    if st.telegram_bot_token.strip():
        st.telegram_allow_from = prompt_input(
            stdscr,
            "Telegram allowFrom",
            "Telegram allowFrom (comma-separated user IDs or @usernames). Blank = pairing default:",
            "",
        )

    # Model list
    models: List[str] = []
    try:
        models = fetch_ollama_models(st.ollama_base)
    except (URLError, HTTPError, ValueError):
        models = []

    if models:
        chosen = select_list(
            stdscr,
            "Pick an Ollama model",
            models,
            help_text="Models are read from Ollama /api/tags. Use ↑/↓ and Enter. (If this list is empty, run: ollama serve; ollama pull llama3.3)",
        )
        st.ollama_model = chosen or (models[0] if models else "")
    else:
        st.ollama_model = prompt_input(
            stdscr,
            "Ollama model",
            "Could not fetch /api/tags. Enter a model name manually (e.g. llama3.3 or qwen2.5-coder:32b):",
            "llama3.3",
        ).strip()

    # Ask whether to start gateway
    start = select_list(stdscr, "Start gateway now?", ["Yes (recommended)", "No, just configure"], help_text="If you choose Yes, the gateway runs in the background and logs to ~/.openclaw/gateway.log")
    st.start_gateway = (start or "").startswith("Yes")

    # Preflight checks
    missing = []
    for cmd in ("git", "node", "corepack"):
        if not which(cmd):
            missing.append(cmd)
    if missing:
        message_box(
            stdscr,
            "Missing dependencies",
            "Missing required commands:\n\n- " + "\n- ".join(missing) + "\n\nInstall them, then rerun.",
        )
        return

    env = os.environ.copy()
    env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
    env["CI"] = "1"

    try:
        ensure_dir(st.openclaw_dir)

        clone_or_update(stdscr, st, env)
        ref = checkout_ref(stdscr, st, env)

        ensure_pnpm(stdscr, env, st.setup_log)
        pnpm_install_and_build(stdscr, st, env)

        write_config_and_env(st)

        if st.start_gateway:
            start_gateway_background(st)

            ok = wait_port("127.0.0.1", st.gateway_port, timeout_s=8.0)

            dash = f"http://127.0.0.1:{st.gateway_port}/?token={st.gateway_token}"
            curl_chat = (
                f"curl -sS http://127.0.0.1:{st.gateway_port}/v1/chat/completions \\\n"
                f"  -H 'Authorization: Bearer {st.gateway_token}' \\\n"
                f"  -H 'Content-Type: application/json' \\\n"
                f"  -H 'x-openclaw-agent-id: main' \\\n"
                f"  -d '{{\"model\":\"openclaw\",\"messages\":[{{\"role\":\"user\",\"content\":\"hi\"}}]}}'\n"
            )

            message_box(
                stdscr,
                "Done",
                "Config written:\n"
                f"  {st.config_path}\n"
                f"Env written:\n"
                f"  {st.env_path}\n"
                f"Run script:\n"
                f"  {st.run_sh}\n\n"
                f"Checked out ref: {ref}\n"
                f"Gateway log:\n"
                f"  {st.gateway_log}\n\n"
                f"Gateway reachable on port {st.gateway_port}: {'YES' if ok else 'NOT YET (check gateway.log)'}\n\n"
                f"Open dashboard (tokenized):\n  {dash}\n\n"
                "Test Ollama itself:\n"
                f"  curl {st.ollama_base.rstrip('/')}/api/tags\n\n"
                "Test OpenClaw Chat Completions:\n"
                f"{curl_chat}",
                footer="Press any key to exit…",
            )
        else:
            message_box(
                stdscr,
                "Configured (not started)",
                "Config written:\n"
                f"  {st.config_path}\n"
                f"Env written:\n"
                f"  {st.env_path}\n"
                f"Run gateway:\n"
                f"  {st.run_sh}\n\n"
                f"Dashboard once running:\n  http://127.0.0.1:{st.gateway_port}/?token={st.gateway_token}\n",
            )

    except Exception as e:
        # Always show a readable error (no curses crash)
        msg = f"{type(e).__name__}: {e}\n\nSee log:\n  {st.setup_log}"
        message_box(stdscr, "Bootstrap failed", msg, footer="Press any key…")


def main() -> None:
    curses.wrapper(wizard)


if __name__ == "__main__":
    main()
