"""Read-only visibility into jarvS: recent runs, tools awaiting your
approval, and the audited command log. It cannot start, stop, queue, or
approve anything -- that's the point of it being read-only.

Run with:  python -m uvicorn dashboard:app --host 127.0.0.1 --port 8899
Then open it from inside your LAN (don't expose this port to the internet;
it has no auth of its own).
"""
from __future__ import annotations

import html
import os
import pathlib

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

import runtime

ROOT = pathlib.Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
HOME = runtime.app_home()
os.environ["JARVIS_HOME"] = str(HOME)
AUDIT = HOME / "logs" / "commands.log"

import memory
import tools

app = FastAPI(title="jarvS Dashboard")
LOGO = ROOT / "assets" / "logo.svg"


def _tail_audit(n: int = 40) -> list[str]:
    if n <= 0 or not AUDIT.exists():
        return []
    with AUDIT.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - 64 * 1024))
        text = stream.read().decode("utf-8", errors="replace")
    if size > 64 * 1024 and "\n" in text:
        remainder = text.split("\n", 1)[1]
        if remainder:
            text = remainder
    return text.splitlines()[-n:]


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    runs = memory.recent(15)
    pending = tools.list_pending()
    audit = _tail_audit(30)

    rows = "".join(
        f"<tr><td>{html.escape(m or '')}</td><td>{html.escape(s or '')}</td>"
        f"<td class='{st}'>{html.escape(st or '')}</td>"
        f"<td>{html.escape((sm or '')[:160])}</td></tr>"
        for m, s, st, sm in runs
    )
    pending_html = "".join(
        f"<li>{html.escape(p)} <span class='muted'>-- awaiting approval "
        f"(worker.py --approve {html.escape(p)})</span></li>" for p in pending
    ) or "<li class='muted'>(none)</li>"
    audit_html = "\n".join(html.escape(l) for l in audit) or "(no commands logged yet)"

    return f"""<!doctype html>
<title>jarvS</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="icon" type="image/svg+xml" href="/logo.svg">
<style>
:root{{color-scheme:dark light;padding-top:env(safe-area-inset-top,0px);
      padding-bottom:env(safe-area-inset-bottom,0px)}}
body{{font-family:-apple-system,system-ui,sans-serif;max-width:900px;margin:0 auto;
      padding:1.5rem 1rem;background:#0b0b0f;color:#e8e8ec}}
h1{{margin:0 0 .25rem;display:flex;align-items:center;gap:.6rem}} h1 img{{width:36px;height:36px}} .sub{{color:#9a9aa5;margin:0 0 1.5rem;font-size:14px}}
h2{{font-size:15px;text-transform:uppercase;letter-spacing:.04em;color:#9a9aa5;
    margin:2rem 0 .75rem;border-bottom:1px solid #22222b;padding-bottom:.4rem}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
td,th{{border-bottom:1px solid #1c1c24;padding:6px 8px;text-align:left;
       overflow-wrap:anywhere}}
th{{color:#9a9aa5;font-weight:600}}
td.ok{{color:#6bcf8f}} td.error{{color:#e56b6b}}
pre{{background:#14141a;padding:1rem;overflow-x:auto;border-radius:8px;
     font-size:12px;line-height:1.5}}
ul{{margin:0;padding-left:1.2rem}} .muted{{color:#7a7a85}}
</style>
<h1><img src="/logo.svg" alt="">jarvS</h1>
<p class="sub">read-only -- refresh the page for updates</p>

<h2>Recent runs</h2>
<table><tr><th>mission</th><th>started</th><th>status</th><th>summary</th></tr>
{rows or '<tr><td colspan=4 class="muted">(no runs yet)</td></tr>'}
</table>

<h2>Tools awaiting your approval</h2>
<ul>{pending_html}</ul>

<h2>Last 30 audited commands</h2>
<pre>{audit_html}</pre>
"""


@app.get("/logo.svg")
def logo() -> Response:
    return Response(LOGO.read_bytes(), media_type="image/svg+xml")


@app.get("/health")
def health() -> dict:
    return {"ok": True}
