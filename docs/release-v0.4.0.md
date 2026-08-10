# agent-brain v0.4.0

This release updates the public read-only dashboards without coupling them to a
specific project controller or private runtime.

## Included

- Selectable DAG presentations for the construction graph, including layered
  orthogonal layouts, Dagre/tree projections, radial and force-directed views,
  compact overview layouts, and a local manual layout mode.
- A native WebGL 3D read-only view with camera orbit, pan, cursor-centered
  wheel zoom, node picking, status coloring, and a fit-to-view action.
- Chinese/English UI switching for the construction dashboard, including the
  graph toolbar, change stream, tables, status labels, and presentation mode.
- A single fullscreen presentation action and invisible internal scrollbars so
  graph navigation uses wheel zoom and pointer panning instead of competing
  nested scrollbar tracks.
- Workgroup status reconciliation that separates registered members,
  historical membership flags, current read-only Codex thread status, and
  project-wide status counts. Missing or stale status projections fail closed
  as `UNKNOWN_STALE`; historical `active=true` is never used as a live-status
  fallback.
- Anonymous demo fixtures and regression tests for the workgroup status
  projection.

## Boundaries

The public release contains no project-specific paths, private thread IDs,
controller decisions, credentials, raw chat transcripts, or private runtime
artifacts. The frontend is read-only: layout drafts remain local to the
browser, and status views do not claim or mutate workgroup/project state.

## Verification

The release branch is checked with Python compilation, the construction
frontend tests, the complete repository test suite, `git diff --check`, and a
public-safe path/identity scan before publication.
