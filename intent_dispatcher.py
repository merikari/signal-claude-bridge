"""Generic config-driven intent dispatcher for the Signal bridge.

Reads `intents.json` (gitignored, user-specific) and executes regex-matched
multi-step HTTP pipelines without invoking Claude. Service-specific logic
(Radarr, Sonarr, webhooks, etc.) lives in the JSON config — the bridge itself
stays generic.

Schema: see `intents.example.json` for a worked example.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("signal-bridge")

INTENTS_PATH = Path(__file__).parent / "intents.json"
_FLAG_MAP = {
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
    "VERBOSE": re.VERBOSE,
}


def _load_intents() -> list[dict]:
    if not INTENTS_PATH.exists():
        log.info("intents: no intents.json found — intent dispatch disabled")
        return []
    try:
        raw = json.loads(INTENTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.warning("intents: failed to parse intents.json: %s", e)
        return []
    loaded = []
    for entry in raw.get("intents", []):
        name = entry.get("name", "?")
        try:
            flags = 0
            for f in (entry.get("match_flags") or "").split("|"):
                f = f.strip()
                if f:
                    flags |= _FLAG_MAP[f]
            entry["_re"] = re.compile(entry["match"], flags)
        except (re.error, KeyError) as e:
            log.warning("intents: skipping %r — %s", name, e)
            continue
        loaded.append(entry)
    if loaded:
        log.info("intents: loaded %d intent(s): %s",
                 len(loaded), ", ".join(i["name"] for i in loaded))
    return loaded


# Lazily populated on first match_intent call so the load is logged *after*
# the bridge configures its logging handlers (this module is imported before
# logging setup runs in app.py).
_INTENTS: list[dict] | None = None


def _envsub(s: str) -> str:
    """Replace ${VAR} with os.environ[VAR] (empty string if unset)."""
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), s)


def _resolve(expr: str, ctx: Any) -> Any:
    """Resolve a dotted/bracketed path like `lookup.title` or `[0].name` against ctx."""
    cur: Any = ctx
    for part in re.findall(r"[^.\[\]]+", expr):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            cur = cur[int(part)] if part.lstrip("-").isdigit() and -len(cur) <= int(part) < len(cur) else None
        else:
            cur = getattr(cur, part, None)
    return cur


def _render(value: Any, ctx: dict) -> Any:
    """Render {placeholders} inside strings; recurse into dicts/lists.

    A string that is exactly one placeholder returns the raw referenced value
    (preserves int/bool/etc). Mixed text returns a string.

    The negative lookbehind `(?<!\\$)` ensures we do not match the `{VAR}`
    portion of an env-substitution token `${VAR}` — those are handled by
    _envsub later in the pipeline.
    """
    if isinstance(value, str):
        whole = re.fullmatch(r"(?<!\$)\{([^{}]+)\}", value)
        if whole:
            return _resolve(whole.group(1), ctx)
        return re.sub(r"(?<!\$)\{([^{}]+)\}",
                      lambda m: str(_resolve(m.group(1), ctx) or ""),
                      value)
    if isinstance(value, dict):
        return {k: _render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, ctx) for v in value]
    return value


def preload() -> None:
    """Force intents to load now. Call after logging is configured so the
    'loaded N intent(s)' log line is visible at startup."""
    global _INTENTS
    if _INTENTS is None:
        _INTENTS = _load_intents()


def match_intent(text: str) -> tuple[dict, dict] | None:
    """Return (intent, named_groups) if text matches an intent, else None."""
    global _INTENTS
    if _INTENTS is None:
        _INTENTS = _load_intents()
    for intent in _INTENTS:
        m = intent["_re"].match(text.strip())
        if m:
            groups = {k: v for k, v in m.groupdict().items() if v is not None}
            # Convenience: expose the first non-None named group as `value`
            # so intents with one or many alternation branches share one ref.
            if "value" not in groups and groups:
                groups["value"] = next(iter(groups.values()))
            return intent, groups
    return None


async def run_intent(intent: dict, groups: dict) -> str:
    """Execute an intent's steps. Returns an `OK: ...` / `FAIL: ...` reply."""
    ctx: dict[str, Any] = dict(groups)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for step in intent.get("steps", []):
                if "skip_if" in step:
                    if _resolve(step["skip_if"], ctx):
                        if "skip_reply" in step:
                            return str(_render(step["skip_reply"], ctx))
                        continue

                # Pure-reply step (no HTTP) — useful for early returns.
                if "url" not in step:
                    if "reply" in step:
                        return str(_render(step["reply"], ctx))
                    continue

                method = step.get("method", "GET").upper()
                url = _envsub(str(_render(step["url"], ctx)))
                headers = {
                    k: _envsub(str(_render(v, ctx)))
                    for k, v in step.get("headers", {}).items()
                }
                params = _render(step.get("params"), ctx) if "params" in step else None
                body = _render(step.get("json"), ctx) if "json" in step else None

                resp = await client.request(method, url, headers=headers,
                                            params=params, json=body)
                if resp.status_code >= 400:
                    tpl = step.get("fail_message",
                                   "{name} step returned {status}: {body}")
                    return "FAIL: " + tpl.format(
                        name=intent["name"], status=resp.status_code,
                        body=resp.text[:200], url=url)

                try:
                    data: Any = resp.json()
                except ValueError:
                    data = resp.text

                if "extract" in step:
                    data = _resolve(step["extract"], data)
                if step.get("fail_if_empty") and not data:
                    return "FAIL: " + str(_render(step["fail_if_empty"], ctx))
                if "save_as" in step:
                    ctx[step["save_as"]] = data

            return str(_render(intent.get("reply", "OK: done"), ctx))
    except httpx.HTTPError as e:
        log.warning("intent %s error: %s", intent["name"], e)
        return f"FAIL: {intent['name']} request error: {e}"
