#!/usr/bin/env python3
"""
OpenClaw + Ollama bootstrap wizard (curses TUI)

What this script does (end-to-end):
- Clones/updates https://github.com/openclaw/openclaw
- Ensures pnpm via corepack (non-interactive download prompt disabled)
- Builds OpenClaw (pnpm install, pnpm ui:build, pnpm build)
- Ollama integration:
  - Lists local models via Ollama /api/tags
  - Arrow-key picker + type-to-filter + Enter to select
  - Optional: ollama pull selected model
- Telegram integration:
  - Key ingress for bot tokens (multi-account)
- Gateway bootstrap:
  - Writes gateway.mode=local
  - Writes gateway.auth.token (the key you must pass to UI/curl)
  - Optionally writes gateway.trustedProxies for Cloudflare Tunnel / reverse proxy
  - Starts gateway with the known-good command:
      pnpm openclaw gateway --port 18789 --verbose --allow-unconfigured --token "<token>"
- Writes:
  - ~/.openclaw/openclaw.json
  - ~/.openclaw/.env
- Shows:
  - Token value
  - Tokenized dashboard URLs
  - Curl examples (with Authorization header)

Controls:
- Lists: ↑/↓ move, Enter select, Esc back, type to filter, Backspace delete, c = custom
- During long-running commands: 'q' abort (for gateway, 'q' stops it normally)

Requires:
- git
- node >= 22
- ollama
- pnpm OR corepack (corepack usually bundled with node)
"""

from __future__ import annotations

import curses
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap
import time
import secrets
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote


# -------------------------
# Path + OS helpers
# -------------------------

def expand_path(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p))).resolve()

def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)

def safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def set_private_perms(p: Path) -> None:
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass

def parse_node_major(version_str: str) -> Optional[int]:
    m = re.search(r"v?(\d+)\.", (version_str or "").strip())
    return int(m.group(1)) if m else None

def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))

def is_printable_char(ch: int) -> bool:
    return 32 <= ch <= 126

def dir_is_empty(p: Path) -> bool:
    try:
        return p.exists() and p.is_dir() and next(p.iterdir(), None) is None
    except Exception:
        return False

def dir_has_git(p: Path) -> bool:
    return (p / ".git").exists()

def safe_rmtree(p: Path) -> None:
    shutil.rmtree(p)

def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False

def port_is_free(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
        return True
    except OSError:
        return False


# -------------------------
# SAFE CURSES WRITES (avoid addwstr ERR)
# -------------------------

def safe_addstr(win, y: int, x: int, s: str, attr: int = 0) -> None:
    try:
        maxy, maxx = win.getmaxyx()
        if y < 0 or y >= maxy:
            return
        if x < 0 or x >= maxx:
            return
        max_len = maxx - x - 1  # avoid last-col edge cases
        if max_len <= 0:
            return
        ss = s[:max_len]
        try:
            if attr:
                win.addstr(y, x, ss, attr)
            else:
                win.addstr(y, x, ss)
        except curses.error:
            return
    except Exception:
        return

def safe_hline(win, y: int, x: int, ch: str, n: int, attr: int = 0) -> None:
    try:
        maxy, maxx = win.getmaxyx()
        if y < 0 or y >= maxy or x < 0 or x >= maxx:
            return
        n = min(n, maxx - x - 1)
        if n <= 0:
            return
        safe_addstr(win, y, x, ch * n, attr)
    except Exception:
        return


# -------------------------
# HTTP helpers (Ollama)
# -------------------------

def http_json(url: str, method: str = "GET", body: Optional[dict] = None, timeout_s: int = 3) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)

def fetch_ollama_tags(base: str) -> List[str]:
    data = http_json(f"{base}/api/tags", timeout_s=3)
    models = data.get("models", []) or []
    names: List[str] = []
    for m in models:
        n = m.get("name")
        if n:
            names.append(n)
    return sorted(set(names))

def ollama_model_capabilities(base: str, model: str) -> List[str]:
    data = http_json(f"{base}/api/show", method="POST", body={"model": model}, timeout_s=4)
    caps = data.get("capabilities", []) or []
    return [str(c) for c in caps if c is not None]

def is_tool_capable(caps: List[str]) -> bool:
    lowered = [c.lower() for c in caps]
    if "tools" in lowered:
        return True
    return any("tool" in c for c in lowered)

