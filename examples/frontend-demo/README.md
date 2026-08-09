# Anonymous construction-state demo

This fixture is synthetic and exists to exercise the same formal construction
state-machine interface used by the read-only deployment. It contains no real
project name, task title, thread ID, local path, lease token, or credential.

Run the demo with:

```powershell
python src/agent_brain/construction_status_frontend.py serve --host 127.0.0.1 --port 8767
```

Open:

```text
http://127.0.0.1:8767/?demo=1
```

In the integrated local deployment, the same anonymous state-machine view is
available at `http://127.0.0.1:8766/?demo=1`; the 8767 workgroup demo has a
top-right **返回项目状态机** link to that page.

The `?demo=1` route uses an in-memory anonymous construction bundle. It keeps
the formal project-scoped interface: a project column identifies the selected
project, and all three child views render from that same verified bundle. It
then provides:

- **流程图** — 16 synthetic stage nodes, 33 hard/soft dependency edges,
  critical-path highlighting, task attachments, and blocking edges. The
  scenario intentionally contains three parallel input branches, a blocked
  acceptance gate, a second parallel execution branch, two merge gates, and a
  stale private-projection side path so the value of the state machine is
  visible rather than looking like a linear checklist. The graph uses a
  layered DAG layout with dependency-depth columns, barycenter crossing
  reduction, semantic swimlanes, and an auto-selected blocker inspector;
- **列表** — stage status, estimated completion, task/member attachments,
  blockers, handoff, and claim ceiling;
- **变更流** — append-only synthetic state changes with global sequence
  numbers.

The demo also shows the ordinary state transitions, orthogonal state axes,
typed side states, queued/active/pending-review/stale task examples, and an
explicit read-only boundary. Its change stream has 12 synthetic events, for
example parallel branch start, evidence attachment, blocker propagation,
merge waiting, acceptance waiting, and private projection expiry. The
construction API is served at
`/api/construction?demo=1`; it never reads the production construction pointer
or the real workgroup runtime. The **工作组** button stays in demo mode and
opens `/workgroups?demo=1`, whose **返回施工状态机** link returns to the same
anonymous state-machine page.
