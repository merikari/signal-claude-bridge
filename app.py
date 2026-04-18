"""Signal → Claude Code bridge (polling daemon).

Polls signal-cli-rest-api for incoming messages, classifies each one,
invokes `claude -p` against the Obsidian vault, and sends a one-line
confirmation back via Signal.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote

import httpx

log = logging.getLogger("signal-bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

VAULT_ROOT = Path(os.environ["VAULT_ROOT"])
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "")  # e.g. claude-sonnet-4-6; empty = CLI default
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))

SIGNAL_API_URL = os.environ.get("SIGNAL_API_URL", "http://127.0.0.1:8080").rstrip("/")
SIGNAL_NUMBER = os.environ["SIGNAL_NUMBER"]  # own E.164 number, e.g. +358...
ALLOWED_SENDERS = {s.strip() for s in os.environ.get("ALLOWED_SENDERS", SIGNAL_NUMBER).split(",") if s.strip()}
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "3"))
SHORT_TOPIC_MAX_TOKENS = int(os.environ.get("SHORT_TOPIC_MAX_TOKENS", "4"))

PROMPTS_DIR = Path(__file__).parent / "prompts"
RESEARCH_PROMPT = (PROMPTS_DIR / "research.md").read_text(encoding="utf-8")
FREEFORM_PROMPT = (PROMPTS_DIR / "freeform.md").read_text(encoding="utf-8")

# Short-topic heuristic: ≤ 4 tokens, no sentence punctuation, ≤ 60 chars
SHORT_TOPIC_RE = re.compile(r"^[^\n.?!]{1,60}$")


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
    log.info("bridge up; signal=%s vault=%s poll=%ss", SIGNAL_NUMBER, VAULT_ROOT, POLL_INTERVAL)
    async with httpx.AsyncClient() as client:
        while True:
            try:
                envelopes = await signal_receive(client)
                for env in envelopes:
                    parsed = extract_message(env)
                    if not parsed:
                        continue
                    sender, text = parsed
                    asyncio.create_task(handle_message(client, sender, text))
            except Exception as e:
                log.warning("receive loop error: %s", e)
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
