# Security and publication boundary

The repository is intentionally generic.

## Never store

- raw chat transcripts;
- hidden chain of thought;
- credentials, tokens, cookies, or authorization headers;
- private source files or datasets;
- real host paths, usernames, task IDs, thread IDs, or internal project names;
- unreviewed workgroup content as permanent memory.

## Trust model

- `host_id` and `thread_id` must come from a trusted launcher, not from an
  agent's prompt.
- Lease tokens are displayed once. Only their SHA-256 digests are persisted.
- The event ledger is append-only and hash chained.
- `view.json` is disposable and must be rebuildable from the event ledger.
- The frontend is read-only and binds to `127.0.0.1` by default.
- A stable handoff is a review candidate, not permission to write to the host
  project's authority files.
- Graphiti export files are requests, not proof that a live database was
  updated.

## Public-release audit

Run:

```powershell
python scripts/audit_public_release.py
```

The audit rejects common private-path, identity, transcript, credential, and
project-name patterns.

