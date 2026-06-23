# signal-claude-bridge

Send a word or phrase to yourself on Signal — get a researched markdown note written to a local folder within seconds.

Built on top of [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api). No cloud relay, just a Docker container and a Python daemon running on your PC.

---

## What it does

You send a Signal message to yourself (Note to Self). The bridge picks it up and routes it one of three ways:

| Message type | Example | What happens |
|---|---|---|
| Bare URL | `https://example.com/article` | Claude fetches the page, classifies the domain from content, and writes a structured note |
| Short topic (≤ 4 words, ≤ 60 chars, no sentence punctuation) | `stoicism` | Claude researches the topic online and writes a short markdown note to your output folder |
| Longer instruction | `summarise the key points of the EU AI Act and save it to my notes` | Claude follows the instruction directly |
| Intent match (optional) | depends on your `intents.json` — e.g. `add Dune Part Two to Radarr` | Bridge runs a config-driven HTTP pipeline and replies — Claude is not invoked. See [Intent dispatcher](#intent-dispatcher) |

You get a one-line Signal reply confirming what was written, plus the full note as a downloadable attachment. On failure you get an error message instead (no attachment).

Notes are tagged `ai-generated` and `signal-bridge` in frontmatter so you can review and promote them later.

---

## Architecture

```
Phone (Signal)
  └─► signal-cli-rest-api  (Docker, 127.0.0.1:8090)
            │
            │  GET /v1/receive  every 3 s
            ▼
      polling daemon  (pythonw.exe, background)
            │
            ├── bare URL     ──► prompts/url.md
            ├── short topic  ──► prompts/research.md
            └── freeform     ──► prompts/freeform.md
                                        │
                              (+ skills.json keyword match → SKILL.md injection)
                              (+ .bridge-history.jsonl → recent context)
                                        │
                                        ▼
                              claude -p <message>
                              cwd = VAULT_ROOT
                                        │
                                        ├── loads VAULT_ROOT/CLAUDE.md (if present)
                                        └── loads Signal inbox/CLAUDE.md  ← domain templates
                                        │
                                        ▼
                              classifies domain from Signal inbox/CLAUDE.md § Domains
                              follows that domain's template
                                        │
                                        ▼
                              writes Signal inbox/YYYY-MM-DD <slug>.md
                                        │
                                        ▼
                         POST /v2/send  (ack + note attachment)
```

Everything runs locally. No data leaves your machine except through Signal's own E2EE channel and Claude's API.

### Terminology

Four moving parts, deliberately distinct — don't conflate them:

- **Prompts** (`prompts/*.md`) — the per-mode system prompts (research / freeform / url) that drive every Claude invocation.
- **Domain templates** — the `###` sections inside `Signal inbox/CLAUDE.md` that define the output schema per topic type (term, product, travel…). Edited in your workspace, not the repo.
- **Skills** (`skills.json`) — workspace `SKILL.md` files injected into the prompt on keyword match, when the action benefits from Claude's reasoning.
- **Intents** (`intents.json`) — regex→HTTP pipelines that bypass Claude entirely for deterministic, well-shaped actions.

(The repo's `template/` folder is an unrelated starter-workspace scaffold — a one-time copy to bootstrap your workspace, not one of the above.)

---

## Requirements

- Windows 10/11 (the task scheduler integration is Windows-specific; the Python daemon itself is cross-platform)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- [Claude Code](https://claude.ai/code) installed
- A Signal account on your phone

---

## Setup

### 1. Clone and start the container

```powershell
git clone https://github.com/yourname/signal-claude-bridge
cd signal-claude-bridge
docker compose up -d signal-cli-rest-api
```

Wait ~15 s, then verify:

```powershell
docker inspect signal-cli-rest-api --format "{{.State.Health.Status}}"
# expected: healthy
```

### 2. Pair your Signal account

The bridge links to your existing Signal account as a secondary device — no new number needed.

```powershell
# Download and open the QR code
curl.exe "http://127.0.0.1:8090/v1/qrcodelink?device_name=signal-claude-bridge" -o qr.png
start qr.png
```

On your phone: **Signal → Settings → Linked devices → Link new device** → scan the QR.

Verify the pairing:
```powershell
curl.exe "http://127.0.0.1:8090/v1/accounts"
# expected: ["+1234567890"]
```

Full pairing guide (smoke tests, recovery steps) in [PAIRING.md](PAIRING.md).

### 3. Configure

```powershell
copy .env.example .env
```

Open `.env` and fill in at minimum:

| Variable | Description |
|---|---|
| `VAULT_ROOT` | Absolute path to your workspace (the working directory Claude runs in) |
| `CLAUDE_BIN` | Path or name of `claude.exe` — defaults to `claude`, see note below |
| `SIGNAL_NUMBER` | Your Signal number in E.164 format, e.g. `+1234567890` |
| `ALLOWED_SENDERS` | Same as above (messages from anyone else are dropped) |

**`CLAUDE_BIN` resolution:** On Windows, Claude Code installs to a versioned directory and is not on PATH by default. The bridge resolves `CLAUDE_BIN` in this order, re-checking on every message so Claude Code auto-updates don't break a running bridge:

1. If the value is an absolute path that exists, use it.
2. `shutil.which(value)` — covers PATH-resolvable names.
3. Probe `%APPDATA%\Claude\claude-code\<version>\claude.exe` and `%LOCALAPPDATA%\Programs\claude-code\<version>\claude.exe`, picking the highest version.

Leave `CLAUDE_BIN=claude` (the default) to get auto-discovery. Override with an absolute path only if you need to pin a specific install.

### 4. Authenticate Claude for the bridge (one-time)

The bridge runs `claude -p` non-interactively, so it depends entirely on stored credentials. The pitfall: if it shares your global `~/.claude` login, that login gets overwritten or expired out from under it by your *interactive* `claude` sessions and by managed/SDK hosts that refresh auth on their own schedule — which surfaces as a silent `401` on **every** message. So give the bridge its own credential that nothing else touches.

**Recommended — a long-lived token in `.env`** (best for an unattended daemon; no shared credential file to clobber):

```powershell
# On Windows `claude` is usually not on PATH — resolve the exe first:
$claude = (Get-ChildItem "$env:APPDATA\Claude\claude-code\*\claude.exe" |
           Sort-Object FullName -Descending | Select-Object -First 1).FullName

& $claude setup-token   # complete the browser step; it prints a long-lived token
```

`setup-token` does **not** write a credential file — it prints a token. Copy it into `.env` (gitignored):

```
CLAUDE_CODE_OAUTH_TOKEN=<the token from setup-token>
```

The bridge passes its environment through to `claude -p`, so the token is picked up on the next message. Verify before relying on it:

```powershell
$env:CLAUDE_CODE_OAUTH_TOKEN = "<the token>"
& $claude -p "say OK"    # should print OK, not "Not logged in" / 401
```

**Alternative — interactive login into an isolated config dir.** The bridge defaults `CLAUDE_CONFIG_DIR` to a repo-local `.claude-config/` (gitignored) so an interactive login lands somewhere private rather than in shared `~/.claude`. Authenticate it once:

```powershell
$env:CLAUDE_CONFIG_DIR = "$PWD\.claude-config"
& $claude               # then run /login inside the session
& $claude -p "say OK"    # verify
```

Either way you do this only once; the credential survives reboots and Claude Code updates. Auth precedence: `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` (env) take effect regardless of `CLAUDE_CONFIG_DIR`; otherwise the stored login in `CLAUDE_CONFIG_DIR` is used. The bridge logs a startup warning if none of these is present.

### 5. Run manually (first test)

```powershell
.\run.ps1
```

Creates `.venv`, installs dependencies, starts the daemon. You should see a line like:

```
bridge up; api=http://127.0.0.1:8090 workspace=C:\... inbox=Signal inbox poll=3.0s claude=...\claude.exe tools=Read,Write,Edit,Glob,Grep,WebSearch
```

Send yourself a Note to Self — try a single word like `stoicism`. Within ~30 s a new markdown file should appear in your output folder and you'll get a Signal reply.

### 6. Install as a background service

Once the end-to-end test passes, register the daemon as a Windows scheduled task — no terminal window, auto-starts at logon, restarts on failure:

```powershell
.\install-service.ps1
```

Logs are written to `logs\bridge.log`.

---

## Configuration reference

All settings live in `.env` (copy from `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `VAULT_ROOT` | *(required)* | Workspace root — working directory passed to `claude -p` |
| `CLAUDE_BIN` | `claude` | Path to Claude Code CLI executable |
| `CLAUDE_MODEL` | *(CLI default)* | Model for `claude -p`, e.g. `claude-sonnet-4-6` |
| `CLAUDE_TIMEOUT` | `300` | Max seconds to wait for Claude subprocess |
| `CLAUDE_CONFIG_DIR` | `.claude-config` (in repo) | Isolated Claude Code config/credential dir for the bridge, so its login isn't clobbered by interactive `claude` sessions. Authenticated once (see setup step 4). Override to point at a shared login if you prefer |
| `CLAUDE_CODE_OAUTH_TOKEN` | *(unset)* | Long-lived token from `claude setup-token` — the recommended auth for an unattended daemon (see setup step 4). Takes precedence over a stored login. Keep it in `.env` only (never commit) |
| `CLAUDE_TOOLS` | `Read,Write,Edit,Glob,Grep,WebSearch` | Comma-separated built-in tools available to Claude (Bash and WebFetch excluded by default for security; WebFetch is auto-added for bare-URL messages only) |
| `SIGNAL_API_PORT` | `8090` | Host port for docker-compose only — sets the container's published port. Not read by the Python daemon; update `SIGNAL_API_URL` to match if you change this |
| `SIGNAL_API_URL` | `http://127.0.0.1:8090` | signal-cli-rest-api base URL used by the daemon |
| `SIGNAL_NUMBER` | *(required)* | Your E.164 Signal number |
| `ALLOWED_SENDERS` | `SIGNAL_NUMBER` | Comma-separated allowlist of sender numbers |
| `POLL_INTERVAL` | `3` | Seconds between `/v1/receive` polls |
| `SHORT_TOPIC_MAX_TOKENS` | `4` | Max **words** for research vs freeform mode (the name says "tokens" but it counts whitespace-separated words). Research mode also requires ≤60 chars and no sentence punctuation/newline |
| `SIGNAL_INBOX` | `Signal inbox` | Subfolder (relative to `VAULT_ROOT`) where research notes are written — must contain a `CLAUDE.md` with domain templates |
| `ATTACH_MD` | `true` | Attach generated .md files to the Signal reply as downloadable files |
| `HISTORY_DEPTH` | `5` | Number of recent messages to include as context for follow-up queries |

Intents (see [Intent dispatcher](#intent-dispatcher)) can reference additional `${VAR}` env vars defined in `.env`; those are user-specific and not listed here.

---

## Workspace setup

The bridge runs `claude -p` with `cwd` set to `VAULT_ROOT`. Claude Code automatically reads any `CLAUDE.md` files it finds there — this is how the output format and domain templates reach the model.

This repo ships a ready-to-use starter-workspace scaffold under `template/` (distinct from the domain templates it contains). Copy it into your workspace:

```powershell
# from the repo root — adjust destination to your VAULT_ROOT
xcopy /E /I template\* "C:\path\to\your\folder\"
```

What you get:

| File | Purpose |
|------|---------|
| `Signal inbox/CLAUDE.md` | Domain templates (term, product, travel), output schema, tagging rules |

The output folder name (`Signal inbox` by default) is set by `SIGNAL_INBOX` in `.env`. If you rename the folder, update that variable to match — and rename the folder in your workspace too.

**Editing domain templates:** the `CLAUDE.md` inside your inbox folder lives in your workspace, not the repo. Changes take effect immediately — no daemon restart. To add a new domain, just add a `###` subsection; `prompts/research.md` is generic and needs no changes.

If you use [Obsidian](https://obsidian.md), the included domain templates produce Obsidian-flavoured markdown — YAML frontmatter, `[[wikilinks]]`, and `ai-generated` tags. If you use a plain markdown folder or a different tool, edit the inbox `CLAUDE.md` and the prompt files to match your preferred format. The bridge itself is format-agnostic.

---

## Customising Claude's behaviour

Edit the prompt files — no code changes needed:

- `prompts/research.md` — classifies the message domain, then delegates formatting to `Signal inbox/CLAUDE.md`
- `prompts/freeform.md` — governs longer instructions (output location, tagging, safety guardrails)
- `prompts/url.md` — fetches a bare URL, classifies domain from page content, writes a structured note

All prompts instruct Claude to return a single `OK: ...` or `FAIL: ...` line on stdout, which is forwarded back to you as the Signal reply.

---

## Skills

Skills are workspace-level `SKILL.md` files that give Claude domain-specific context and capabilities. When a message contains configured keywords, the bridge loads the matching skill's instructions into the system prompt alongside the normal research/freeform/url prompt.

### Setup

1. Discover available skills in your workspace:

```powershell
Get-ChildItem "$env:VAULT_ROOT\.claude\skills" -Directory | Select-Object Name
```

2. Copy the example config and edit it:

```powershell
copy skills.example.json skills.json
```

3. Edit `skills.json` — add entries for each skill you want to activate:

```json
{
  "skills": [
    {
      "name": "home-assistant-mcp",
      "keywords": ["lights", "thermostat", "ha", "automation", "temperature"],
      "skill_path": ".claude/skills/home-assistant-mcp/SKILL.md",
      "extra_tools": []
    }
  ]
}
```

| Field | Description |
|---|---|
| `name` | Display name (for logs) |
| `keywords` | Case-insensitive words that trigger this skill — if any keyword appears in the message, the skill is injected |
| `skill_path` | Path to the skill's `SKILL.md`, relative to `VAULT_ROOT` |
| `extra_tools` | Additional built-in tools to enable for this skill (appended to `CLAUDE_TOOLS`). Leave empty to use defaults |
| `mcp_config` | *(optional)* Path (relative to `VAULT_ROOT`) to an MCP config JSON loaded **only** for invocations this skill matches. Bridge sessions run with `--strict-mcp-config` (MCP off by default); set this if the skill genuinely needs a server. Omit to keep MCP disabled |

If `skills.json` is missing, the bridge starts normally with skill injection disabled. Multiple skills can match a single message (they're additive).

> **Security note:** Skills that enable write operations (e.g. Home Assistant control) expand the blast radius of any message. This is mitigated by the sender allowlist — only your own Signal number can trigger the bridge.

---

## Intent dispatcher

For clearly-shaped actions where invoking Claude would be overkill (e.g. "add this movie to Radarr", "trigger this webhook"), the bridge supports a config-driven intent dispatcher. Messages matching an intent's regex run a small HTTP pipeline and reply directly — Claude is not invoked.

The dispatcher is generic: the bridge ships zero service-specific logic. All bindings live in `intents.json` (gitignored, user-specific), so a fresh clone has no opinions about which services you integrate with.

### Setup

1. Copy the example:

```powershell
copy intents.example.json intents.json
```

2. Edit `intents.json`. The shipped example wires up a Radarr "add movie" flow as a worked illustration — remove it if you don't use Radarr, or replicate the shape for other services.

3. Add any `${ENV_VAR}` placeholders your intents reference (e.g. `RADARR_API_KEY`) to `.env`.

If `intents.json` is missing or contains no intents, dispatch is disabled and all messages flow to Claude as before.

### Schema

Each intent has a `match` regex, an ordered list of `steps` (HTTP calls or control steps), and a `reply` format string (a string-interpolation template, unrelated to the domain templates). Step responses are saved under `save_as`, optionally narrowed with `extract`, and later steps can reference them via `{name.path}`. `${ENV_VAR}` placeholders expand from `.env` at request time. See `intents.example.json` for full field documentation and a working example.

### When to use intents vs skills

- **Intent** when the action is well-shaped (one regex captures it), the work is a deterministic HTTP sequence, and you want fast/reliable execution without LLM-in-loop ambiguity.
- **Skill** when the action benefits from Claude's reasoning — classification, summarisation, writing notes, anything requiring judgement.

---

## Follow-up messages

The bridge maintains a lightweight message history so follow-up queries like "tell me more about that" or "expand on the last note" work across invocations.

### How it works

After each successful invocation, the bridge appends a one-line summary to `{SIGNAL_INBOX}/.bridge-history.jsonl` (capped at the most recent 100 entries; older lines are trimmed automatically). On the next message, the last N entries (controlled by `HISTORY_DEPTH`, default 5) are included in the system prompt as context.

When a message contains referential words (e.g. "that", "previous", "last", "expand", or Finnish equivalents like "edellinen", "lisää"), the bridge also reads the first 500 characters of the most recent output note and includes them as additional context.

### Persistent memory

Claude can also maintain a `.bridge-memory.md` file in the inbox folder for durable preferences (e.g. "always write recipes in Finnish"). This file is loaded into every invocation's system prompt if it exists. Claude creates and appends to it when it detects a standing preference in your message.

> **Note:** because every message can append to this file and it is injected into every future prompt, it is a persistent instruction surface. The bridge injects it labelled as untrusted recalled *data* (not commands) and caps its size, but if you ever share the bridge or relax `ALLOWED_SENDERS`, review `.bridge-memory.md` periodically and delete anything you didn't intend.

Both files are auto-managed — no manual maintenance required. To reset history, delete `.bridge-history.jsonl`. To reset preferences, delete `.bridge-memory.md`.

---

## Liveness heartbeat (optional)

The bridge can POST to a webhook after every successful Signal poll, so an external monitor can alert you if it goes silent. Configure in `.env`:

- `HEARTBEAT_WEBHOOK_URL` — URL to ping (empty = disabled). The ping fires **only after a poll that actually reached the Signal API**, so a downed `signal-cli-rest-api` container stops the heartbeat too — not just a crashed daemon.
- `HEARTBEAT_INTERVAL` — minimum seconds between pings (default `60`).

The watchdog itself lives outside the bridge. The reference setup is a Home Assistant dead-man's-switch: a webhook automation restarts a 6-minute `timer` on each ping, and a `timer.finished` automation sends a mobile-app push if the pings stop (covering daemon crash, Docker-down, and machine/network outages alike).

---

## Security

- **Signal E2EE** — messages are end-to-end encrypted. Only contacts you've accepted can reach your linked account.
- **Container is loopback-bound** (`127.0.0.1:8090`) — unreachable from the LAN or internet.
- **Sender allowlist** — `ALLOWED_SENDERS` defaults to your own number. Messages from anyone else are logged and dropped before Claude is invoked.
- **No inbound ports** are opened on your router.
- **Image is pinned** to a specific release tag in `docker-compose.yml`. Upgrades are deliberate.
- **Tool restriction** — `CLAUDE_TOOLS` controls which built-in tools Claude has access to. Bash is excluded by default to prevent prompt injection from fetched web pages or skill content reaching a shell. `WebFetch` is also excluded by default and only enabled for bare-URL messages (the one mode that needs to fetch a page).
- **MCP servers are blocked.** Spawned sessions run with `--strict-mcp-config` and no `--mcp-config`, so **no** MCP server loads — not even ones configured in your user/project settings. This matters because `--tools` scopes only *built-in* tools; without `--strict-mcp-config`, `mcp__*` tools (e.g. a Home Assistant server) would still load and run under `bypassPermissions`. A skill that genuinely needs an MCP server opts back in per-invocation via its `skills.json` `mcp_config` field.

> **Why the sender allowlist is load-bearing.** Sessions run with `--permission-mode bypassPermissions` (a non-interactive `-p` run can't answer a permission prompt). Under that mode **`permissions.deny` rules are not consulted** — so a deny-rule settings file is *not* a usable guardrail here. The real boundaries are the ones above (`--tools`, `--strict-mcp-config`) plus the sender allowlist, which is what stops anyone but you from reaching the model at all. Keep `ALLOWED_SENDERS` tight.

- **Log hygiene** — the daemon keeps request logging quiet, but anything written *before* this was upgraded may still contain your number and (if heartbeat is enabled) the webhook URL in `logs\bridge*.log`. Delete the `logs\` folder to purge historical copies; it's gitignored and regenerated on next start.

---

## Upgrading the container

Check [releases](https://github.com/bbernhard/signal-cli-rest-api/releases), then:

```powershell
# Edit image: tag in docker-compose.yml, then:
docker compose pull && docker compose up -d
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| *Every* `/v1/receive` returns `400 Bad Request` | Container in `json-rpc` mode | Ensure `MODE: normal` in `docker-compose.yml`, recreate container |
| `FileNotFoundError` on startup | `CLAUDE_BIN` not resolvable | Leave `CLAUDE_BIN=claude` for auto-discovery, or set an absolute path in `.env` |
| Bridge silently stops replying after a Claude Code update | (Pre-fix bug) startup-cached `CLAUDE_BIN` pointed at the old versioned dir | Fixed: bridge now re-resolves per message. Restart the service if running an older build |
| Task shows `Ready` not `Running` | Normal — the VBScript launcher exits immediately after spawning pythonw | Verify the bridge is alive by checking `logs\bridge.log` for recent poll lines. If the log is stale, check `logs\bridge-stderr.log` for startup tracebacks |
| Messages received but no note written | Claude failed silently | Signal reply will say `FAIL: ...`; check logs for details |
| Every message replies `FAIL: claude exited 1` (often with `401`/`Invalid authentication credentials`) | The bridge's Claude login is missing or expired — usually because credentials were shared with another `claude` session/host that overwrote them | Authenticate the bridge's isolated config dir (setup step 4): `$env:CLAUDE_CONFIG_DIR="$PWD\.claude-config"; claude` → `/login` (or `claude setup-token`), then restart the bridge. Startup also logs a warning if this dir has no credentials |
| Bridge silently ignores all messages (no `dispatch:` lines, occasional `400`s) | Outdated signal-cli drops sealed-sender messages — `getServerGuid(...) must not be null` NPE ([signal-cli #2059](https://github.com/AsamK/signal-cli/issues/2059), affects 0.14.1–0.14.4.1). A manual `GET /v1/receive/<number>` shows envelopes with `source:null` and an `exception` field | Upgrade the container: bump `image:` to ≥ `0.100` (bundles signal-cli ≥ 0.14.5), then `docker compose pull && docker compose up -d`. Pairing persists via the named volume |
| System drive filling up | Container log not rotated | Ensure `logging:` block is present in `docker-compose.yml`; recreate the container with `docker compose down && docker compose up -d` to apply it |

---

## License

MIT