def build_model_menu_items(base: str, names: List[str]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for n in names:
        try:
            caps = ollama_model_capabilities(base, n)
        except Exception:
            caps = []
        flags: List[str] = []
        if is_tool_capable(caps):
            flags.append("tools")
        tag = f" [{' '.join(flags)}]" if flags else ""
        out.append((f"{n}{tag}", n))
    out.sort(key=lambda lv: (0 if "tools" in lv[0] else 1, lv[0].lower()))
    return out


# -------------------------
# Files: dotenv + config
# -------------------------

def update_dotenv(path: Path, kv: Dict[str, str]) -> None:
    lines: List[str] = []
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []

    out_lines: List[str] = []
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            out_lines.append(line)
            continue
        k, _v = line.split("=", 1)
        k = k.strip()
        if k in kv:
            continue
        out_lines.append(line)

    for k, v in kv.items():
        out_lines.append(f"{k}={v}")

    safe_mkdir(path.parent)
    path.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")
    set_private_perms(path)

def read_json_if_exists(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

def deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst

def write_json_config(path: Path, obj: dict) -> None:
    safe_mkdir(path.parent)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    set_private_perms(path)


# -------------------------
# Curses UI primitives
# -------------------------

class LogBuffer:
    def __init__(self, max_lines: int = 4000) -> None:
        self.max_lines = max_lines
        self.lines: List[str] = []

    def add(self, s: str) -> None:
        s = s.replace("\r", "")
        s = "".join(c if (c == "\t" or (32 <= ord(c) <= 126)) else " " for c in s)
        self.lines.append(s)
        if len(self.lines) > self.max_lines:
            self.lines = self.lines[-self.max_lines:]

    def tail(self, n: int = 60) -> str:
        return "\n".join(self.lines[-n:]).strip()


def ui_header(stdscr, title: str, subtitle: str = "") -> None:
    h, w = stdscr.getmaxyx()
    stdscr.attron(curses.A_REVERSE)
    safe_hline(stdscr, 0, 0, " ", w, 0)
    safe_addstr(stdscr, 0, 1, title[: max(0, w - 3)], curses.A_BOLD)
    stdscr.attroff(curses.A_REVERSE)
    if subtitle:
        safe_addstr(stdscr, 1, 1, subtitle[: max(0, w - 3)], 0)

def ui_footer(stdscr, text: str) -> None:
    h, w = stdscr.getmaxyx()
    stdscr.attron(curses.A_REVERSE)
    safe_hline(stdscr, h - 1, 0, " ", w, 0)
    safe_addstr(stdscr, h - 1, 1, text[: max(0, w - 3)], 0)
    stdscr.attroff(curses.A_REVERSE)

def ui_paragraph(stdscr, y: int, x: int, w: int, text: str) -> int:
    maxy, maxx = stdscr.getmaxyx()
    usable_w = min(w, max(10, maxx - x - 2))
    if usable_w < 10:
        return min(y, maxy - 2)

    wrapped = textwrap.wrap(text, width=usable_w)
    written = 0
    for i, line in enumerate(wrapped):
        yy = y + i
        if yy >= maxy - 1:
            break
        safe_addstr(stdscr, yy, x, line, 0)
        written += 1
    return min(y + written, maxy - 2)

def message_box(stdscr, title: str, text: str, footer: str = "Press any key") -> None:
    stdscr.clear()
    ui_header(stdscr, title)
    h, w = stdscr.getmaxyx()
    y = 3
    truncated = False

    for para in text.split("\n"):
        if y >= h - 2:
            truncated = True
            break
        y2 = ui_paragraph(stdscr, y, 2, w - 4, para)
        if y2 == y and para:
            truncated = True
            break
        y = y2
        if y < h - 2:
            y += 1

    if truncated:
        footer = (footer + " (truncated; resize terminal for more)")[: max(0, w - 3)]

    ui_footer(stdscr, footer)
    stdscr.refresh()
    stdscr.getch()

def yes_no(stdscr, title: str, question: str, default_yes: bool = True) -> bool:
    stdscr.clear()
    ui_header(stdscr, title)
    h, w = stdscr.getmaxyx()
    y = 3
    y = ui_paragraph(stdscr, y, 2, w - 4, question)
    y = min(y + 2, h - 3)

    choice = 0 if default_yes else 1
    options = ["Yes", "No"]
    while True:
        for i, opt in enumerate(options):
            attr = curses.A_REVERSE if i == choice else curses.A_NORMAL
            safe_addstr(stdscr, y, 2 + i * 8, opt, attr)
        ui_footer(stdscr, "←/→ or y/n   Enter=Select")
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (curses.KEY_LEFT, ord("h")):
            choice = 0
        elif ch in (curses.KEY_RIGHT, ord("l")):
            choice = 1
        elif ch in (ord("y"), ord("Y")):
            return True
        elif ch in (ord("n"), ord("N")):
            return False
        elif ch in (10, 13):
            return choice == 0

def prompt_text(stdscr, title: str, prompt: str, initial: str = "", secret: bool = False) -> Optional[str]:
    curses.curs_set(1)
    stdscr.clear()
    ui_header(stdscr, title)
    h, w = stdscr.getmaxyx()
    y = 3
    y = ui_paragraph(stdscr, y, 2, w - 4, prompt)
    y = min(y + 1, h - 3)

    buf = list(initial)
    while True:
        safe_hline(stdscr, y, 2, " ", w - 4, 0)
        shown = ("*" * len(buf)) if secret else "".join(buf)
        safe_addstr(stdscr, y, 2, shown, 0)
        try:
            stdscr.move(y, 2 + min(len(shown), max(0, w - 5)))
        except curses.error:
            pass

        ui_footer(stdscr, "Enter=OK  Esc=Cancel  Backspace=Delete")
        stdscr.refresh()

        ch = stdscr.getch()
        if ch == 27:
            curses.curs_set(0)
            return None
        if ch in (10, 13):
            curses.curs_set(0)
            return "".join(buf).strip()
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            if buf:
                buf.pop()
            continue
        if is_printable_char(ch):
            buf.append(chr(ch))

def select_list(
    stdscr,
    title: str,
    items: List[Tuple[str, str]],
    subtitle: str = "",
    allow_filter: bool = True,
    allow_custom: bool = True,
) -> Optional[str]:
    filter_text = ""
    idx = 0
    scroll = 0

    while True:
        stdscr.clear()
        ui_header(stdscr, title, subtitle)
        h, w = stdscr.getmaxyx()
        list_y = 3
        list_h = max(1, h - 5)

        flt = [(l, v) for (l, v) in items if filter_text.lower() in l.lower()] if filter_text else items[:]

        if not flt:
            ui_paragraph(stdscr, list_y, 2, w - 4, "No items match your filter." if filter_text else "No items.")
        else:
            idx = clamp(idx, 0, len(flt) - 1)
            if idx < scroll:
                scroll = idx
            if idx >= scroll + list_h:
                scroll = idx - list_h + 1
            scroll = clamp(scroll, 0, max(0, len(flt) - list_h))

            for row in range(list_h):
                j = scroll + row
                if j >= len(flt):
                    break
                label, _val = flt[j]
                attr = curses.A_REVERSE if j == idx else curses.A_NORMAL
                safe_addstr(stdscr, list_y + row, 2, label[: max(0, w - 4)], attr)

        parts = []
        if allow_filter:
            parts.append(f"Filter: {filter_text}" if filter_text else "Type to filter")
        parts.append("↑↓=Move  Enter=Select  Esc=Back")
        if allow_custom:
            parts.append("c=Custom")
        ui_footer(stdscr, "   |   ".join(parts))

        stdscr.refresh()
        ch = stdscr.getch()
        if ch == 27:
            return None
        if ch in (curses.KEY_UP, ord("k")) and flt:
            idx = max(0, idx - 1)
        elif ch in (curses.KEY_DOWN, ord("j")) and flt:
            idx = min(len(flt) - 1, idx + 1)
        elif ch in (10, 13) and flt:
            return flt[idx][1]
        elif allow_custom and ch in (ord("c"), ord("C")):
            custom = prompt_text(stdscr, title, "Enter a custom model name (e.g., llama3.3 or qwen2.5-coder:32b):")
            if custom:
                return custom
        elif allow_filter and ch in (curses.KEY_BACKSPACE, 127, 8):
            if filter_text:
                filter_text = filter_text[:-1]
                idx = 0
                scroll = 0
        elif allow_filter and is_printable_char(ch):
            filter_text += chr(ch)
            idx = 0
            scroll = 0


def manage_telegram_accounts(stdscr) -> Dict[str, Dict[str, str]]:
    accounts: Dict[str, Dict[str, str]] = {"default": {"name": "Primary bot", "botToken": ""}}
    idx = 0

    while True:
        stdscr.clear()
        ui_header(stdscr, "Telegram bot setup", "Add one or more bot tokens (multi-account supported)")
        h, w = stdscr.getmaxyx()

        keys = sorted(accounts.keys())
        if keys:
            idx = clamp(idx, 0, len(keys) - 1)

        y = 3
        safe_addstr(stdscr, y, 2, "Accounts:", curses.A_BOLD)
        y += 1

        max_rows = max(1, h - 8)
        for i, k in enumerate(keys[:max_rows]):
            a = accounts[k]
            label = f"{k:12}  {a.get('name','')[:24]:24}  token={'set' if a.get('botToken') else 'EMPTY'}"
            attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
            safe_addstr(stdscr, y + i, 2, label[: max(0, w - 4)], attr)

        ui_footer(stdscr, "a=Add  e=Edit  d=Delete  ↑↓=Move  Enter=Done  Esc=Cancel")
        stdscr.refresh()

        ch = stdscr.getch()
        if ch == 27:
            return {}
        if ch in (10, 13):
            return {k: v for k, v in accounts.items() if v.get("botToken")}
        if ch in (curses.KEY_UP, ord("k")):
            idx = max(0, idx - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            idx = min(max(0, len(keys) - 1), idx + 1)
        elif ch in (ord("a"), ord("A")):
            acc_id = prompt_text(stdscr, "Add Telegram account", "Enter accountId (e.g., default, alerts):", initial="alerts")
            if not acc_id:
                continue
            acc_id = re.sub(r"[^a-zA-Z0-9_-]", "", acc_id) or "alerts"
            if acc_id in accounts:
                message_box(stdscr, "Already exists", f"Account '{acc_id}' already exists.")
                continue
            name = prompt_text(stdscr, "Add Telegram account", "Optional display name:", initial=acc_id)
            token = prompt_text(stdscr, "Add Telegram account", "Enter bot token (hidden):", secret=True)
            accounts[acc_id] = {"name": name or acc_id, "botToken": token or ""}
            idx = 0
        elif ch in (ord("e"), ord("E")) and keys:
            acc_id = keys[idx]
            cur = accounts[acc_id]
            name = prompt_text(stdscr, "Edit Telegram account", f"Name for '{acc_id}':", initial=cur.get("name", acc_id))
            token = prompt_text(stdscr, "Edit Telegram account", f"Bot token for '{acc_id}' (hidden):", secret=True)
            if name is not None:
                cur["name"] = name or acc_id
            if token is not None:
                cur["botToken"] = token
        elif ch in (ord("d"), ord("D")) and keys:
            acc_id = keys[idx]
            if acc_id == "default" and len(keys) == 1:
                message_box(stdscr, "Cannot delete", "You must have at least one account row; clear the token instead.")
                continue
            if yes_no(stdscr, "Delete account", f"Delete Telegram account '{acc_id}'?", default_yes=False):
                accounts.pop(acc_id, None)
                idx = 0


# -------------------------
# Setup state
# -------------------------

@dataclass
class SetupState:
    repo_url: str = "https://github.com/openclaw/openclaw.git"
    clone_dir: Path = field(default_factory=lambda: expand_path("~/src/openclaw"))

    state_dir: Path = field(default_factory=lambda: expand_path("~/.openclaw"))
    config_path: Path = field(default_factory=lambda: expand_path("~/.openclaw/openclaw.json"))
    dotenv_path: Path = field(default_factory=lambda: expand_path("~/.openclaw/.env"))
    workspace_dir: Path = field(default_factory=lambda: expand_path("~/.openclaw/workspace"))

    ollama_base: str = "http://127.0.0.1:11434"
    ollama_model: str = ""
    pull_model: bool = True

    telegram_enabled: bool = False
    telegram_dm_policy: str = "pairing"
    telegram_accounts: Dict[str, Dict[str, str]] = field(default_factory=dict)

    gateway_port: int = 18789
    gateway_token: str = ""  # required
    start_gateway: bool = True

    # Reverse proxy / tunnel support (Cloudflare Tunnel, nginx, caddy, etc.)
    behind_proxy: bool = False
    trusted_proxies: List[str] = field(default_factory=list)

    # Optional: if you have a public URL (e.g. https://xxxxx.trycloudflare.com)
    public_dashboard_url: str = ""


# -------------------------
# Dependency checks
# -------------------------

def ensure_node_ok() -> Tuple[bool, str]:
    if not which("node"):
        return False, "Missing: node (need Node >= 22)"
    p = subprocess.run(["node", "-v"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    major = parse_node_major(p.stdout)
    if major is None or major < 22:
        return False, f"Node too old: {p.stdout.strip()} (need >= 22)"
    return True, ""

def dependency_screen(stdscr) -> None:
    issues: List[str] = []

    if not which("git"):
        issues.append("Missing: git")

    ok_node, msg_node = ensure_node_ok()
    if not ok_node:
        issues.append(msg_node)

    if not (which("pnpm") or which("corepack")):
        issues.append("Missing: pnpm OR corepack (corepack is usually bundled with node)")

    if not which("ollama"):
        issues.append("Missing: ollama CLI (needed for ollama pull/list)")

    if issues:
        msg = (
            "Some requirements are missing or out of date:\n\n"
            + "\n".join(f"- {i}" for i in issues)
            + "\n\nFix these and re-run.\n\nHints:\n"
              "- Node: install Node 22+ (nvm recommended)\n"
              "- pnpm: `corepack enable` then `corepack prepare pnpm@latest --activate`\n"
              "- Ollama: install and run `ollama serve`\n"
        )
        message_box(stdscr, "Dependency check failed", msg, footer="Press any key to exit")
        raise SystemExit(1)

    message_box(stdscr, "Dependency check", "All required commands were found (git, node>=22, and ollama; pnpm or corepack present).", footer="Press any key to continue")


# -------------------------
# Command runner (live log)
# -------------------------

def run_cmd_live(
    stdscr,
    log: LogBuffer,
    cmd: List[str],
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    title: str = "",
    *,
    check: bool = True,
    abort_is_error: bool = True,
) -> int:
    base_env = os.environ.copy()
    if env:
        base_env.update(env)

    log.add("")
    log.add("$ " + " ".join(shlex.quote(c) for c in cmd))

    stdscr.nodelay(True)
    try:
        p = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=base_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        stdscr.nodelay(False)
        raise RuntimeError(f"Command not found: {cmd[0]}")

    def redraw() -> None:
        stdscr.clear()
        ui_header(stdscr, "Running setup steps", title)
        h, w = stdscr.getmaxyx()
        top = 3
        bottom = h - 2
        height = max(1, bottom - top)
        view = log.lines[-height:]
        for i, line in enumerate(view):
            safe_addstr(stdscr, top + i, 1, line, 0)
        ui_footer(stdscr, "q=Abort   (output is live)")
        stdscr.refresh()

    redraw()
    aborted = False

    assert p.stdout is not None
    for line in p.stdout:
        log.add(line.rstrip("\n"))
        redraw()
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            aborted = True
            break

    if aborted:
        try:
            p.terminate()
            time.sleep(0.2)
            p.kill()
        except Exception:
            pass
        stdscr.nodelay(False)
        if abort_is_error:
            raise RuntimeError("Aborted by user.")
        return 130

    rc = p.wait()
    stdscr.nodelay(False)
    if check and rc != 0:
        raise RuntimeError(f"Command failed (exit {rc}): {' '.join(cmd)}")
    return rc


# -------------------------
# Git clone/update
# -------------------------

def git_clone_or_update(stdscr, log: LogBuffer, state: SetupState, script_path: Path, start_cwd: Path) -> None:
    if state.clone_dir.exists() and dir_has_git(state.clone_dir):
        run_cmd_live(stdscr, log, ["git", "fetch", "--all", "--prune"], cwd=state.clone_dir, title="Updating repo (git fetch)")
        run_cmd_live(stdscr, log, ["git", "checkout", "main"], cwd=state.clone_dir, title="Updating repo (git checkout main)")
        run_cmd_live(stdscr, log, ["git", "pull", "--ff-only"], cwd=state.clone_dir, title="Updating repo (git pull)")
        return

    unsafe_to_delete = is_within(start_cwd, state.clone_dir) or is_within(script_path, state.clone_dir)

    if state.clone_dir.exists():
        if dir_is_empty(state.clone_dir):
            run_cmd_live(stdscr, log, ["git", "clone", state.repo_url, "."], cwd=state.clone_dir, title="Cloning into existing empty directory")
            return

        menu: List[Tuple[str, str]] = []
        if not unsafe_to_delete:
            menu.append((f"Delete '{state.clone_dir}' and re-clone (DANGEROUS)", "delete"))
        menu.append(("Choose a different clone directory", "choose"))
        menu.append(("Abort", "abort"))

        subtitle = "The selected directory exists and is not a git repo."
        if unsafe_to_delete:
            subtitle += " (Delete disabled: directory contains your running script or current cwd.)"

        choice = select_list(stdscr, "Clone directory is not empty", menu, subtitle=subtitle, allow_filter=False, allow_custom=False)

        if choice == "delete":
            ok = yes_no(stdscr, "Confirm delete", f"Really delete this folder and ALL contents?\n\n{state.clone_dir}", default_yes=False)
            if not ok:
                raise SystemExit(0)
            safe_rmtree(state.clone_dir)
            safe_mkdir(state.clone_dir.parent)
            run_cmd_live(stdscr, log, ["git", "clone", state.repo_url, str(state.clone_dir)], cwd=state.clone_dir.parent, title="Cloning OpenClaw")
            return

        if choice == "choose":
            newp = prompt_text(stdscr, "Choose clone directory", "Enter a new clone directory path (it should not exist, or should be empty):", initial=str(state.clone_dir.parent / "openclaw"))
            if not newp:
                raise SystemExit(0)
            state.clone_dir = expand_path(newp)
            return git_clone_or_update(stdscr, log, state, script_path, start_cwd)

        raise SystemExit(0)

    safe_mkdir(state.clone_dir.parent)
    run_cmd_live(stdscr, log, ["git", "clone", state.repo_url, str(state.clone_dir)], cwd=state.clone_dir.parent, title="Cloning OpenClaw")


# -------------------------
# pnpm/corepack bootstrap (non-interactive)
# -------------------------

def ensure_pnpm_for_build(stdscr, log: LogBuffer, state: SetupState, env: Dict[str, str]) -> None:
    if which("pnpm"):
        return
    if not which("corepack"):
        raise RuntimeError("pnpm not found and corepack not available. Install pnpm and retry.")

    env2 = dict(env)
    env2["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"

    run_cmd_live(stdscr, log, ["corepack", "enable"], cwd=state.clone_dir, env=env2, title="corepack enable")
    run_cmd_live(stdscr, log, ["corepack", "prepare", "pnpm@latest", "--activate"], cwd=state.clone_dir, env=env2, title="corepack prepare pnpm@latest --activate")

    if not which("pnpm"):
        raise RuntimeError("pnpm still not found after corepack activation. Install pnpm manually.")


# -------------------------
# Build + Config
# -------------------------

def build_openclaw(stdscr, log: LogBuffer, state: SetupState, env: Dict[str, str]) -> None:
    ensure_pnpm_for_build(stdscr, log, state, env)

    env2 = dict(env)
    env2["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"

    run_cmd_live(stdscr, log, ["pnpm", "install"], cwd=state.clone_dir, env=env2, title="pnpm install")
    run_cmd_live(stdscr, log, ["pnpm", "ui:build"], cwd=state.clone_dir, env=env2, title="pnpm ui:build")
    run_cmd_live(stdscr, log, ["pnpm", "build"], cwd=state.clone_dir, env=env2, title="pnpm build")

def maybe_pull_ollama_model(stdscr, log: LogBuffer, state: SetupState) -> None:
    if state.pull_model and state.ollama_model:
        run_cmd_live(stdscr, log, ["ollama", "pull", state.ollama_model], title=f"ollama pull {state.ollama_model}")

def write_openclaw_files(state: SetupState) -> None:
    safe_mkdir(state.state_dir)
    safe_mkdir(state.workspace_dir)

    # This is convenience; you can "source" it for your shell session.
    update_dotenv(
        state.dotenv_path,
        {
            "OLLAMA_API_KEY": "ollama-local",
            "OLLAMA_BASE_URL": state.ollama_base,
            "OPENCLAW_GATEWAY_TOKEN": state.gateway_token,
            "OPENCLAW_CONFIG_PATH": str(state.config_path),
        },
    )

    base_cfg = read_json_if_exists(state.config_path) or {}

    desired: dict = {
        "gateway": {
            "mode": "local",
            "auth": {"token": state.gateway_token},
        },
        "agents": {
            "defaults": {"model": {"primary": f"ollama/{state.ollama_model}"}},
            "list": [{"id": "main", "default": True, "workspace": str(state.workspace_dir)}],
        },
    }

    if state.behind_proxy and state.trusted_proxies:
        desired.setdefault("gateway", {})["trustedProxies"] = state.trusted_proxies

    if state.telegram_enabled and state.telegram_accounts:
        desired["channels"] = {
            "telegram": {
                "enabled": True,
                "dmPolicy": state.telegram_dm_policy,
                "accounts": state.telegram_accounts,
            }
        }

    deep_merge(base_cfg, desired)
    write_json_config(state.config_path, base_cfg)

def run_postcheck(stdscr, log: LogBuffer, state: SetupState, env: Dict[str, str]) -> None:
    env2 = dict(env)
    env2["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
    run_cmd_live(
        stdscr,
        log,
        ["pnpm", "openclaw", "models", "list"],
        cwd=state.clone_dir,
        env=env2,
        title="Post-check: pnpm openclaw models list",
    )


# -------------------------
# Wizard steps
# -------------------------

def pick_clone_dir(stdscr, state: SetupState) -> None:
    p = prompt_text(
        stdscr,
        "OpenClaw setup",
        "Where should I clone OpenClaw?\n\nExamples:\n- ~/src/openclaw\n- ~/openclaw\n\nIf the folder exists, we'll handle it safely.",
        initial=str(state.clone_dir),
    )
    if not p:
        raise SystemExit(0)
    state.clone_dir = expand_path(p)

def pick_telegram(stdscr, state: SetupState) -> None:
    state.telegram_enabled = yes_no(
        stdscr,
        "Telegram setup",
        "Do you want to configure Telegram bot(s) now?\n\nIf yes, you'll enter one or more bot tokens.",
        default_yes=False,
    )
    if not state.telegram_enabled:
        state.telegram_accounts = {}
        return

    dm = select_list(
        stdscr,
        "Telegram DM policy",
        [
            ("pairing (recommended; unknown senders get a pairing code)", "pairing"),
            ("allowlist (only allowed senders)", "allowlist"),
            ("open (allow all; risky)", "open"),
            ("disabled (ignore DMs)", "disabled"),
        ],
        subtitle="Choose how Telegram DMs are handled",
        allow_filter=False,
        allow_custom=False,
    )
    state.telegram_dm_policy = dm or "pairing"

    accounts = manage_telegram_accounts(stdscr)
    if not accounts:
        state.telegram_enabled = False
        state.telegram_accounts = {}
        message_box(stdscr, "Telegram", "No Telegram tokens were set; Telegram will be skipped.")
        return
    state.telegram_accounts = accounts

def pick_ollama_model(stdscr, state: SetupState) -> None:
    try:
        names = fetch_ollama_tags(state.ollama_base)
    except Exception:
        message_box(
            stdscr,
            "Ollama not reachable",
            "I couldn't reach Ollama at:\n"
            f"  {state.ollama_base}\n\n"
            "Make sure Ollama is running (e.g. `ollama serve`).\n\n"
            "You can still enter a custom model name.",
        )
        chosen = prompt_text(stdscr, "Ollama model", "Enter model name (example: llama3.3):")
        if not chosen:
            raise SystemExit(0)
        state.ollama_model = chosen
        state.pull_model = yes_no(stdscr, "Ollama pull", f"Run `ollama pull {chosen}` now?", default_yes=True)
        return

    if not names:
        message_box(stdscr, "Ollama", "No local models found via /api/tags.\n\nYou can enter a model to pull.")
        chosen = prompt_text(stdscr, "Ollama model", "Enter model name to pull (example: llama3.3):")
        if not chosen:
            raise SystemExit(0)
        state.ollama_model = chosen
        state.pull_model = True
        return

    items = build_model_menu_items(state.ollama_base, names)
    chosen = select_list(
        stdscr,
        "Select Ollama model",
        items,
        subtitle="Arrow keys: move | Enter: pick | Type: filter | c: custom",
        allow_filter=True,
        allow_custom=True,
    )
    if not chosen:
        raise SystemExit(0)
    state.ollama_model = chosen
    state.pull_model = yes_no(stdscr, "Ollama pull", f"Run `ollama pull {chosen}` now?", default_yes=True)

def show_token_howto(stdscr, state: SetupState) -> None:
    tok = state.gateway_token
    port = state.gateway_port
    local_url = f"http://127.0.0.1:{port}/?token={quote(tok)}"
    msg = (
        "This is your Gateway token (admin credential for Dashboard/Control UI + API).\n"
        "Do not share it.\n\n"
        f"Token:\n{tok}\n\n"
        "Where to use it:\n\n"
        "1) Start gateway (known-good):\n"
        f"   pnpm openclaw gateway --port {port} --verbose --allow-unconfigured --token \"{tok}\"\n\n"
        "2) Dashboard / Control UI:\n"
        f"   Open: {local_url}\n"
        "   Or paste token into Control UI settings.\n\n"
        "3) curl / HTTP calls:\n"
        f"   curl -H 'Authorization: Bearer {tok}' http://127.0.0.1:{port}/v1/models\n"
    )
    if state.public_dashboard_url:
        pub_url = f"{state.public_dashboard_url}/?token={quote(tok)}"
        msg += f"\nPublic/tunnel URL (if applicable):\n{pub_url}\n"
    message_box(stdscr, "Gateway token + usage", msg, footer="Press any key")

def pick_gateway_settings(stdscr, state: SetupState) -> None:
    state.start_gateway = yes_no(
        stdscr,
        "Gateway",
        "Start the OpenClaw gateway after setup?\n\nIf yes, it will run in this terminal until you press 'q'.",
        default_yes=True,
    )

    p = prompt_text(stdscr, "Gateway port", "Port for the gateway:", initial=str(state.gateway_port))
    if p is None:
        raise SystemExit(0)
    try:
        port = int(p.strip())
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        port = state.gateway_port

    if not port_is_free("127.0.0.1", port):
        use_anyway = yes_no(stdscr, "Port in use", f"Port {port} appears to be in use.\n\nUse it anyway?", default_yes=False)
        if not use_anyway:
            message_box(stdscr, "Tip", "Pick a different port and re-run this script, or free the port first.")
    state.gateway_port = port

    default_token = "dev-" + secrets.token_urlsafe(18)
    t = prompt_text(
        stdscr,
        "Gateway token",
        "Gateway requires a token.\n\n"
        "This will be written to:\n"
        "- ~/.openclaw/openclaw.json (gateway.auth.token)\n"
        "- ~/.openclaw/.env (OPENCLAW_GATEWAY_TOKEN)\n\n"
        "Choose a token:",
        initial=default_token,
        secret=False,
    )
    if not t:
        raise SystemExit(0)
    state.gateway_token = t.strip()

def pick_proxy_settings(stdscr, state: SetupState) -> None:
    state.behind_proxy = yes_no(
        stdscr,
        "Reverse proxy / Tunnel",
        "Are you accessing the dashboard through a reverse proxy or tunnel?\n\n"
        "Examples:\n"
        "- Cloudflare Tunnel (trycloudflare.com)\n"
        "- nginx/caddy/traefik on the same host\n\n"
        "If yes, we'll set gateway.trustedProxies so OpenClaw can trust X-Forwarded-* headers\n"
        "and avoid the 'Proxy headers detected from untrusted address' warnings.",
        default_yes=False,
    )

    if not state.behind_proxy:
        state.trusted_proxies = []
        state.public_dashboard_url = ""
        return

    # Minimal safe defaults: trust only loopback (common for cloudflared/nginx on same host)
    default_list = ["127.0.0.1/32", "::1/128"]
    state.trusted_proxies = default_list

    extra = prompt_text(
        stdscr,
        "Trusted proxies",
        "Trusted proxy CIDRs/IPs (comma-separated).\n\n"
        "Defaults are loopback only (recommended).\n"
        "Only add the IP/CIDR of the proxy process that connects to OpenClaw.\n\n"
        "Leave blank to keep defaults:",
        initial="",
    )
    if extra:
        parts = [p.strip() for p in extra.split(",") if p.strip()]
        cleaned: List[str] = []
        for p in parts:
            if re.match(r"^[0-9a-fA-F:.]+(?:/\d{1,3})?$", p):
                cleaned.append(p)
        # preserve order, remove dups
        merged = []
        for x in default_list + cleaned:
            if x not in merged:
                merged.append(x)
        state.trusted_proxies = merged

    pub = prompt_text(
        stdscr,
        "Public dashboard URL (optional)",
        "If you have a public URL (e.g. https://xxxxx.trycloudflare.com), paste it here.\n"
        "We’ll show the tokenized URL for convenience.\n\n"
        "Leave blank if you only use localhost:",
        initial="",
    )
    state.public_dashboard_url = (pub or "").strip().rstrip("/")

def final_confirm(stdscr, state: SetupState) -> None:
    summary = [
        f"Clone dir:   {state.clone_dir}",
        f"Config:      {state.config_path}",
        f".env:        {state.dotenv_path}",
        f"Workspace:   {state.workspace_dir}",
        f"Ollama:      {state.ollama_base}",
        f"Model:       {state.ollama_model} (pull={state.pull_model})",
        f"Gateway:     port={state.gateway_port} start={state.start_gateway}",
        f"Token:       {'set' if state.gateway_token else 'MISSING'}",
        f"Proxy/Tunnel:{'yes' if state.behind_proxy else 'no'}",
        f"Telegram:    {'enabled' if state.telegram_enabled else 'skipped'}",
    ]
    ok = yes_no(stdscr, "Confirm", "\n".join(summary) + "\n\nProceed with clone/build/config?", default_yes=True)
    if not ok:
        raise SystemExit(0)


# -------------------------
# Gateway start + tests
# -------------------------

def build_env(state: SetupState) -> Dict[str, str]:
    return {
        "SHARP_IGNORE_GLOBAL_LIBVIPS": "1",
        "OPENCLAW_CONFIG_PATH": str(state.config_path),
        "OLLAMA_API_KEY": "ollama-local",
        "OLLAMA_BASE_URL": state.ollama_base,
        "OPENCLAW_GATEWAY_TOKEN": state.gateway_token,
        "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
    }

def maybe_start_gateway(stdscr, log: LogBuffer, state: SetupState, env: Dict[str, str]) -> None:
    if not state.start_gateway:
        return

    # Avoid buffered 'q' from previous screens killing gateway instantly.
    try:
        curses.flushinp()
    except Exception:
        pass

    cmd = [
        "pnpm", "openclaw", "gateway",
        "--port", str(state.gateway_port),
        "--verbose",
        "--allow-unconfigured",
        "--token", state.gateway_token,
    ]

    env2 = dict(env)
    env2["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"

    rc = run_cmd_live(
        stdscr,
        log,
        cmd,
        cwd=state.clone_dir,
        env=env2,
        title=f"Gateway running on port {state.gateway_port} (press q to stop)",
        check=False,
        abort_is_error=False,  # stopping gateway is normal
    )

    if rc not in (0, 130):
        message_box(
            stdscr,
            "Gateway exited",
            "Gateway exited with a non-zero status.\n\nLast output:\n\n"
            f"{log.tail(60)}",
            footer="Press any key to continue",
        )


# -------------------------
# Orchestration
# -------------------------

def run_all_steps(stdscr, state: SetupState, script_path: Path, start_cwd: Path) -> None:
    log = LogBuffer()
    env = build_env(state)

    try:
        git_clone_or_update(stdscr, log, state, script_path=script_path, start_cwd=start_cwd)
        build_openclaw(stdscr, log, state, env=env)
        maybe_pull_ollama_model(stdscr, log, state)

        log.add("")
        log.add("Writing ~/.openclaw/.env and ~/.openclaw/openclaw.json ...")
        write_openclaw_files(state)
        log.add("Wrote config files.")

        run_postcheck(stdscr, log, state, env=env)
        maybe_start_gateway(stdscr, log, state, env=env)

    except SystemExit:
        raise
    except Exception as e:
        message_box(stdscr, "Setup failed", f"{e}\n\nLast output:\n\n{log.tail(80)}", footer="Press any key to exit")
        raise SystemExit(1)

    tok = state.gateway_token
    port = state.gateway_port
    local_url = f"http://127.0.0.1:{port}/?token={quote(tok)}"
    pub_url = f"{state.public_dashboard_url}/?token={quote(tok)}" if state.public_dashboard_url else ""

    done_msg = (
        "Setup complete.\n\n"
        "KEY YOU NEED (Gateway token):\n"
        f"{tok}\n\n"
        "Where to pass it:\n\n"
        "A) Start gateway:\n"
        f"cd {state.clone_dir}\n"
        f"export OPENCLAW_CONFIG_PATH=\"$HOME/.openclaw/openclaw.json\"\n"
        f"export OLLAMA_API_KEY=\"ollama-local\"\n"
        f"pnpm openclaw gateway --port {port} --verbose --allow-unconfigured --token \"{tok}\"\n\n"
        "B) Control UI / Dashboard:\n"
        f"- Local:  {local_url}\n"
        + (f"- Public: {pub_url}\n" if pub_url else "")
        + "  (Or paste token into Control UI settings.)\n\n"
        "C) curl tests (token required):\n"
        f"curl -sv -H 'Authorization: Bearer {tok}' http://127.0.0.1:{port}/ 2>&1 | head -n 30\n\n"
        "Probe common endpoints:\n"
        "for p in / /health /healthz /ready /readyz /status /version /api/health /api/status /v1/models; do\n"
        "  echo \"== $p\";\n"
        f"  curl -sS -o /dev/null -w \"%{{http_code}}\\n\" -H \"Authorization: Bearer {tok}\" \"http://127.0.0.1:{port}$p\" || true;\n"
        "done\n\n"
        "Ollama quick checks:\n"
        f"curl -s {state.ollama_base}/api/tags | head\n"
        "ollama list\n\n"
        f"Config: {state.config_path}\n"
        f"Env:    {state.dotenv_path}\n"
    )

    message_box(stdscr, "Done", done_msg, footer="Press any key to exit")


# -------------------------
# Main
# -------------------------

def main_curses(stdscr) -> None:
    curses.use_default_colors()
    curses.curs_set(0)
    stdscr.keypad(True)

    script_path = Path(__file__).resolve()
    start_cwd = Path.cwd().resolve()

    state = SetupState()

    message_box(
        stdscr,
        "OpenClaw + Ollama bootstrap wizard",
        "This wizard will clone/build OpenClaw, set Ollama as the primary model provider,\n"
        "configure gateway mode + token (the key you must use in Control UI + curl),\n"
        "optionally configure trusted proxies for Cloudflare/nginx, optionally configure Telegram,\n"
        "and optionally start the gateway.\n\n"
        "During long steps: press 'q' to abort (gateway: 'q' stops it normally).",
        footer="Press any key to begin",
    )

    dependency_screen(stdscr)
    pick_clone_dir(stdscr, state)
    pick_telegram(stdscr, state)
    pick_ollama_model(stdscr, state)
    pick_gateway_settings(stdscr, state)
    pick_proxy_settings(stdscr, state)

    # Now that we have token + (maybe) public URL, show the “how to use the key” screen.
    show_token_howto(stdscr, state)

    final_confirm(stdscr, state)
    run_all_steps(stdscr, state, script_path=script_path, start_cwd=start_cwd)

def main() -> None:
    if sys.platform.startswith("win") and "WSL_DISTRO_NAME" not in os.environ:
        print("This curses wizard expects a proper terminal. On Windows, run it in WSL2.")
        return
    curses.wrapper(main_curses)

if __name__ == "__main__":
    main()
