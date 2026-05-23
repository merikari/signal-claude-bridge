"""Direct REST handlers for *arr services (currently Radarr).

Used by the Signal bridge to handle clear "add X to Radarr" intents
without round-tripping through `claude -p`. Avoids the need for an
MCP server and the headless-mode --mcp-config plumbing.
"""

from __future__ import annotations

import logging
import os
import re

import httpx

log = logging.getLogger("signal-bridge")

RADARR_URL = os.environ.get("RADARR_URL", "http://localhost:7878").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")

# Intent patterns:
#   "add <title> to radarr"
#   "radarr add <title>"
#   "radarr: <title>"     "radarr - <title>"
#   "lisää <title> radariin"  (Finnish)
_RADARR_ADD_RE = re.compile(
    r"""^\s*(?:
          add\s+(?P<a>.+?)\s+to\s+radarr
        | radarr\s+add\s+(?P<b>.+)
        | radarr\s*[:\-]\s*(?P<c>.+)
        | lis(?:ää|aa)\s+(?P<d>.+?)\s+radariin
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def parse_radarr_add(text: str) -> str | None:
    """Return the movie title if `text` is a Radarr add intent, else None."""
    m = _RADARR_ADD_RE.match(text)
    if not m:
        return None
    title = m.group("a") or m.group("b") or m.group("c") or m.group("d")
    return title.strip() if title else None


async def add_movie(title: str) -> str:
    """Search Radarr, add top match with the default profile + root folder.

    Returns a one-line summary suitable for the Signal reply, prefixed
    `OK:` on success or `FAIL:` on any error.
    """
    if not RADARR_API_KEY:
        return "FAIL: RADARR_API_KEY not set in .env"

    headers = {"X-Api-Key": RADARR_API_KEY}
    base = f"{RADARR_URL}/api/v3"

    try:
        async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
            r = await client.get(f"{base}/movie/lookup", params={"term": title})
            r.raise_for_status()
            results = r.json()
            if not results:
                return f"FAIL: no Radarr match for {title!r}"

            movie = results[0]
            # If Radarr already knows about this movie, `id` will be non-zero.
            if movie.get("id"):
                return (
                    f"OK: {movie.get('title')} ({movie.get('year')}) "
                    f"already in Radarr (id {movie['id']})."
                )

            qp_resp = await client.get(f"{base}/qualityprofile")
            qp_resp.raise_for_status()
            quality_profiles = qp_resp.json()
            rf_resp = await client.get(f"{base}/rootfolder")
            rf_resp.raise_for_status()
            root_folders = rf_resp.json()
            if not quality_profiles or not root_folders:
                return "FAIL: Radarr has no quality profiles or root folders configured"

            qp = quality_profiles[0]
            rf = root_folders[0]
            payload = {
                "title": movie["title"],
                "qualityProfileId": qp["id"],
                "rootFolderPath": rf["path"],
                "tmdbId": movie["tmdbId"],
                "year": movie.get("year"),
                "monitored": True,
                "minimumAvailability": "announced",
                "addOptions": {"searchForMovie": True},
            }
            add_resp = await client.post(f"{base}/movie", json=payload)
            if add_resp.status_code >= 400:
                return f"FAIL: Radarr add returned {add_resp.status_code}: {add_resp.text[:200]}"

            return (
                f"OK: Added {movie['title']} ({movie.get('year', '?')}) to Radarr "
                f"— profile: {qp['name']}, root: {rf['path']}. Search triggered."
            )
    except httpx.HTTPError as e:
        log.warning("radarr add error: %s", e)
        return f"FAIL: Radarr request error: {e}"
