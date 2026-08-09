# Workgroup Memory Shadow Adapter

This document describes the read-only adapter for a workgroup runtime that is
not already stored in the native `agent-brain` event schema. It is designed for
long-running coding-agent teams where source projections may exist as separate
`group.json`, `members.json`, task-pool, snapshot, and lane receipt files.

The adapter adds a private shadow ledger. It does not replace the source
runtime, the host project's authority files, or the model provider.

## The three memory layers

Keep these names distinct in the UI and in controller prompts:

1. **Complete workgroup memory** is the append-only shadow event archive plus
   its rebuildable `view.json`. It is the source for reconstruction and exact
   lookup. It is not automatically sent to a model.
2. **Current injection slice** is the bounded projection selected for one
   member, task, scope, and continuation. Its receipt records what was
   included and omitted. It is not the complete archive.
3. **Long-term memory candidates** are reviewed candidates extracted at a
   checkpoint. A candidate is not a project fact and is not a long-term-memory
   fact until the host control plane explicitly accepts it.

Graphiti is a retrieval sidecar. The adapter creates a pending request and a
review queue; it does not silently import candidates.

## Lifecycle

```text
source projections
    -> source manifest (path + SHA-256)
    -> idempotent PRIVATE_SHADOW bootstrap events
    -> rebuild view and member position cards
    -> generate a bounded slice before each continuation
    -> checkpoint candidate (append-only, with supersedes)
    -> controller review
       ├─ reject / leave pending
       ├─ promote through the host's explicit long-term-memory command
       └─ approve a separate Graphiti import request
```

The importer copies source bytes into a shadow-only snapshot for audit. It
never writes the source group. Re-running the import with the same source
hashes produces no new bootstrap events.

## Provenance and event chain

Every shadow event contains:

- `source_path`, `source_relpath`, `source_sha256`, and `source_size`;
- `group_id`, source task/thread attribution when present;
- `authority: PRIVATE_SHADOW`;
- a deterministic import key for idempotency;
- `prev_event_hash` and `event_hash`.

The event payload contains a structured entry or a safe artifact summary, not
raw chat, credentials, or hidden reasoning. Lane receipts are represented by
path/hash/status references. The independent reader recomputes the source
projection and checks task, member, status, and evidence counts against the
shadow view.

## Position cards

The adapter reuses the core `materialize_view`,
`build_member_position_cards`, and `compact_context_entry` contracts. A card
contains the verified Codex task title, member/thread binding, core claim,
strongest evidence, strongest counterevidence, scope, claim ceiling, model-gate
status, signing authority, and source entry IDs.

An inactive or queued source member is not placed in the active member list.
Its card remains available as historical diagnostic evidence. A title is never
invented from delegation markup or a thread identifier; a missing title fails
closed to `Task title pending sync` in a consuming UI.

## Compression and recovery

The adapter cannot intercept or prove control over a Codex platform's internal
automatic context compaction. Project-level recovery is defined more narrowly:

1. Before dispatch, follow-up, or recovery, read the current verified view.
2. Reject a caller-supplied version or event head if it is stale.
3. Run the core bounded-context selector for the member and scope.
4. Write an `INJECTION_RECEIPTS.jsonl` row with the slice hash, source event
   head, view version, included IDs, omitted IDs, byte/token estimate, budget
   mode, and target thread.
5. Treat the state as `GENERATED_ONLY` unless a controller explicitly asserts
   `SENT_BY_CONTROLLER`. In both cases, expose
   `PLATFORM_CONSUMPTION_UNKNOWN`.

Omitted entries remain in the raw event archive and can be retrieved with
`get_entry(shadow_root, entry_id)`. Destroying `view.json` or the in-memory
context does not destroy the event source; `recover_shadow_view` rebuilds the
view, cards, and recovery receipt.

### Budget policy

For a known 353K-token model window, the normal injection ceiling is 384 KiB
and the hard ceiling is 512 KiB. A remaining-output reserve reduces the
available injection budget. For an unknown model window, the compatibility
mode uses the hard ceiling. A 1 MiB budget is available only for offline
export, not normal live injection. The core selector evaluates a configurable
ladder and stops at the target-coverage/marginal-gain elbow.

