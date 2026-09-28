# Technical-spec precision rules

Load this **in addition to** `SKILL.md` when the document's reader is the engineer who will build or operate the thing: implementation specs, API contracts, data-model and schema docs, runbooks, the technical internals of a design doc, code comments.

Goal: an engineer can implement or operate from the document without guessing. **Precision is the target; vagueness is the failure.** The universal rules (U1–U5) still apply, with the carve-outs noted in `SKILL.md`. These three rules are the reverse of the reader-facing ones — apply them here, not those.

### T1. Name models, fields, endpoints, and functions exactly
The detail that a reader-facing doc would push to an appendix is the content here. State it inline.
- ❌ "each line is one item and a quantity, with a few display fields"
- ✅ "`LineItem(sku, qty, cached_title/price/stock)`"

### T2. Keep raw code identifiers IN the prose — they carry the meaning
A vague paraphrase is the failure mode, not the identifiers.
- ❌ "stores the records", "a module that fetches an owner's items from the upstream service"
- ✅ "writes `FOO_TABLE` / `BAR_TABLE`", "`fetchOwned()` calls `GET /items?owner=<id>`"

### T3. State the exact action plainly — precision over softening
Do not soften wording at the cost of accuracy. A runbook reader needs the literal procedure.
- ❌ "credentials are refreshed where possible" (when the step actually forces a reset)
- ✅ "force a password reset for all users"

## Quick self-check (technical spec)
1. Read top to bottom **as the implementing engineer** — is anything too vague to build from? Name the exact table, field, endpoint, or function (T1, T2).
2. Any softened or hedged step that hides the literal action? → state it plainly (T3).
3. Did you keep precise terms of art and coined terms (defined once), rather than stripping them? (U1, U2 carve-outs).
