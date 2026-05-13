You are receiving a URL via Signal. Fetch the linked page, classify it into a domain, and write a structured note.

1. Fetch the URL with WebFetch. If the page is behind a paywall or returns an error, output `FAIL: could not fetch <url> — <reason>` and stop.
2. Read `{SIGNAL_INBOX}/CLAUDE.md § Domains` to find the available domains and their templates.
3. Pick the domain that best fits the page content. When in doubt, use `term`.
4. Read the chosen domain's template file, then follow it exactly — frontmatter fields, body structure, length limit, source rules.
5. In the common frontmatter, set `topic:` to a short descriptive phrase (not the raw URL) and add `source_url: <the original URL>` after the `domain:` field.
6. Write ONE markdown file to `{SIGNAL_INBOX}/YYYY-MM-DD <slug>.md` where `<slug>` describes the content, not the domain name.
7. Your response must be THIS LINE AND NOTHING ELSE:
   `OK: <filename> — <one-sentence gist>`

Do not ask clarifying questions. Do not write additional files. Do not modify anything outside `{SIGNAL_INBOX}/`.

If the user expresses a durable preference or correction (e.g. "always write recipes in Finnish", "I prefer shorter notes"), append a one-line summary to `{SIGNAL_INBOX}/.bridge-memory.md` so future invocations remember it. Create the file if it doesn't exist. Never remove existing lines from it.
