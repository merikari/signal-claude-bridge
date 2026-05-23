"""Signal → Claude Code bridge (polling daemon).

Polls signal-cli-rest-api for incoming messages, classifies each one,
invokes `claude -p` against the workspace folder, and sends a one-line
confirmation back via Signal.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

import httpx

import intent_dispatcher

# --- Logging: rotating file + stderr (so any stream redirect still catches crashes) ---
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_PATH = LOG_DIR / "bridge.log"

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_root = logging.getLogger()
_root.setLevel(logging.INFO)
# Avoid double-logging if the module is reloaded
if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in _root.handlers):
    _file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    _root.addHandler(_file_handler)
if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.handlers.RotatingFileHandler)
           for h in _root.handlers):
    # stderr is captured to logs/bridge-stderr.log by run-hidden.ps1 — keep it
    # at WARNING+ so it only collects real problems and unhandled tracebacks,
    # not every httpx INFO line (those go to the rotating bridge.log instead).
    _stream_handler = logging.StreamHandler()
    _stream_handler.setLevel(logging.WARNING)
    _stream_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    _root.addHandler(_stream_handler)

log = logging.getLogger("signal-bridge")

VAULT_ROOT = Path(os.environ["VAULT_ROOT"])
_CLAUDE_BIN_RAW = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "")  # e.g. claude-sonnet-4-6; empty = CLI default
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))


def _resolve_claude_bin(value: str) -> str:
    """Resolve CLAUDE_BIN to an absolute path that exists.

    Order:
      1. If `value` is an absolute path that exists → use it.
      2. shutil.which(value) — covers PATH-resolvable names.
      3. Probe standard Claude Code install dirs and pick the highest version.
         This survives auto-updates that change the version subdir.
    Raises RuntimeError if nothing is found, so the bridge fails fast at startup
    rather than silently dropping every incoming message.
    """
    p = Path(value)
    if p.is_absolute() and p.exists():
        return str(p)

    found = shutil.which(value)
    if found:
        return found

    # Standard Claude Code install root on Windows: %APPDATA%\Claude\claude-code\<version>\claude.exe
    candidate_roots = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidate_roots.append(Path(appdata) / "Claude" / "claude-code")
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        candidate_roots.append(Path(localappdata) / "Programs" / "claude-code")

    def _version_key(name: str) -> tuple:
        parts = re.findall(r"\d+", name)
        return tuple(int(x) for x in parts) if parts else (0,)

    for root in candidate_roots:
        if not root.is_dir():
            continue
        versions = sorted([d for d in root.iterdir() if d.is_dir()],
                          key=lambda d: _version_key(d.name), reverse=True)
        for vdir in versions:
            exe = vdir / "claude.exe"
            if exe.exists():
                return str(exe)

    raise RuntimeError(
        f"Cannot resolve CLAUDE_BIN={value!r}. Not absolute-path-existing, not on PATH, "
        f"and no claude.exe found under standard install roots. "
        f"Set CLAUDE_BIN in .env to an absolute path."
    )


CLAUDE_BIN = _resolve_claude_bin(_CLAUDE_BIN_RAW)

# Built-in tool set for the spawned claude session.
#
# Use --tools (not --allowed-tools): --allowed-tools only auto-grants
# permissions and is silently overridden by user-level allow rules
# (e.g. ~/.claude/settings.json with "Bash(*)"). --tools controls which
# built-in tools exist in the session at all, so they cannot be invoked
# regardless of permission settings.
#
# Bash, Task/Agent, NotebookEdit, and TodoWrite are deliberately
# excluded. The bridge runs with --permission-mode bypassPermissions
# because it is non-interactive (any prompt would hang); --tools is the
# real boundary against prompt-injected webpages telling Claude to
# read ~/.ssh/id_rsa, exfiltrate via shell, etc.
#
# Note: --tools restricts only built-in tools; MCP-server tools
# (mcp__*) still load if user/project settings configure them. Path
# scoping for Read/Write and dropping WebFetch are separate, larger
# fixes to consider next.
#
# Override via CLAUDE_TOOLS in .env if a specific use case needs more.
CLAUDE_TOOLS = os.environ.get(
    "CLAUDE_TOOLS",
    "Read,Write,Edit,Glob,Grep,WebSearch,WebFetch",
)

SIGNAL_API_URL = os.environ.get("SIGNAL_API_URL", "http://127.0.0.1:8090").rstrip("/")
SIGNAL_NUMBER = os.environ["SIGNAL_NUMBER"]  # own E.164 number, e.g. +358...
ALLOWED_SENDERS = {s.strip() for s in os.environ.get("ALLOWED_SENDERS", SIGNAL_NUMBER).split(",") if s.strip()}
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "3"))
SHORT_TOPIC_MAX_TOKENS = int(os.environ.get("SHORT_TOPIC_MAX_TOKENS", "4"))

SIGNAL_INBOX = os.environ.get("SIGNAL_INBOX", "Signal inbox")
ATTACH_MD = os.environ.get("ATTACH_MD", "true").lower() in ("true", "1", "yes")
HISTORY_DEPTH = int(os.environ.get("HISTORY_DEPTH", "5"))

PROMPTS_DIR = Path(__file__).parent / "prompts"
RESEARCH_PROMPT = (PROMPTS_DIR / "research.md").read_text(encoding="utf-8").replace("{SIGNAL_INBOX}", SIGNAL_INBOX)
FREEFORM_PROMPT = (PROMPTS_DIR / "freeform.md").read_text(encoding="utf-8").replace("{SIGNAL_INBOX}", SIGNAL_INBOX)
URL_PROMPT = (PROMPTS_DIR / "url.md").read_text(encoding="utf-8").replace("{SIGNAL_INBOX}", SIGNAL_INBOX)

# Short-topic heuristic: ≤ 4 tokens, no sentence punctuation, ≤ 60 chars
SHORT_TOPIC_RE = re.compile(r"^[^\n.?!]{1,60}$")

# Bare-URL heuristic: message is a single http(s) link with no surrounding text.
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

# Re-emit a noisy receive-loop error at WARNING at most once per this many seconds.
# Intermediate occurrences drop to DEBUG so the log doesn't fill up.
RECEIVE_ERROR_REEMIT_SECONDS = 300.0


# --- Skill injection (optional, config-driven) ---

def _load_skills() -> list[dict]:
    """Load skill definitions from skills.json (relative to the bridge repo dir).

    Returns a list of validated skill dicts with resolved absolute paths,
    or an empty list if the file is missing or invalid.
    """
    skills_path = Path(__file__).parent / "skills.json"
    if not skills_path.exists():
        log.info("skills: no skills.json found — skill injection disabled")
        return []
    try:
        raw = json.loads(skills_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("skills: failed to parse skills.json: %s", e)
        return []

    loaded = []
    for entry in raw.get("skills", []):
        name = entry.get("name", "<unnamed>")
        keywords = [kw.lower() for kw in entry.get("keywords", [])]
        skill_path = VAULT_ROOT / entry.get("skill_path", "")
        extra_tools = entry.get("extra_tools", [])
        if not keywords:
            log.warning("skills: skipping %r — no keywords", name)
            continue
        if not skill_path.exists():
            log.warning("skills: skipping %r — %s not found", name, skill_path)
            continue
        loaded.append({
            "name": name,
            "keywords": keywords,
            "skill_path": skill_path,
            "extra_tools": extra_tools,
        })
    log.info("skills: loaded %d skill(s): %s", len(loaded), ", ".join(s["name"] for s in loaded))
    return loaded


SKILLS = _load_skills()


def match_skills(msg: str) -> list[dict]:
    """Return all skills whose keywords appear in the message (case-insensitive)."""
    lower = msg.lower()
    return [s for s in SKILLS if any(kw in lower for kw in s["keywords"])]


# --- Context injection (message history + referential detection) ---

_HISTORY_MAX_ENTRIES = 100

REFERENTIAL_WORDS = {
    "that", "previous", "last", "more detail", "expand", "elaborate",
    "lisää", "edellinen", "viime", "tarkenna", "laajenna", "sama",
}

_HISTORY_PATH: Path | None = None


def _get_history_path() -> Path:
    global _HISTORY_PATH
    if _HISTORY_PATH is None:
        _HISTORY_PATH = VAULT_ROOT / SIGNAL_INBOX / ".bridge-history.jsonl"
    return _HISTORY_PATH


def append_history(msg: str, mode: str, result: str) -> None:
    """Append a history entry and trim to _HISTORY_MAX_ENTRIES."""
    from datetime import datetime, timezone
    path = _get_history_path()
    entry = json.dumps({
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "msg": msg[:200],
        "mode": mode,
        "result": result[:300],
    }, ensure_ascii=False)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > _HISTORY_MAX_ENTRIES:
            path.write_text("\n".join(lines[-_HISTORY_MAX_ENTRIES:]) + "\n", encoding="utf-8")
    except Exception as e:
        log.debug("history write failed: %s", e)


def _is_referential(msg: str) -> bool:
    lower = msg.lower()
    return any(w in lower for w in REFERENTIAL_WORDS)


def load_history_context(msg: str) -> str:
    """Build a context string from recent history for the system prompt."""
    path = _get_history_path()
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return ""
    if not lines:
        return ""

    recent = lines[-HISTORY_DEPTH:]
    entries = []
    last_result_file: str | None = None
    for line in recent:
        try:
            e = json.loads(line)
            entries.append(f"- [{e['ts']}] ({e['mode']}) \"{e['msg']}\" → {e['result']}")
            result_str = e.get("result", "")
            if result_str.startswith("OK:"):
                parts = result_str.split("—")
                fname = parts[0].replace("OK:", "").strip()
                if fname:
                    last_result_file = fname
        except Exception:
            continue
    if not entries:
        return ""

    ctx = "## Recent messages from this sender\n\n" + "\n".join(entries)

    if _is_referential(msg) and last_result_file:
        note_path = VAULT_ROOT / SIGNAL_INBOX / last_result_file
        if note_path.exists():
            try:
                content = note_path.read_text(encoding="utf-8")[:500]
                ctx += f"\n\n## Content of most recent note ({last_result_file})\n\n{content}"
            except Exception:
                pass

    memory_path = VAULT_ROOT / SIGNAL_INBOX / ".bridge-memory.md"
    if memory_path.exists():
        try:
            mem = memory_path.read_text(encoding="utf-8").strip()
            if mem:
                ctx += f"\n\n## Persistent memory\n\n{mem}"
        except Exception:
            pass

    return ctx


def is_url(msg: str) -> bool:
    return bool(URL_RE.match(msg.strip()))


def is_short_topic(msg: str) -> bool:
    m = msg.strip()
    return bool(SHORT_TOPIC_RE.match(m)) and len(m.split()) <= SHORT_TOPIC_MAX_TOKENS


def _inbox_snapshot() -> set[Path]:
    inbox = VAULT_ROOT / SIGNAL_INBOX
    return set(inbox.glob("*.md")) if inbox.is_dir() else set()


def _encode_attachment(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:text/plain;filename={path.name};base64,{b64}"
    except Exception as e:
        log.warning("attach encode failed for %s: %s", path.name, e)
        return None


async def run_claude(
    system_prompt: str,
    user_message: str,
    extra_tools: list[str] | None = None,
) -> str:
    tools = CLAUDE_TOOLS
    if extra_tools:
        tools = ",".join(dict.fromkeys((tools + "," + ",".join(extra_tools)).split(",")))
    # Re-resolve per call so Claude Code auto-updates (which replace the versioned
    # install dir under %APPDATA%\Claude\claude-code\) don't leave us with a stale
    # path cached at startup.
    claude_bin = _resolve_claude_bin(_CLAUDE_BIN_RAW)
    args = [
        claude_bin,
        "-p",
        user_message,
        "--append-system-prompt",
        system_prompt,
        "--permission-mode",
        "bypassPermissions",
        "--tools",
        tools,
    ]
    if CLAUDE_MODEL:
        args += ["--model", CLAUDE_MODEL]
    log.info("claude start: model=%s msg=%r", CLAUDE_MODEL or "default", user_message[:80])
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(VAULT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return "FAIL: claude subprocess timed out"

    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        log.warning("claude exit=%s stderr=%s", proc.returncode, stderr[:500])
        return f"FAIL: claude exited {proc.returncode}: {stderr[:200]}"

    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return "FAIL: claude produced no output"
    # Prefer the last line starting with OK: or FAIL: — Claude sometimes outputs
    # extra text (URLs, preamble) after the sentinel.  Falling back to lines[-1]
    # preserves original behaviour when no sentinel is present.
    for line in reversed(lines):
        if line.startswith(("OK:", "FAIL:")):
            return line[:500]
    return lines[-1][:500]


async def signal_receive(client: httpx.AsyncClient) -> list[dict]:
    """GET /v1/receive returns any queued envelopes since the last call."""
    url = f"{SIGNAL_API_URL}/v1/receive/{quote(SIGNAL_NUMBER, safe='')}"
    r = await client.get(url, timeout=30.0)
    r.raise_for_status()
    data = r.json()
    # The API returns a list; each item has an "envelope" with possibly a dataMessage.
    return data if isinstance(data, list) else []


async def signal_send(
    client: httpx.AsyncClient,
    recipient: str,
    text: str,
    attachment: str | None = None,
) -> None:
    url = f"{SIGNAL_API_URL}/v2/send"
    payload = {"message": text, "number": SIGNAL_NUMBER, "recipients": [recipient]}
    if attachment:
        payload["base64_attachments"] = [attachment]
    try:
        r = await client.post(url, json=payload, timeout=15.0)
        r.raise_for_status()
    except Exception as e:
        log.warning("signal send failed: %s", e)


def extract_message(envelope: dict) -> tuple[str, str] | None:
    """Return (sender, text) if this envelope is a usable inbound message."""
    env = envelope.get("envelope") or envelope
    source = env.get("sourceNumber") or env.get("source")
    data_msg = env.get("dataMessage") or {}
    sync_msg = (env.get("syncMessage") or {}).get("sentMessage") or {}
    text = data_msg.get("message") or sync_msg.get("message")
    # For "Note to Self" (messages sent from the phone to self), syncMessage is used.
    if sync_msg and not text:
        return None
    if not text or not source:
        return None
    return source, text


async def handle_message(client: httpx.AsyncClient, sender: str, text: str) -> None:
    if sender not in ALLOWED_SENDERS:
        log.warning("drop: sender %s not in allowlist", sender)
        return

    # Direct intent dispatch — config-driven HTTP pipelines that bypass Claude
    # for clearly-shaped actions. See intents.json / intents.example.json.
    matched = intent_dispatcher.match_intent(text)
    if matched:
        intent, groups = matched
        log.info("dispatch: intent=%s groups=%s", intent["name"], list(groups.keys()))
        result = await intent_dispatcher.run_intent(intent, groups)
        append_history(text, f"intent:{intent['name']}", result)
        await signal_send(client, sender, result, None)
        log.info("replied: %s", result[:120])
        return

    if is_url(text):
        mode = "url"
    elif is_short_topic(text):
        mode = "research"
    else:
        mode = "freeform"
    prompt = {"research": RESEARCH_PROMPT, "freeform": FREEFORM_PROMPT, "url": URL_PROMPT}[mode]

    matched = match_skills(text)
    extra_tools: list[str] = []
    if matched:
        skill_names = [s["name"] for s in matched]
        log.info("dispatch: mode=%s skills=%s text=%r", mode, skill_names, text[:80])
        for s in matched:
            skill_content = s["skill_path"].read_text(encoding="utf-8")
            prompt += f"\n\n---\n\n# Skill: {s['name']}\n\n{skill_content}"
            extra_tools.extend(s["extra_tools"])
    else:
        log.info("dispatch: mode=%s text=%r", mode, text[:80])

    history_ctx = load_history_context(text)
    if history_ctx:
        prompt = history_ctx + "\n\n---\n\n" + prompt

    before = _inbox_snapshot() if ATTACH_MD else set()

    result = await run_claude(prompt, text, extra_tools=extra_tools or None)

    append_history(text, mode, result)

    attachment = None
    if ATTACH_MD and result.startswith("OK:"):
        new_files = sorted(_inbox_snapshot() - before,
                           key=lambda p: p.stat().st_mtime, reverse=True)
        if new_files:
            attachment = _encode_attachment(new_files[0])
            if attachment:
                log.info("attaching: %s", new_files[0].name)

    await signal_send(client, sender, result, attachment)
    log.info("replied: %s", result[:120])


async def main() -> None:
    if not VAULT_ROOT.exists():
        raise RuntimeError(f"VAULT_ROOT does not exist: {VAULT_ROOT}")
    # Eager-load intents now that logging is configured, so the "loaded N intents"
    # line shows up at startup rather than on first matching message.
    intent_dispatcher.preload()
    log.info("bridge up; api=%s workspace=%s inbox=%s poll=%ss claude=%s tools=%s",
             SIGNAL_API_URL, VAULT_ROOT, SIGNAL_INBOX, POLL_INTERVAL, CLAUDE_BIN, CLAUDE_TOOLS)

    last_error_emit = 0.0
    last_error_msg = ""
    suppressed_count = 0

    async with httpx.AsyncClient() as client:
        while True:
            try:
                envelopes = await signal_receive(client)
                if last_error_msg:
                    # Recovered — log once with the suppressed count
                    log.info("receive loop recovered (suppressed %d repeats)", suppressed_count)
                    last_error_msg = ""
                    suppressed_count = 0
                for env in envelopes:
                    parsed = extract_message(env)
                    if not parsed:
                        continue
                    sender, text = parsed
                    asyncio.create_task(handle_message(client, sender, text))
            except Exception as e:
                msg = str(e)
                now = asyncio.get_event_loop().time()
                if msg != last_error_msg or (now - last_error_emit) > RECEIVE_ERROR_REEMIT_SECONDS:
                    extra = f" (suppressed {suppressed_count} repeats)" if suppressed_count else ""
                    log.warning("receive loop error: %s%s", msg, extra)
                    last_error_emit = now
                    last_error_msg = msg
                    suppressed_count = 0
                else:
                    suppressed_count += 1
                    log.debug("receive loop error (suppressed): %s", msg)
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
