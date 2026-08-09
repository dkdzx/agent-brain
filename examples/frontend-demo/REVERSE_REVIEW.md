# Anonymous frontend demo — seven-section reverse review

## 1. Scope and authority

This demo is a read-only projection of one synthetic workgroup. It does not
claim, complete, restart, reconcile, or promote any task. The host control
plane and the existing runtime remain authoritative.

## 2. Input truth and provenance

The page reads `group.json`, `members.json`, `view.json`, and the optional task
pool sidecar. When a real runtime has an append-only `events.jsonl`, the
frontend derives the global stream count from its sequence values. The demo
uses an explicit anonymous stream-count fixture so a screenshot can show the
difference between a large global sequence and a small filtered view.

## 3. Privacy boundary

Public demo data contains no real project name, host identity, thread identity,
lease token, local path, or API credential. Member cards display only the
synthetic Codex task title and a boolean identity-availability flag. Exact
entry responses remove host/thread/token fields before rendering.

## 4. Context semantics

The blue card is the **current injection slice**. It reports measured bytes,
rough tokens, selected budget, budget ladder, elbow, marginal gain, scan
candidate count, injected count, omission count, coverage, duplicate rate,
retrieval supplements, and latency. The amber card is **complete workgroup
memory** and is explicitly retrieval-only; it is never treated as one giant
model input. The anonymous backend curve uses six arbitrary-length points:
32KB, 64KB, 128KB, 256KB, 512KB, and 1MB. Its selected point is 512KB; the
512KB-to-1MB marginal gain is zero, so the displayed selection reason is
“target coverage and marginal-gain elbow reached”.

## 5. Member and history separation

Active members are rendered as current position cards. A revoked or exited
member is excluded from the active-member section and can only appear in the
historical diagnostic evidence section. Every position card keeps the claim,
strongest evidence, strongest counterevidence, scope, claim ceiling, evidence
status, model-gate status, signing status, and exact-entry reference. Related
source entries are rendered as a local position timeline ordered by global
event sequence; the timeline is a display projection and does not rewrite the
append-only event sequence.

## 6. Task pool and event semantics

The task pool is split into pending and claimed columns and reports the
one-person-one-task policy plus member/task conflicts. Event display defaults to
core events: decision, real effect, challenge, evidence, or claim/completion.
The page also reports global stream count, core count, real-effect count, and
evidence count. The “view all stream” toggle exposes accounting/projection and
ABSTAIN-style rows without renumbering global sequence values; core rows receive
a separate local `1/N` sequence.

The left workgroup index excludes an `ACTIVE` directory that has no valid
active members. Such a stale projection is counted as
`ACTIVE_NO_ACTIVE_MEMBERS` for diagnosis instead of being shown as a running
project with a misleading zero-member card.

The detail page has a module visibility list. Operators can independently hide
or show the task pool, structural issues, context slice, position cards,
members, events, history, and boundary panels. Each long panel owns its own
scroll container so the page does not grow without limit.

The structural-issues panel is read-only and defaults to active issues. It
supports severity/status filters and expandable impact, ruling, allowed route,
forbidden interpretation, evidence references, and resolution references. The
public fixture uses only anonymous issue text; private workgroup issue sidecars
are not part of this repository demo.

## 7. Failure handling and verification

Missing views, incomplete member identity fields, unavailable task pools, and
missing exact entries fail closed or use a read-only compatibility projection.
The browser verification receipt records the anonymous URL, projection checks,
event toggle, exact-entry lookup, and the fact that no GitHub push or mainline
runtime change was performed.
