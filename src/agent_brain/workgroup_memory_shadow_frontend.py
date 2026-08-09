#!/usr/bin/env python3
"""Small read-only browser panel for a workgroup-memory shadow runtime.

This is intentionally a separate, opt-in diagnostic server.  It does not
replace the existing workgroup dashboard and it never writes runtime state.
"""

from __future__ import annotations

import argparse
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any

try:
    from .workgroup_memory_shadow import diagnostics_projection, get_entry
except ImportError:  # pragma: no cover
    from workgroup_memory_shadow import diagnostics_projection, get_entry  # type: ignore[no-redef]


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"))


def _metric(label: str, value: Any, hint: str = "") -> str:
    return (
        f'<article class="metric"><div class="label">{_escape(label)}</div>'
        f'<div class="value">{_escape(value)}</div><div class="hint">{_escape(hint)}</div></article>'
    )


def build_frontend_projection(shadow_root: Path) -> dict[str, Any]:
    return diagnostics_projection(shadow_root)


def render_html(projection: dict[str, Any]) -> str:
    chain = projection.get("event_chain", {})
    cards = projection.get("position_cards", {})
    slice_info = projection.get("context_slice", {})
    checkpoints = projection.get("checkpoints", {})
    graphiti = projection.get("graphiti", {})
    recovery = projection.get("recovery", {})
    statuses = projection.get("view_memory", {}).get("entry_statuses", {})
    authority = projection.get("authority", {})
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Brain · Shadow Memory</title>
<style>
:root {{ color-scheme: dark; --bg:#081521; --panel:#102638; --line:#284a63; --muted:#91acc0; --accent:#68d4bd; --warn:#efc46e; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:linear-gradient(135deg,#07131f,#0c2231); color:#edf6fb; font:14px/1.55 system-ui,Segoe UI,sans-serif; }}
main {{ max-width:1280px; margin:0 auto; padding:28px; }} header {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-start; margin-bottom:22px; }}
h1 {{ margin:0; font-size:26px; }} h2 {{ margin:0 0 12px; font-size:17px; }} .sub {{ color:var(--muted); margin-top:5px; }}
.badge {{ display:inline-flex; border:1px solid var(--line); border-radius:999px; padding:5px 10px; color:var(--accent); margin-left:6px; }}
.grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }} .metric,.panel {{ border:1px solid var(--line); border-radius:14px; background:rgba(16,38,56,.86); box-shadow:0 8px 22px rgba(0,0,0,.18); }}
.metric {{ padding:13px 15px; min-height:92px; }} .label,.hint {{ color:var(--muted); }} .value {{ font-size:23px; font-weight:700; margin:2px 0; }}
.panel {{ padding:18px; margin-top:14px; }} .three {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }} .memory {{ border-left:3px solid var(--accent); padding:10px 12px; background:rgba(7,22,32,.62); border-radius:8px; }}
.memory.archive {{ border-left-color:#7194bd; }} .memory.candidate {{ border-left-color:var(--warn); }} .row {{ display:flex; justify-content:space-between; gap:12px; }} code {{ color:#b9e7ff; }} table {{ width:100%; border-collapse:collapse; }} th,td {{ text-align:left; vertical-align:top; padding:9px; border-bottom:1px solid rgba(40,74,99,.65); }} th {{ color:var(--muted); font-weight:600; }}
.status {{ color:var(--accent); }} .warn {{ color:var(--warn); }} .muted {{ color:var(--muted); }} pre {{ overflow:auto; background:#06111a; border:1px solid var(--line); padding:12px; border-radius:8px; }}
@media(max-width:850px) {{ main {{ padding:15px; }} .grid,.three {{ grid-template-columns:1fr 1fr; }} header {{ flex-direction:column; }} }} @media(max-width:560px) {{ .grid,.three {{ grid-template-columns:1fr; }} }}
</style></head><body><main>
<header><div><h1>Agent Brain · Shadow Memory</h1><div class="sub">Read-only projection · group <code>{_escape(projection.get('group_id'))}</code></div></div>
<div><span class="badge">PRIVATE_SHADOW</span><span class="badge">no promotion</span></div></header>
<section class="grid">
{_metric('Event-chain', chain.get('count',0), 'head ' + str(chain.get('head','GENESIS'))[:16])}
{_metric('Position cards', cards.get('count',0), f"active {cards.get('active',0)} · historical {cards.get('historical',0)}")}
{_metric('Injection slice', f"{slice_info.get('bytes',0)} B", f"≈ {slice_info.get('approx_tokens',0)} tokens · coverage {slice_info.get('coverage_score','—')}")}
{_metric('Omitted / get-entry', slice_info.get('omitted',0), 'exact lookup ' + ('available' if slice_info.get('get_entry_available') else 'unavailable'))}
</section>
<section class="panel"><h2>Three distinct memory layers</h2><div class="three">
<div class="memory archive"><b>Complete workgroup memory</b><div class="muted">Raw append-only archive · rebuild source</div><div>statuses: {_escape(statuses)}</div></div>
<div class="memory"><b>Current injection slice</b><div class="muted">Only this bounded projection is eligible for controller injection</div><div>{_escape(slice_info.get('selected_budget_bytes'))} B budget · {_escape(slice_info.get('included'))} included</div></div>
<div class="memory candidate"><b>Long-term candidates</b><div class="muted">Candidate only · explicit host review required</div><div>{_escape(checkpoints.get('candidate_count',0))} candidate(s) · Graphiti pending {_escape(graphiti.get('pending_review',0))}</div></div>
</div></section>
<section class="panel"><h2>Recovery and authority</h2><div class="row"><span>Project regeneration</span><span class="status">supported</span></div><div class="row"><span>Codex platform compression hook</span><span class="warn">not controlled / consumption unknown</span></div><div class="row"><span>Graphiti approved receipts</span><span>{_escape(graphiti.get('episode_receipts',0))}</span></div><div class="row"><span>Project-control writes</span><span>{_escape(authority.get('project_control_written',False))}</span></div></section>
<section class="panel"><h2>Injection state</h2><pre>{_escape(json.dumps(recovery.get('last_receipt') or {{'status':'no slice generated'}}, ensure_ascii=False, indent=2))}</pre></section>
<section class="panel"><h2>Diagnostic projection</h2><table><tr><th>Field</th><th>Value</th></tr><tr><td>Schema</td><td><code>{_escape(projection.get('schema_version'))}</code></td></tr><tr><td>Event-chain head</td><td><code>{_escape(chain.get('head'))}</code></td></tr><tr><td>Slice kind</td><td><code>{_escape(slice_info.get('kind'))}</code></td></tr><tr><td>Recovery test</td><td class="status">{_escape('ready' if recovery.get('project_level_regeneration_supported') else 'not available')}</td></tr></table></section>
<div class="muted" style="margin-top:14px">This page is read-only. Refreshes the projection manually; it does not send context to Codex or Graphiti.</div>
</main><script>setTimeout(()=>location.reload(),3000);</script></body></html>"""


class ShadowHandler(BaseHTTPRequestHandler):
    shadow_root: Path

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/shadow":
                body = json.dumps(build_frontend_projection(self.shadow_root), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
                return
            if parsed.path == "/api/entry":
                entry_id = parse_qs(parsed.query).get("entry_id", [""])[0]
                body = json.dumps(get_entry(self.shadow_root, entry_id), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
                return
            if parsed.path in {"/", "/index.html"}:
                body = render_html(build_frontend_projection(self.shadow_root)).encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:  # fail closed without exposing source files
            self._send(500, json.dumps({"status": "REJECTED", "error": str(exc)}).encode("utf-8"), "application/json; charset=utf-8")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shadow-root", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args(argv)
    root = Path(args.shadow_root).resolve()

    class Handler(ShadowHandler):
        shadow_root = root

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(json.dumps({"status": "READY", "url": f"http://{args.host}:{args.port}/", "read_only": True}, ensure_ascii=False))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
