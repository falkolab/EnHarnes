# Engineering Rules

An index, not a rulebook. Each rule below is stated in full somewhere else; this
file says which rule exists and where it is enforced, so a reader arriving from
the harness skill can find it. When a rule moves, change the pointer here rather
than restating the rule in two places.

## 1) Agent execution rules

- An agent ships only verifiable changes, and records the verification it ran.
  Stated in `AGENTS.md` → Verification-First Engineering.
- Autonomy is bounded by risk: Low executes, Medium requires an approved plan
  before implementation, High plans only. Stated in `AGENTS.md` → Autonomy, and
  the tiers themselves live in `policies/risk-policy.json`.
- An agent does not skip the documentation update when it changes a process.
  Enforced by the doc-drift rules in `policies/risk-policy.json`, checked at the
  pre-PR gate.

## 2) Change management rules

- Every significant change goes through a commit and a review request — a merge
  request or a pull request, depending on the forge; the harness treats them as
  the same thing and its tooling accepts either spelling.
- A medium or high risk task is carried by an ExecPlan before any code is
  written. Stated in `AGENTS.md` → Autonomy, with the required sections under
  ExecPlan Minimum Content and the full format in
  `.claude/skills/harness.plan/OPENAI_PLANS.md`.
- Size is judged separately from kind: an oversized plan must decompose, and an
  oversized branch blocks at the pre-PR gate. Thresholds live in
  `policies/size-policy.json`.
- TODO: [AI->HUMAN] No service level is set for how long a review may sit, and
  no criterion says when a stalled review escalates. Decide both, or state that
  neither is wanted.

## 3) Security and reliability rules

- Secrets never enter the repository. Environment files stay untracked, and a
  prompt-level hook (`.claude/hooks/prompt-validator.py`) refuses a prompt that
  carries one before it is sent.
- A change to a security or reliability surface updates the document that
  describes it — the doc-drift rules name which.
- Reviews carry a dedicated security pass; the bar and the output contract are
  in `REVIEW.md`.
- TODO: [HUMAN] Minimum compliance requirements are not written down. State them
  or record that none apply.

## 4) Documentation rules

- Unknown parts of the future product are recorded as TODOs with an owner tag,
  enforced by the TODO linter (`make lint-todos`).
- Documents are written for their reader, per `.claude/skills/doc-clarity/`.
- Index consistency across the reference documents is generated rather than
  maintained by hand — `make sync-indexes`, checked by `make check-docs`.

---

_Engineering philosophy (simplicity, anti-overengineering) lives in the optional
skill: `.claude/skills/harness.anti-overengineering/SKILL.md`. The mechanical
invariants — the ones each mapped to a linter or a structural test — are in
`GOLDEN_PRINCIPLES.md` beside this file._
