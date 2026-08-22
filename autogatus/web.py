"""Read-only web view for container details, styled to match Gatus.

Loads Gatus's own stylesheet (/css/app.css on the same host) and mirrors the
Tailwind/shadcn classes from Gatus v5.x EndpointDetails, so the look tracks
Gatus's theme across versions without forking it. Served under /details on the
same domain, so it sits behind the same Authentik middleware.
"""

from __future__ import annotations

import html

from flask import Flask, abort

app = Flask(__name__)
_store = None


def init(store):
    global _store
    _store = store
    return app


# ── formatting helpers ────────────────────────────────────────────────────────

def _bytes(n):
    if n is None:
        return "n/a"
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def _uptime(sec):
    if sec is None:
        return "n/a"
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, _ = divmod(sec, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _e(s):
    return html.escape(str(s)) if s is not None else ""


def _status(verdict, health):
    if health is None or health.state != "running":
        return "unhealthy"
    return "healthy" if verdict.success else "unhealthy"


def _badge(status):
    m = {
        "healthy": ("bg-green-500", "bg-green-300", "Healthy"),
        "unhealthy": ("bg-red-500", "bg-red-300", "Unhealthy"),
        "degraded": ("bg-yellow-500", "bg-yellow-300", "Degraded"),
    }
    pill, dot, label = m.get(status, ("bg-gray-500", "bg-gray-300", "Unknown"))
    return (
        f'<div class="inline-flex items-center gap-1 rounded-full border border-transparent '
        f'px-2.5 py-0.5 text-xs font-semibold text-white {pill}">'
        f'<span class="w-2 h-2 rounded-full {dot}"></span>{label}</div>'
    )


def _squares(history):
    cells = []
    for ok, _ in history[-40:]:
        color = "bg-green-500" if ok else "bg-red-500"
        cells.append(f'<div class="flex-1 h-6 sm:h-8 rounded-sm {color}"></div>')
    # pad with empty (no-data) squares on the left to a fixed width
    pad = 40 - len(cells)
    empties = ['<div class="flex-1 h-6 sm:h-8 rounded-sm bg-gray-700"></div>'] * max(0, pad)
    return '<div class="flex gap-0.5">' + "".join(empties + cells) + "</div>"


def _page(title, body):
    return f"""<!doctype html><html lang="en" class="dark"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{_e(title)} | Homelab Status</title>
<link rel="stylesheet" href="/css/custom.css"/>
<link rel="stylesheet" href="/css/app.css"/>
</head><body class="bg-background text-foreground">
<div class="dashboard-container bg-background min-h-screen">
<div class="container mx-auto px-4 py-8 max-w-7xl">{body}</div></div>
</body></html>"""


def _card(title, value):
    return (
        '<div class="rounded-lg border bg-card text-card-foreground shadow-sm">'
        '<div class="flex flex-col space-y-1.5 p-6 pb-2">'
        f'<h3 class="text-sm font-medium leading-none tracking-tight text-muted-foreground">{_e(title)}</h3></div>'
        f'<div class="p-6 pt-0"><div class="text-2xl font-bold">{value}</div></div></div>'
    )


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/details")
def index():
    items = _store.all() if _store else {}
    by_stack = {}
    for key, it in items.items():
        by_stack.setdefault(it.get("stack", "?"), []).append((key, it))

    sections = []
    for stack in sorted(by_stack):
        cards = []
        for key, it in sorted(by_stack[stack], key=lambda x: x[1].get("name", "")):
            h, v = it.get("health"), it.get("verdict")
            status = _status(v, h)
            mem = _bytes(h.mem_used) if h else "n/a"
            cpu = f"{h.cpu_percent}%" if h and h.cpu_percent is not None else "n/a"
            cards.append(
                f'<a href="/details/{_e(key)}" class="block rounded-lg border bg-card text-card-foreground '
                'shadow-sm p-4 transition hover:shadow-lg hover:scale-[1.01] dark:hover:border-gray-700">'
                '<div class="flex items-start justify-between gap-2">'
                f'<div class="min-w-0"><div class="font-semibold truncate">{_e(it.get("name"))}</div>'
                f'<div class="text-xs text-muted-foreground">cpu {cpu} · mem {mem}</div></div>'
                f'{_badge(status)}</div></a>'
            )
        sections.append(
            f'<div class="mb-8"><h2 class="text-lg font-semibold mb-3 text-muted-foreground">{_e(stack)}</h2>'
            f'<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">{"".join(cards)}</div></div>'
        )

    header = (
        '<div class="mb-6"><a href="/" class="inline-flex items-center text-sm text-muted-foreground '
        'hover:text-foreground mb-4">&larr; Back to Dashboard</a>'
        '<h1 class="text-4xl font-bold tracking-tight">Container Details</h1>'
        f'<p class="text-muted-foreground mt-2">{len(items)} containers monitored</p></div>'
    )
    return _page("Container Details", header + "".join(sections))


@app.route("/details/<key>")
def detail(key):
    it = _store.get(key) if _store else None
    if not it:
        abort(404)
    h, v = it.get("health"), it.get("verdict")
    status = _status(v, h)

    cur = "Operational" if status == "healthy" else "Issues Detected"
    mem = f"{_bytes(h.mem_used)}" if h else "n/a"
    if h and h.mem_percent is not None:
        mem += f' <span class="text-sm text-muted-foreground">({h.mem_percent}%)</span>'
    cpu = f"{h.cpu_percent}%" if h and h.cpu_percent is not None else "n/a"
    restarts = str(h.restart_count) if h else "n/a"

    cards = (
        '<div class="grid gap-6 md:grid-cols-2 lg:grid-cols-4">'
        + _card("Current Status", _e(cur))
        + _card("Memory", mem)
        + _card("CPU", _e(cpu))
        + _card("Restarts", _e(restarts))
        + "</div>"
    )

    rows = []
    if h:
        rows = [
            ("State", _e(h.state)),
            ("Health", _e(h.health_status or "n/a")),
            ("Uptime", _e(_uptime(h.uptime_seconds))),
            ("Memory", f"{_e(_bytes(h.mem_used))} / {_e(_bytes(h.mem_limit))}"),
            ("Network", f"RX {_e(_bytes(h.net_rx))} · TX {_e(_bytes(h.net_tx))}"),
            ("Block I/O", f"read {_e(_bytes(h.blk_read))} · write {_e(_bytes(h.blk_write))}"),
            ("OOM killed", "yes" if h.oom_killed else "no"),
        ]
        if h.health_output:
            rows.append(("Last healthcheck", _e(h.health_output[:300])))
    detail_rows = "".join(
        f'<div class="flex justify-between py-2 border-t"><span class="text-muted-foreground">{k}</span>'
        f'<span class="font-medium text-right">{val}</span></div>'
        for k, val in rows
    )
    status_line = _e(v.error) if v else ""

    body = f"""
<div class="mb-6">
  <a href="/details" class="inline-flex items-center text-sm text-muted-foreground hover:text-foreground mb-4">&larr; Back to Container Details</a>
  <div class="space-y-6">
    <div class="flex items-start justify-between">
      <div><h1 class="text-4xl font-bold tracking-tight">{_e(it.get('name'))}</h1>
        <div class="text-muted-foreground mt-2">Group: {_e(it.get('stack'))}</div></div>
      {_badge(status)}
    </div>
    {cards}
    <div class="rounded-lg border bg-card text-card-foreground shadow-sm">
      <div class="flex flex-col space-y-1.5 p-6"><h3 class="text-2xl font-semibold leading-none tracking-tight">Recent Checks</h3></div>
      <div class="p-6 pt-0">{_squares(it.get('history', []))}</div>
    </div>
    <div class="rounded-lg border bg-card text-card-foreground shadow-sm">
      <div class="flex flex-col space-y-1.5 p-6"><h3 class="text-2xl font-semibold leading-none tracking-tight">Details</h3></div>
      <div class="p-6 pt-0"><div class="text-sm">{detail_rows}</div>
      <p class="text-xs text-muted-foreground mt-4 break-words">{status_line}</p></div>
    </div>
  </div>
</div>"""
    return _page(it.get("name", key), body)


@app.route("/details/health")
def health():
    return {"ok": True, "containers": len(_store.all()) if _store else 0}
