#!/usr/bin/env python3
"""
app.py — OpenClaw + Ollama setup wizard (curses TUI)

Fixes included vs previous version:
1) If `pnpm openclaw gateway ...` exits non-zero, the wizard no longer crashes.
   - It shows the last log lines and continues to the "Done" screen.
   - If you press 'q' to stop the gateway, that's treated as a normal exit (not an error).

2) Safer clone/delete handling:
   - If the chosen clone directory contains the running script OR is your current working directory,
     the wizard will NOT offer "Delete & re-clone" (prevents cwd disappearing / apport issues).

Other features:
- Clones/updates https://github.com/openclaw/openclaw
- Builds from source (pnpm install, pnpm ui:build, pnpm build)
- Curses UI:
  - Telegram bot token ingress (multi-account: channels.telegram.accounts)
  - Arrow-key model picker from local Ollama (/api/tags), Enter to select, type-to-filter
  - Optional: pull the selected model (ollama pull ...)
- Writes:
  - ~/.openclaw/.env          (OLLAMA_API_KEY=ollama-local for Ollama implicit discovery)
  - ~/.openclaw/openclaw.json (sets primary model to ollama/<selected>, telegram config if provided)

Controls:
- Lists: ↑/↓ to move, Enter to select, Esc to go back, type to filter, Backspace to delete
- Long-running steps: press 'q' to abort (gateway stop is treated as normal)

Notes:
- Requires: git, node (>=22), ollama
- pnpm: if missing but corepack is present, wizard will attempt to enable/activate pnpm automatically.
- Best on Linux/macOS terminals. Windows: run in WSL2.
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
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


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
        pass  # best-effort on Windows/odd FS

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
    """True if child path is inside parent (or equals parent)."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False

