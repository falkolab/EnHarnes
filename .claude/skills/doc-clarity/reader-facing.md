# Reader-facing clarity rules

Load this **in addition to** `SKILL.md` when the document's primary reader is a non-engineer or a mixed audience (execs, PMs, partners, business owners), or when the document exists to make or communicate a decision.

Goal: a reader with no insider knowledge can act on every sentence. Implementation detail belongs elsewhere.

### R1. Keep implementation detail out of decision/business prose
Keep models, function names, endpoints, and schemas OUT; describe behavior in plain words. Put the detail in an implementation doc (or a clearly-marked appendix) and link to it.
- ❌ "add `LineItem(sku, qty, cached_title/price/stock)`"
- ✅ "each line is one item and a quantity, with a few display fields for reference" (field list lives in the implementation doc)

### R2. Keep raw code identifiers out of explanatory prose
Table names, class/function names, endpoint syntax, and field tuples read as noise to most readers and often signal that detail landed in the wrong document.
- ❌ "writes `FOO_TABLE` / `BAR_TABLE`", "`fetchOwned()` via `GET /items?owner=<id>`"
- ✅ "stores the records", "a module that fetches an owner's items from the upstream service"

### R3. Prefer neutral phrasing; lead with the least-disruptive accurate option
Avoid loaded or alarming wording when a calmer, equally-true phrasing exists.
- ❌ "force a password reset for all users"
- ✅ "migrate credentials transparently where possible; require a reset only if there's no other way"

## Quick self-check (reader-facing)
1. Any table / function / endpoint / field name in prose? → move to an implementation doc; describe in words (R1, R2).
2. Any loaded wording where a neutral one fits? → soften (R3).
3. Read top to bottom **as the target reader** — does any sentence need insider knowledge (a tool name, internal metaphor, reference code, code identifier)? → rewrite.
