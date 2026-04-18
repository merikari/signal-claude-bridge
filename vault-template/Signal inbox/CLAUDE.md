# CLAUDE.md — Signal inbox

Folder for notes produced by the Signal → Claude Code bridge. Anything here was written automatically in response to a Signal message sent to the paired bot account.

## Purpose

- **Short topic (≤ 4 words)** → Claude classifies the domain, researches, and writes a note here.
- **Longer instruction** → Claude follows the instruction; if no folder is specified, output lands here by default.

These notes are raw, point-in-time AI output. Review and either promote to a proper folder or delete. Nothing here is authoritative.

## General rules (all domains)

- Language follows the Signal message's language. Finnish in → Finnish note.
- Never delete or rename existing files here or elsewhere.
- Never create files outside this folder unless the Signal message explicitly names a target folder.
- If research fails (no usable sources), return `FAIL: <reason>` and do not write a file.

## Common frontmatter

Every file must start with:

```yaml
---
tags: [ai-generated, signal-bridge]
source: signal
topic: <original message verbatim>
domain: <term|product|travel>
created: YYYY-MM-DD HH:MM
---
```

Add any domain-specific fields listed below after `domain:`.

---

## Domains

### term *(default)*

Use for concepts, words, people, historical events, definitions, and general knowledge.

**Extra frontmatter:** none

**Body:**

```
# <Topic>

One-paragraph summary (2–4 sentences).

## Key facts
- 3–6 concise bullets.

## Sources
- Markdown links to sources actually used.

## Go deeper
- 2–3 bullets suggesting follow-up angles or related topics.
```

**Length:** ≤ 300 words. No filler, no disclaimers.

**Source quality:** encyclopaedic (Wikipedia, Britannica), academic abstracts, reputable journalism. No SEO farms.

---

### product

Use when the user wants to buy, compare, or evaluate a consumer product, device, app, or service.

**Extra frontmatter:**

```yaml
price_checked: YYYY-MM-DD
currency: EUR          # adjust to your locale
```

**Body:**

```
# <Product / Category>

One-paragraph context: what problem it solves, who it's for, current market situation.

## Best pick
- **Model name** — price, where to buy (link), one-sentence reason.

## Alternatives

| Model | Price | Retailer | Standout |
|-------|-------|----------|---------|
| …     | …     | …        | …       |

## Key specs to compare
- Bullet list of the 3–5 specs that matter most for this category.

## Watch out for
- 1–3 known issues, gotchas, or things to verify before buying.

## Sources
- Markdown links to sources actually used.
```

**Length:** ≤ 400 words.

**Source quality:** check at least two retailers relevant to your region and one specialist review site (rtings.com, Wirecutter, or similar). Prices must be from today's search — do not guess or recall from training data. If live pricing is unavailable, say so explicitly.

---

### travel

Use for destinations, cities, routes, trip ideas, and travel planning.

**Extra frontmatter:**

```yaml
destination: <city or region>
origin: <departure city if mentioned, else leave blank>
travel_dates: <if specified, else leave blank>
```

**Body:**

```
# <Destination>

One-paragraph orientation: geography, character, best season, typical visitor.

## Getting there
- Transit options with approximate cost and travel time (train, bus, flight).

## Where to stay
- Budget / mid / splurge tier with one concrete example each (name, rough price/night).

## Sample day
- 4–6 bullet itinerary items that capture the place's character.

## Practical notes
- Visa, currency, language, tipping norm, safety — only the non-obvious points.

## Sources
- Markdown links to sources actually used.
```

**Length:** ≤ 450 words.

**Source quality:** official tourism board, Lonely Planet / Rough Guides, Wikitravel for practical notes; live transport prices from relevant booking sites. If the user mentioned specific dates, search for those; otherwise use current typical fares.

---

## Customising

This file lives in your vault (pointed to by `VAULT_ROOT`), not in the bridge repo. Edit it freely — changes take effect immediately with no daemon restart. To add a new domain, add a new `###` subsection and update `prompts/research.md` in the repo to include the new domain name in the classification table.