## Checkpoints and close/freeze

`checkpoint_memory` selects confirmed facts, local decisions, conflicts,
questions, scope warnings, and evidence references. It creates a candidate
with:

```text
memory_id, group_id, task_id, source_thread, source_member,
content, status=candidate, confidence, valid_at, invalid_at,
evidence_refs, entry_ids, supersedes, scope, claim_ceiling, hash
```

The append-only candidate ledger is the history. The JSON projection is
rebuildable and never silently replaces an old candidate. A changed candidate
points to the prior one through `supersedes`.

Freeze/close remains a host workgroup lifecycle decision. A checkpoint may be
made before freeze, at a gate change, or before close, but it does not close the
group and does not promote anything.

## Graphiti review gate

The adapter writes:

- `GRAPHITI_PENDING_IMPORT_REQUESTS.jsonl`;
- `GRAPHITI_REVIEW_QUEUE.json`;
- `GRAPHITI_APPROVALS.jsonl` only after an explicit review call;
- `GRAPHITI_EPISODE_RECEIPTS.jsonl` after the separate import command.

Without a live importer callback, the final receipt is
`APPROVED_NOT_IMPORTED_NO_LIVE_ADAPTER` with `live_writes_performed: 0`.
This is deliberate. A candidate cannot reach Graphiti merely because a model
called a checkpoint command. Group/project namespaces are preserved in every
request, and project-control writes are always false.

## Read-only diagnostics frontend

`src/agent_brain/workgroup_memory_shadow_frontend.py` is an opt-in diagnostic
server. It is separate from the normal workgroup dashboard and does not change
the 8766 production entry. It exposes:

```text
GET /                  HTML panel, auto-refreshing every 3 seconds
GET /api/shadow       event head/count, cards, slice, checkpoints, Graphiti, recovery
GET /api/entry?...    exact entry lookup by ID
```

The panel labels complete archive, current slice, and candidate memory as
different layers. It also shows `GENERATED_ONLY`, `SENT_BY_CONTROLLER`, and
`PLATFORM_CONSUMPTION_UNKNOWN` as separate states.

## Anonymous command example

The public synthetic canary creates its source files in a temporary directory,
then runs the complete flow:

```powershell
python examples/workgroup-memory-shadow/run_anonymous_shadow.py
```

The lower-level CLI is useful when an adapter has already produced a source
directory:

```powershell
python -m agent_brain.workgroup_memory_shadow import `
  --source-group-root .synthetic-source `
  --shadow-root .synthetic-shadow

python -m agent_brain.workgroup_memory_shadow external-reader `
  --source-group-root .synthetic-source `
  --shadow-root .synthetic-shadow

python -m agent_brain.workgroup_memory_shadow context `
  --shadow-root .synthetic-shadow `
  --thread-id synthetic-thread-worker

python -m agent_brain.workgroup_memory_shadow checkpoint-memory `
  --shadow-root .synthetic-shadow `
  --reason "before synthetic freeze"

python -m agent_brain.workgroup_memory_shadow diagnostics `
  --shadow-root .synthetic-shadow
```

For a browser panel, use an unused local port:

```powershell
python -m agent_brain.workgroup_memory_shadow_frontend `
  --shadow-root .synthetic-shadow `
  --port 8770
```

## Failure modes and authority boundary

The shadow rejects missing source hashes, malformed JSON, duplicate members,
event sequence/hash breaks, stale context, scope mismatch, wrong thread
lookup, missing Graphiti approval, secret-like output, and candidate promotion
without a host review. Deleting a previously checkpointed event tail is also
rejected by the import receipt lower-bound.

The following remain intentionally outside the claim ceiling:

- intercepting Codex's internal context compaction hook;
- proving that a provider consumed a generated slice;
- automatically deciding that a candidate is a project fact;
- writing a host project's `project_control`, world, or canonical state;
- importing raw chat or hidden chain of thought into Graphiti.

## Verification

```powershell
python -m unittest discover -s tests -v
```

The shadow test suite covers source equality, idempotent import, title and
historical-member rules, stale-view rejection, exact lookup, recovery,
candidate supersedes, Graphiti approval, event tamper/deletion, and the
read-only frontend projection.
