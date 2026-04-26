"""Signal → Claude Code bridge (polling daemon).

Polls signal-cli-rest-api for incoming messages, classifies each one,
invokes `claude -p` against the workspace folder, and sends a one-line
confirmation back via Signal.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

import httpx

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

SIGNAL_API_URL = os.environ.get("SIGNAL_API_URL", "http://127.0.0.1:8080").rstrip("/")
SIGNAL_NUMBER = os.environ["SIGNAL_NUMBER"]  # own E.164 number, e.g. +358...
ALLOWED_SENDERS = {s.strip() for s in os.environ.get("ALLOWED_SENDERS", SIGNAL_NUMBER).split(",") if s.strip()}
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "3"))
SHORT_TOPIC_MAX_TOKENS = int(os.environ.get("SHORT_TOPIC_MAX_TOKENS", "4"))

SIGNAL_INBOX = os.environ.get("SIGNAL_INBOX", "Signal inbox")

PROMPTS_DIR = Path(__file__).parent / "prompts"
RESEARCH_PROMPT = (PROMPTS_DIR / "research.md").read_text(encoding="utf-8").replace("{SIGNAL_INBOX}", SIGNAL_INBOX)
FREEFORM_PROMPT = (PROMPTS_DIR / "freeform.md").read_text(encoding="utf-8").replace("{SIGNAL_INBOX}", SIGNAL_INBOX)

# Short-topic heuristic: ≤ 4 tokens, no sentence punctuation, ≤ 60 chars
SHORT_TOPIC_RE = re.compile(r"^[^\n.?!]{1,60}$")

# Re-emit a noisy receive-loop error at WARNING at most once per this many seconds.
# Intermediate occurrences drop to DEBUG so the log doesn't fill up.
RECEIVE_ERROR_REEMIT_SECONDS = 300.0


def is_short_topic(msg: str) -> bool:
    m = msg.strip()
    return bool(SHORT_TOPIC_RE.match(m)) and len(m.split()) <= SHORT_TOPIC_MAX_TOKENS


async def run_claude(system_prompt: str, user_message: str) -> str:
    args = [
        CLAUDE_BIN,
        "-p",
        user_message,
        "--append-system-prompt",
        system_prompt,
        "--permission-mode",
        "bypassPermissions",
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
    return lines[-1][:500]


async def signal_receive(client: httpx.AsyncClient) -> list[dict]:
    """GET /v1/receive returns any queued envelopes since the last call."""
    url = f"{SIGNAL_API_URL}/v1/receive/{quote(SIGNAL_NUMBER, safe='')}"
    r = await client.get(url, timeout=30.0)
    r.raise_for_status()
    data = r.json()
    # The API returns a list; each item has an "envelope" with possibly a dataMessage.
    return data if isinstance(data, list) else []


async def signal_send(client: httpx.AsyncClient, recipient: str, text: str) -> None:
    url = f"{SIGNAL_API_URL}/v2/send"  # send body uses JSON, no URL encoding needed
    payload = {"message": text, "number": SIGNAL_NUMBER, "recipients": [recipient]}
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
    mode = "research" if is_short_topic(text) else "freeform"
    prompt = RESEARCH_PROMPT if mode == "research" else FREEFORM_PROMPT
    log.info("dispatch: mode=%s text=%r", mode, text[:80])
    result = await run_claude(prompt, text)
    await signal_send(client, sender, result)
    log.info("replied: %s", result[:120])


async def main() -> None:
    if not VAULT_ROOT.exists():
        raise RuntimeError(f"VAULT_ROOT does not exist: {VAULT_ROOT}")
    log.info("bridge up; api=%s workspace=%s inbox=%s poll=%ss claude=%s",
             SIGNAL_API_URL, VAULT_ROOT, SIGNAL_INBOX, POLL_INTERVAL, CLAUDE_BIN)

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
