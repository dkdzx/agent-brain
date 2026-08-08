# Anonymous frontend fixture

This fixture is synthetic and exists only to exercise the read-only workgroup
dashboard. It contains no real project name, thread ID, local path, lease
token, or credential.

Run the public frontend with:

```powershell
python src/agent_brain/workgroup_status_frontend.py serve --host 127.0.0.1 --port 8766
```

Then open:

```text
http://127.0.0.1:8766/?demo=1
```

The demo endpoint reads `runtime/` as an anonymous workgroup runtime. The
initial response contains bounded previews and references only. Clicking an
entry loads its exact content through `/api/entry`.
