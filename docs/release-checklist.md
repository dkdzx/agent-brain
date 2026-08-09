# Public release checklist

This checklist keeps a release of `agent-brain` auditable and safe to publish.
It is a release procedure, not a claim that a local runtime has been migrated
or that a provider consumed a generated context slice.

## Public release set

The English release should contain:

- `src/agent_brain/workgroup_memory_shadow.py`;
- `src/agent_brain/workgroup_memory_shadow_frontend.py`;
- `tests/test_workgroup_memory_shadow.py` and the frontend tests;
- `examples/workgroup-memory-shadow/`;
- `docs/workgroup-memory-shadow.md`;
- `assets/architecture-en.mmd` and its rendered `architecture-en.png`;
- the updated English `README.md` and `SECURITY.md`.

The diagram source is the maintainable artifact. The PNG is a convenience for
repository readers and must be regenerated or explicitly verified against the
source before a release.

## Required gates

Before committing or pushing:

1. Run `py_compile` for every changed Python module.
2. Run the complete `unittest` suite and the anonymous shadow example.
3. Run an independent Reader against the synthetic source fixture.
4. Run a changed-file scan for private paths, real project identifiers, real
   task/thread IDs, credentials, and raw chat.
5. Inspect `git diff --check` and the complete `git diff`.
6. Confirm that Graphiti receipts report `live_writes_performed: 0` unless an
   explicit, reviewed import command was used.
7. Confirm that no candidate is promoted to project truth automatically.

## Semantics that must remain visible

The release must distinguish:

- the complete append-only workgroup archive;
- the bounded injection slice for one member/task/scope;
- group-memory candidates awaiting host review;
- `GENERATED_ONLY` versus `SENT_BY_CONTROLLER`;
- `PLATFORM_CONSUMPTION_UNKNOWN`.

Project-level regeneration is supported by the adapter. Interception of a
provider's internal automatic context-compaction hook is not claimed.

## Publish policy

Keep the final publication as one reviewed changeset. Do not push a partially
tested frontend, a real runtime snapshot, or a private shadow directory. If a
repository owner requests an English-only release, keep local translations out
of the commit rather than silently mixing public-language variants into the
release manifest.
