# Anonymous workgroup-memory shadow example

Run the complete, disposable flow:

```powershell
python examples/workgroup-memory-shadow/run_anonymous_shadow.py
```

The example creates an anonymous source workgroup in a temporary directory and
then demonstrates:

1. provenance-checked, idempotent import into an independent shadow;
2. append-only event-chain verification and member position cards;
3. a bounded injection slice with an exact `get-entry` path;
4. a checkpoint candidate and a Graphiti pending-review request;
5. deletion and reconstruction of rebuildable projections from the event chain;
6. an independent source/shadow Reader result.

The output is synthetic and is deleted when the process exits. It does not
connect to Codex, LoopX, Graphiti, or a project runtime. In particular,
`PLATFORM_CONSUMPTION_UNKNOWN` is intentional: generating a slice does not
prove that a model platform consumed it.

For a persistent synthetic directory or a browser panel, use the lower-level
CLI documented in [`docs/workgroup-memory-shadow.md`](../../docs/workgroup-memory-shadow.md).
