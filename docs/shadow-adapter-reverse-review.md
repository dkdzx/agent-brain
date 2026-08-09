# Shadow adapter reverse review

This is the release-facing review for the read-only workgroup-memory shadow
adapter. All examples in this document are synthetic. It does not describe a
particular private project or claim that a model platform hook is controlled.

## 1. Scope and authority

The adapter operates below the host project's authority layer. It can create an
independent shadow ledger, rebuild projections, produce a bounded injection
receipt, and create review requests. It cannot write the source workgroup,
project-control truth, world/canonical state, or a project's formal completion
state. Group-memory candidates remain `candidate` until a host controller
explicitly reviews them.

## 2. Source identity and provenance

The importer requires `group.json`, `members.json`, `task_pool.json`,
`working_snapshot.json`, and a `lanes/` directory. Required source files are
hashed before import. Every bootstrap event carries a source path/hash,
group/task/member/thread attribution when available, `PRIVATE_SHADOW`
authority, a deterministic import key, and a predecessor/event hash. Missing
source material fails closed. Repeating an import with unchanged source hashes
is idempotent.

## 3. Event chain, projections, and Reader

`events.jsonl` is append-only and is the reconstruction source. `view.json` and
`position_cards.json` are rebuildable projections. The independent Reader
recomputes the source projection and compares member, task, status, and
evidence counts. A sequence break, hash mismatch, or deletion below the
import-receipt lower bound is rejected. Historical member evidence may remain
diagnosable, but inactive members are not reintroduced into the active roster.

## 4. Context budget and recovery

The complete archive is not the live injection. Before a continuation or
recovery, the wrapper reads the current view, checks the expected version and
event head, selects a bounded member/task/scope slice, and writes an injection
receipt containing included and omitted entry IDs, a slice hash, byte/token
estimates, and the target thread. A known 353K-token window uses a normal 384
KiB ceiling and a 512 KiB hard ceiling; a remaining-output reserve can shrink
the budget. A 1 MiB budget is offline-only. `get-entry` retrieves omitted
entries exactly. Destroying view/context projections is recoverable from the
event chain.

The receipt distinguishes `GENERATED_ONLY` from `SENT_BY_CONTROLLER` and always
exposes `PLATFORM_CONSUMPTION_UNKNOWN`. The adapter does not intercept or
assert control over a provider's internal automatic compaction hook.

## 5. Checkpoint memory and Graphiti gate

Checkpoint extraction creates append-only group-memory candidates with source
entries, evidence references, validity fields, scope, claim ceiling, hash, and
`supersedes`. It never silently overwrites a prior candidate. Each candidate
can produce a Graphiti pending-review request. Approval is a separate action;
without a live importer callback the resulting episode receipt reports zero
live writes. No Graphiti operation writes project-control truth.

## 6. Diagnostics and public artifacts

The opt-in diagnostic frontend reports event-chain head/count, position cards,
slice size/coverage/omissions, checkpoints, Graphiti review counts, recovery
state, and authority boundaries. It is separate from any host dashboard and is
read-only. The public example creates a temporary anonymous source projection,
runs the flow, simulates projection loss, and exits without preserving private
runtime files.

## 7. Tests, security, and release boundary

The release gate includes Python compilation, the complete unittest suite, the
anonymous end-to-end example, the independent Reader, `git diff --check`, and
`scripts/audit_public_release.py`. The public tree must not contain private
paths, project names, real task/thread IDs, credentials, raw chat, or hidden
reasoning. The final English release is one reviewed changeset; local language
drafts, private shadow runtimes, and generated caches are excluded. No commit
or push is implied by this review.
