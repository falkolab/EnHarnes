---
name: doc-clarity
description: Write clear documents at the right register for their reader. Classify the document first, then apply the matching rules — strip jargon, transliterations, raw code identifiers, undefined codenames, and bare reference codes from reader-facing docs, while keeping the precise identifiers a technical spec needs. Use when authoring or editing ANY document (decision docs, specs, RFCs, design docs, wiki/Confluence pages, READMEs, notes, summaries).
---

# Document Clarity Rules

These rules are about **lexicon and style**, not content. A document can be factually correct and still fail — if a non-engineer can't parse it, or if a technical spec is too vague to implement from. The fix is the same idea in both cases: **match the register to the reader.** What that means depends on who the reader is, so classify the document before applying any rule.

All examples below use generic placeholders (`ServiceA`, `FOO_TABLE`, `D-7`, …) on purpose — apply the rule, not the example.

## First: classify the document

Decide the **primary reader** and **purpose**, then load the matching mode file from this skill's directory in addition to the universal rules below:

- **Reader-facing** — read by non-engineers or a mixed audience (execs, PMs, partners, business owners), or written to make/communicate a decision. Decision, strategy, and business docs; RFCs; most Confluence pages; user-facing READMEs; notes; summaries. The failure mode is **jargon the reader can't parse**. → Also read **`reader-facing.md`**.
- **Technical spec** — read by the engineers who will build or operate the thing. Implementation specs, API contracts, data-model and schema docs, runbooks, the technical internals of a design doc, code comments. The failure mode is **vagueness an engineer can't build from**; precise identifiers are required, not noise. → Also read **`technical-spec.md`**.
- **Mixed** — e.g. a design doc with a stakeholder summary up front and a technical section below. → Read **both** mode files; apply reader-facing rules to the narrative and technical-spec rules to the clearly-marked technical sections, and don't let either bleed into the other.

A README is reader-facing or technical depending on who actually reads it — classify by the real reader, not the file type. When in doubt about the reader, treat it as reader-facing.

## Universal rules (apply to every document, both modes)

### U1. No jargon loanwords or transliterated foreign tech-terms
Use the established plain word or phrase, not insider slang or a borrowed term. (Examples below are in English; for a document written in another language, the same rule bans transliterated spellings of English jargon.)
| Don't write | Write |
|---|---|
| cutover | switch to the new system |
| release train | release schedule |
| prereqs | what's needed before starting |
| creds | credentials / access |
| retry + dead-letter | retry on failure, with a queue for undelivered messages |
| pin the versions | lock the versions |

Carve-out for a **technical spec**: a borrowed term that is the genuine established term of art among the engineers reading it (e.g. "commit", "idempotent") is fine. The sloppy slang above is banned even there.

### U2. Define any coined term, codename, or internal metaphor on first use
Say what it means in ordinary words the first time it appears.
- ❌ "god-user", "a cross-cutting doc", "the X lens", "we buy exactly one split", "blast radius"
- ✅ "one universal account for everyone", "a document that affects several areas", "from the X angle", "we make exactly one part a separate service", "how much breaks when it fails"

In a technical spec you may keep the precise coined term (it carries meaning for engineers) — but still define it once before relying on it.

### U3. Spell out reference codes on first use
Don't make the reader look up `D-7`, `Q-12`, `T-3`. State the meaning the first time, then put the code in parentheses.
- ❌ "folds in only when D-7 lifts"
- ✅ "folds in later, once the staged rollout is enabled (decision D-7)"

### U4. Don't trade accuracy for a snappy label
A neat phrase that is technically wrong costs more than it saves — a careful reviewer will reject it.
- ❌ "read-path vs write-path" (when both sides read *and* write)
- ✅ "one component consumes the data; the other produces it"

### U5. One idea per sentence; cause before effect
Long clause-chains hide the point. Split them. State the fact, then why it matters.
- ❌ "Z, which gates launch but not the start, fast-follow with Y once W is confirmed…"
- ✅ "Z is needed before launch, not before starting work. (Y follows once W is confirmed.)"

## Quick self-check (run before finishing)

First confirm the mode, then:

1. Any jargon loanword or transliterated foreign term? → replace, unless it's a genuine term of art in a technical spec (U1).
2. Any coined term or metaphor undefined on first use? → define it (U2).
3. Any bare reference code without its meaning? → spell it out (U3).
4. Any catchy phrase that's actually inaccurate? → fix for accuracy (U4).
5. One idea per sentence, cause before effect? (U5)
6. Then run the checklist in the mode file you loaded (`reader-facing.md` and/or `technical-spec.md`).