def port_is_free(host: str, port: int) -> bool:
    """Best-effort local bind test for port availability."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
        return True
    except OSError:
        return False


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
        if any(c.lower() == "thinking" for c in caps):
            flags.append("thinking")
        tag = f" [{' '.join(flags)}]" if flags else ""
        out.append((f"{n}{tag}", n))
    # Prefer tool-capable first
    out.sort(key=lambda lv: (0 if "tools" in lv[0] else 1, lv[0].lower()))
    return out


# -------------------------
# Dotenv + config writers
# -------------------------

def update_dotenv(path: Path, kv: Dict[str, str]) -> None:
    """Upsert keys into a dotenv file without clobbering unrelated keys."""
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

def write_json_config(path: Path, obj: dict) -> None:
    safe_mkdir(path.parent)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    set_private_perms(path)


# -------------------------
# Curses UI
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

    def tail(self, n: int = 40) -> str:
        view = self.lines[-n:]
        return "\n".join(view).strip()


def ui_header(stdscr, title: str, subtitle: str = "") -> None:
    h, w = stdscr.getmaxyx()
    stdscr.attron(curses.A_REVERSE)
    stdscr.addstr(0, 0, " " * (w - 1))
    stdscr.addstr(0, 1, title[: w - 3], curses.A_BOLD)
    stdscr.attroff(curses.A_REVERSE)
    if subtitle:
        stdscr.addstr(1, 1, subtitle[: w - 3])

def ui_footer(stdscr, text: str) -> None:
    h, w = stdscr.getmaxyx()
    stdscr.attron(curses.A_REVERSE)
    stdscr.addstr(h - 1, 0, " " * (w - 1))
    stdscr.addstr(h - 1, 1, text[: w - 3])
    stdscr.attroff(curses.A_REVERSE)

def ui_paragraph(stdscr, y: int, x: int, w: int, text: str) -> int:
    wrapped = textwrap.wrap(text, width=max(10, w))
    for i, line in enumerate(wrapped):
        stdscr.addstr(y + i, x, line)
    return y + len(wrapped)

def message_box(stdscr, title: str, text: str, footer: str = "Press any key") -> None:
    stdscr.clear()
    ui_header(stdscr, title)
    h, w = stdscr.getmaxyx()
    y = 3
    for para in text.split("\n"):
        y = ui_paragraph(stdscr, y, 2, w - 4, para)
        y += 1
    ui_footer(stdscr, footer)
    stdscr.refresh()
    stdscr.getch()

def yes_no(stdscr, title: str, question: str, default_yes: bool = True) -> bool:
    stdscr.clear()
    ui_header(stdscr, title)
    h, w = stdscr.getmaxyx()
    y = 3
    y = ui_paragraph(stdscr, y, 2, w - 4, question)
    y += 2

    choice = 0 if default_yes else 1
    options = ["Yes", "No"]
    while True:
        for i, opt in enumerate(options):
            attr = curses.A_REVERSE if i == choice else curses.A_NORMAL
            stdscr.addstr(y, 2 + i * 8, opt, attr)
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
    y += 1

    buf = list(initial)
    while True:
        stdscr.addstr(y, 2, " " * (w - 4))
        shown = ("*" * len(buf)) if secret else "".join(buf)
        stdscr.addstr(y, 2, shown[: w - 4])
        stdscr.move(y, 2 + min(len(shown), w - 5))
        ui_footer(stdscr, "Enter=OK  Esc=Cancel  Backspace=Delete")
        stdscr.refresh()

        ch = stdscr.getch()
        if ch == 27:  # ESC
            curses.curs_set(0)
            return None
        if ch in (10, 13):  # Enter
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
        list_h = h - 5

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
                stdscr.addstr(list_y + row, 2, label[: w - 4], attr)

        parts = []
        if allow_filter:
            parts.append(f"Filter: {filter_text}" if filter_text else "Type to filter")
        parts.append("↑↓=Move  Enter=Select  Esc=Back")
        if allow_custom:
            parts.append("c=Custom")
        ui_footer(stdscr, "   |   ".join(parts)[: w - 3])
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
    """
    Returns mapping:
      { "default": {"name": "...", "botToken": "..."}, ... }
    """
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
        stdscr.addstr(y, 2, "Accounts:", curses.A_BOLD)
        y += 1

        max_rows = h - 8
        for i, k in enumerate(keys[:max_rows]):
            a = accounts[k]
            label = f"{k:12}  {a.get('name','')[:24]:24}  token={'set' if a.get('botToken') else 'EMPTY'}"
            attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
            stdscr.addstr(y + i, 2, label[: w - 4], attr)

        ui_footer(stdscr, "a=Add  e=Edit  d=Delete  ↑↓=Move  Enter=Done  Esc=Cancel")
        stdscr.refresh()

        ch = stdscr.getch()
        if ch == 27:  # cancel
            return {}
        if ch in (10, 13):  # done
            cleaned = {k: v for k, v in accounts.items() if v.get("botToken")}
            return cleaned
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
# Setup state + dependency checks
# -------------------------

@dataclass
class SetupState:
    repo_url: str = "https://github.com/openclaw/openclaw.git"
    # safer default than ~/openclaw because many people run app.py from ~/openclaw
    clone_dir: Path = field(default_factory=lambda: expand_path("~/src/openclaw"))
    state_dir: Path = field(default_factory=lambda: expand_path("~/.openclaw"))
    config_path: Path = field(default_factory=lambda: expand_path("~/.openclaw/openclaw.json"))
    dotenv_path: Path = field(default_factory=lambda: expand_path("~/.openclaw/.env"))
    workspace_dir: Path = field(default_factory=lambda: expand_path("~/.openclaw/workspace"))
    ollama_base: str = "http://127.0.0.1:11434"
    ollama_model: str = ""
    pull_model: bool = True
    telegram_enabled: bool = False
    telegram_dm_policy: str = "pairing"  # pairing | allowlist | open | disabled
    telegram_accounts: Dict[str, Dict[str, str]] = field(default_factory=dict)
    start_gateway: bool = False
    gateway_port: int = 18789


def ensure_node_ok() -> Tuple[bool, str]:
    if not which("node"):
        return False, "Missing: node (need Node >= 22)"
    p = subprocess.run(["node", "-v"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    major = parse_node_major(p.stdout)
    if major is None or major < 22:
        return False, f"Node too old: {p.stdout.strip()} (need >= 22)"
    return True, ""

def ensure_pnpm_available() -> Tuple[bool, str]:
    if which("pnpm"):
        return True, ""
    if which("corepack"):
        return True, "pnpm not found, but corepack is present (will try to activate pnpm automatically)"
    return False, "Missing: pnpm (install pnpm, or install a Node distro with corepack)"

def dependency_screen(stdscr) -> None:
    issues: List[str] = []
    notes: List[str] = []

    if not which("git"):
        issues.append("Missing: git")
    ok_node, msg_node = ensure_node_ok()
    if not ok_node:
        issues.append(msg_node)
    ok_pnpm, msg_pnpm = ensure_pnpm_available()
    if not ok_pnpm:
        issues.append(msg_pnpm)
    elif msg_pnpm:
        notes.append(msg_pnpm)
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

    ok_msg = "All required commands were found (git, node>=22, ollama)."
    if notes:
        ok_msg += "\n\nNotes:\n" + "\n".join(f"- {n}" for n in notes)
    message_box(stdscr, "Dependency check", ok_msg, footer="Press any key to continue")


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
    """
    Runs a command and streams output into the curses log view.

    - Press 'q' to abort.
    - If `check=True`, nonzero exit raises RuntimeError.
    - If `abort_is_error=False`, abort returns 130 (instead of raising).
    """
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
        height = bottom - top
        view = log.lines[-height:]
        for i, line in enumerate(view):
            stdscr.addstr(top + i, 1, line[: w - 2])
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
# Git clone/update (safer + fixed for exit 128 cases)
# -------------------------

def git_clone_or_update(stdscr, log: LogBuffer, state: SetupState, script_path: Path, start_cwd: Path) -> None:
    """
    Handles:
    - existing git repo -> fetch/pull
    - existing empty dir -> clone into '.' (works even if dir exists)
    - existing non-empty non-git dir -> prompt: choose new dir, abort
      (delete option is only offered if it's safe)
    - non-existing dir -> normal clone
    """
    # Case A: Existing git repo -> update
    if state.clone_dir.exists() and dir_has_git(state.clone_dir):
        run_cmd_live(stdscr, log, ["git", "fetch", "--all", "--prune"], cwd=state.clone_dir, title="Updating repo (git fetch)")
        run_cmd_live(stdscr, log, ["git", "checkout", "main"], cwd=state.clone_dir, title="Updating repo (git checkout main)")
        run_cmd_live(stdscr, log, ["git", "pull", "--ff-only"], cwd=state.clone_dir, title="Updating repo (git pull)")
        return

    unsafe_to_delete = is_within(start_cwd, state.clone_dir) or is_within(script_path, state.clone_dir)

    # Case B: Directory exists but is NOT a git repo
    if state.clone_dir.exists():
        if dir_is_empty(state.clone_dir):
            run_cmd_live(
                stdscr,
                log,
                ["git", "clone", state.repo_url, "."],
                cwd=state.clone_dir,
                title="Cloning into existing empty directory",
            )
            return

        menu: List[Tuple[str, str]] = []
        if not unsafe_to_delete:
            menu.append((f"Delete '{state.clone_dir}' and re-clone (DANGEROUS)", "delete"))
        menu.append(("Choose a different clone directory", "choose"))
        menu.append(("Abort", "abort"))

        subtitle = "The selected directory exists and is not a git repo."
        if unsafe_to_delete:
            subtitle += " (Delete disabled: this directory contains your running script or current shell cwd.)"

        choice = select_list(
            stdscr,
            "Clone directory is not empty",
            menu,
            subtitle=subtitle,
            allow_filter=False,
            allow_custom=False,
        )

        if choice == "delete":
            ok = yes_no(
                stdscr,
                "Confirm delete",
                f"Really delete this folder and ALL contents?\n\n{state.clone_dir}",
                default_yes=False,
            )
            if not ok:
                raise SystemExit(0)
            safe_rmtree(state.clone_dir)
            safe_mkdir(state.clone_dir.parent)
            run_cmd_live(
                stdscr,
                log,
                ["git", "clone", state.repo_url, str(state.clone_dir)],
                cwd=state.clone_dir.parent,
                title="Cloning OpenClaw",
            )
            return

        if choice == "choose":
            newp = prompt_text(
                stdscr,
                "Choose clone directory",
                "Enter a new clone directory path (it should not exist, or should be empty):",
                initial=str(state.clone_dir.parent / "openclaw"),
            )
            if not newp:
                raise SystemExit(0)
            state.clone_dir = expand_path(newp)
            return git_clone_or_update(stdscr, log, state, script_path, start_cwd)

        raise SystemExit(0)

    # Case C: Directory doesn't exist -> normal clone
    safe_mkdir(state.clone_dir.parent)
    run_cmd_live(
        stdscr,
        log,
        ["git", "clone", state.repo_url, str(state.clone_dir)],
        cwd=state.clone_dir.parent,
        title="Cloning OpenClaw",
    )


# -------------------------
# Build + config
# -------------------------

def ensure_pnpm_for_build(stdscr, log: LogBuffer, state: SetupState) -> None:
    if which("pnpm"):
        return
    if not which("corepack"):
        raise RuntimeError("pnpm not found and corepack not available. Install pnpm and retry.")
    run_cmd_live(stdscr, log, ["corepack", "enable"], cwd=state.clone_dir, title="corepack enable")
    run_cmd_live(stdscr, log, ["corepack", "prepare", "pnpm@latest", "--activate"], cwd=state.clone_dir, title="corepack prepare pnpm@latest --activate")
    if not which("pnpm"):
        raise RuntimeError("pnpm still not found after corepack activation. Install pnpm manually.")

def build_openclaw(stdscr, log: LogBuffer, state: SetupState, env: Dict[str, str]) -> None:
    ensure_pnpm_for_build(stdscr, log, state)
    run_cmd_live(stdscr, log, ["pnpm", "install"], cwd=state.clone_dir, env=env, title="pnpm install")
    run_cmd_live(stdscr, log, ["pnpm", "ui:build"], cwd=state.clone_dir, env=env, title="pnpm ui:build")
    run_cmd_live(stdscr, log, ["pnpm", "build"], cwd=state.clone_dir, env=env, title="pnpm build")

def maybe_pull_ollama_model(stdscr, log: LogBuffer, state: SetupState) -> None:
    if state.pull_model and state.ollama_model:
        run_cmd_live(stdscr, log, ["ollama", "pull", state.ollama_model], title=f"ollama pull {state.ollama_model}")

def write_openclaw_files(state: SetupState) -> None:
    safe_mkdir(state.state_dir)
    safe_mkdir(state.workspace_dir)

    update_dotenv(state.dotenv_path, {"OLLAMA_API_KEY": "ollama-local"})

    cfg: dict = {
        "agents": {
            "defaults": {
                "model": {"primary": f"ollama/{state.ollama_model}"}
            },
            "list": [
                {"id": "main", "default": True, "workspace": str(state.workspace_dir)}
            ],
        }
    }

    if state.telegram_enabled and state.telegram_accounts:
        cfg["channels"] = {
            "telegram": {
                "enabled": True,
                "dmPolicy": state.telegram_dm_policy,
                "accounts": state.telegram_accounts,
            }
        }

    write_json_config(state.config_path, cfg)

def run_postcheck(stdscr, log: LogBuffer, state: SetupState, env: Dict[str, str]) -> None:
    run_cmd_live(
        stdscr,
        log,
        ["pnpm", "openclaw", "models", "list"],
        cwd=state.clone_dir,
        env=env,
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
        default_yes=True,
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

def pick_gateway_port_if_needed(stdscr, state: SetupState) -> None:
    if not state.start_gateway:
        return
    # keep it simple: only validate once here; if busy, prompt another port
    while True:
        p = prompt_text(
            stdscr,
            "Gateway port",
            "Enter the port to run the OpenClaw gateway on:",
            initial=str(state.gateway_port),
        )
        if p is None:
            # user cancelled this prompt -> don't start gateway
            state.start_gateway = False
            return
        try:
            port = int(p.strip())
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            message_box(stdscr, "Invalid port", "Please enter a number between 1 and 65535.")
            continue

        # quick local check (best-effort)
        if not port_is_free("127.0.0.1", port):
            message_box(
                stdscr,
                "Port in use",
                f"Port {port} appears to be in use on 127.0.0.1.\n\nPick a different port.",
            )
            continue

        state.gateway_port = port
        return

def final_confirm(stdscr, state: SetupState) -> None:
    summary = []
    summary.append(f"Clone dir:  {state.clone_dir}")
    summary.append(f"Config:     {state.config_path}")
    summary.append(f".env:       {state.dotenv_path}")
    summary.append(f"Workspace:  {state.workspace_dir}")
    summary.append(f"Ollama:     {state.ollama_base}")
    summary.append(f"Model:      {state.ollama_model} (pull={state.pull_model})")
    if state.telegram_enabled:
        summary.append(f"Telegram:   enabled (dmPolicy={state.telegram_dm_policy}) accounts={', '.join(sorted(state.telegram_accounts.keys()))}")
    else:
        summary.append("Telegram:   skipped")
    summary.append("")
    summary.append("Proceed with clone/build/config write?")

    ok = yes_no(stdscr, "Confirm", "\n".join(summary), default_yes=True)
    if not ok:
        raise SystemExit(0)

    state.start_gateway = yes_no(
        stdscr,
        "Start gateway",
        "After setup finishes, do you want to start the OpenClaw gateway now?\n\n"
        "If yes, it will run in this terminal until you press 'q'.",
        default_yes=False,
    )

def maybe_start_gateway(stdscr, log: LogBuffer, state: SetupState, env: Dict[str, str]) -> None:
    if not state.start_gateway:
        return

    # If it fails, do NOT crash the wizard—show tail and continue.
    cmd = ["pnpm", "openclaw", "gateway", "--port", str(state.gateway_port), "--verbose"]
    try:
        rc = run_cmd_live(
            stdscr,
            log,
            cmd,
            cwd=state.clone_dir,
            env=env,
            title=f"Gateway running on port {state.gateway_port} (press q to stop)",
            check=False,
            abort_is_error=False,  # stopping the gateway is normal
        )
        if rc not in (0, 130):
            message_box(
                stdscr,
                "Gateway exited",
                "The gateway exited with a non-zero status.\n\n"
                "Last output:\n\n"
                f"{log.tail(60)}\n\n"
                "Common causes:\n"
                "- Port already in use\n"
                "- Missing/invalid config\n"
                "- Node runtime error\n\n"
                "You can try starting it manually:\n"
                f"  cd {state.clone_dir}\n"
                f"  {' '.join(cmd)}\n",
                footer="Press any key to continue",
            )
    except Exception as e:
        message_box(
            stdscr,
            "Gateway failed to start",
            f"{e}\n\nLast output:\n\n{log.tail(60)}\n\n"
            "You can try starting it manually from another terminal:\n"
            f"  cd {state.clone_dir}\n"
            f"  {' '.join(cmd)}\n",
            footer="Press any key to continue",
        )


# -------------------------
# Run all steps (no traceback spam)
# -------------------------

def run_all_steps(stdscr, state: SetupState, script_path: Path, start_cwd: Path) -> None:
    log = LogBuffer()

    env = {
        "SHARP_IGNORE_GLOBAL_LIBVIPS": "1",
        "OPENCLAW_CONFIG_PATH": str(state.config_path),
        "OLLAMA_API_KEY": "ollama-local",
    }

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
        message_box(
            stdscr,
            "Setup failed",
            f"{e}\n\nLast output:\n\n{log.tail(80)}",
            footer="Press any key to exit",
        )
        raise SystemExit(1)

    message_box(
        stdscr,
        "Done",
        "Setup complete.\n\n"
        "Next steps:\n"
        f"1) (Optional) Start the gateway:\n"
        f"   cd {state.clone_dir}\n"
        f"   pnpm openclaw gateway --port {state.gateway_port} --verbose\n\n"
        "2) Confirm models:\n"
        "   pnpm openclaw models list\n\n"
        f"Config written to:\n  {state.config_path}\n"
        f"Env written to:\n  {state.dotenv_path}\n",
        footer="Press any key to exit",
    )


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
        "OpenClaw + Ollama setup wizard",
        "This wizard will clone and build OpenClaw from source, configure Ollama (implicit discovery),\n"
        "and optionally configure Telegram bot tokens.\n\n"
        "Controls:\n"
        "- Lists: arrow keys + Enter\n"
        "- Type to filter lists\n"
        "- Esc cancels screens\n"
        "- During long-running commands, press 'q' to abort\n"
        "  (Stopping the gateway with 'q' is normal.)",
        footer="Press any key to begin",
    )

    dependency_screen(stdscr)
    pick_clone_dir(stdscr, state)
    pick_telegram(stdscr, state)
    pick_ollama_model(stdscr, state)
    final_confirm(stdscr, state)
    pick_gateway_port_if_needed(stdscr, state)
    run_all_steps(stdscr, state, script_path=script_path, start_cwd=start_cwd)

def main() -> None:
    if sys.platform.startswith("win"):
        if "WSL_DISTRO_NAME" not in os.environ and "TERM" not in os.environ:
            print("This curses wizard expects a proper terminal. On Windows, run it in WSL2.")
            return
    curses.wrapper(main_curses)

if __name__ == "__main__":
    main()
