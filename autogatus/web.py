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

    cards = []
    groups = set()
    for key, it in items.items():
        h, v = it.get("health"), it.get("verdict")
        status = _status(v, h)
        stack = it.get("stack", "?")
        groups.add(stack)
        mem_bytes = int(h.mem_used) if (h and h.mem_used is not None) else -1
        cpu_val = float(h.cpu_percent) if (h and h.cpu_percent is not None) else -1
        mem_txt = _bytes(h.mem_used) if h else "n/a"
        cpu_txt = f"{h.cpu_percent}%" if h and h.cpu_percent is not None else "n/a"
        cards.append(
            f'<a href="/details/{_e(key)}" class="ag-card block rounded-lg border bg-card text-card-foreground '
            'shadow-sm p-4 transition hover:shadow-lg hover:scale-[1.01] dark:hover:border-gray-700" '
            f'data-name="{_e((it.get("name") or "").lower())}" data-group="{_e(stack)}" '
            f'data-status="{status}" data-mem="{mem_bytes}" data-cpu="{cpu_val}">'
            '<div class="flex items-start justify-between gap-2">'
            f'<div class="min-w-0"><div class="font-semibold truncate">{_e(it.get("name"))}</div>'
            f'<div class="text-xs text-muted-foreground truncate">{_e(stack)}</div>'
            f'<div class="text-xs text-muted-foreground mt-1">cpu {cpu_txt} · mem {mem_txt}</div></div>'
            f'{_badge(status)}</div></a>'
        )

    sel = "text-sm bg-background border rounded-md px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-ring"
    group_opts = '<option value="all">All groups</option>' + "".join(
        f'<option value="{_e(g)}">{_e(g)}</option>' for g in sorted(groups)
    )
    controls = f"""
    <div class="rounded-lg border bg-card text-card-foreground shadow-sm p-4 mb-6">
      <div class="flex flex-wrap items-center gap-3">
        <input id="ag-q" type="text" placeholder="Search name..." class="{sel} flex-1 min-w-[10rem]"/>
        <select id="ag-group" class="{sel}">{group_opts}</select>
        <select id="ag-status" class="{sel}">
          <option value="all">All statuses</option>
          <option value="healthy">Healthy</option>
          <option value="unhealthy">Unhealthy</option>
        </select>
        <select id="ag-sort" class="{sel}">
          <option value="name">Sort: Name</option>
          <option value="group">Sort: Group</option>
          <option value="mem">Sort: Memory</option>
          <option value="cpu">Sort: CPU</option>
          <option value="status">Sort: Status</option>
        </select>
        <button id="ag-order" class="{sel} font-medium" title="Toggle sort order">↑ Asc</button>
      </div>
    </div>"""

    header = (
        '<div class="mb-6"><a href="/" class="inline-flex items-center text-sm text-muted-foreground '
        'hover:text-foreground mb-4">&larr; Back to Dashboard</a>'
        '<h1 class="text-4xl font-bold tracking-tight">Container Details</h1>'
        f'<p class="text-muted-foreground mt-2"><span id="ag-count">{len(items)}</span> of '
        f'{len(items)} containers</p></div>'
    )
    grid = f'<div id="ag-grid" class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">{"".join(cards)}</div>'
    empty = '<p id="ag-empty" class="text-muted-foreground text-center py-12" style="display:none">No containers match.</p>'
    return _page("Container Details", header + controls + grid + empty + _INDEX_JS)


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


# Client-side filter/sort for the index. All cards are already in the DOM, so
# this is instant and needs no round-trips. Choices persist in localStorage.
_INDEX_JS = """
<script>
(function () {
  var grid = document.getElementById('ag-grid');
  if (!grid) return;
  var cards = Array.prototype.slice.call(grid.querySelectorAll('.ag-card'));
  var q = document.getElementById('ag-q');
  var fGroup = document.getElementById('ag-group');
  var fStatus = document.getElementById('ag-status');
  var sortBy = document.getElementById('ag-sort');
  var orderBtn = document.getElementById('ag-order');
  var count = document.getElementById('ag-count');
  var empty = document.getElementById('ag-empty');
  var order = 'asc';

  function load() {
    try {
      var s = JSON.parse(localStorage.getItem('autogatus.filters') || '{}');
      if (s.q) q.value = s.q;
      if (s.group) fGroup.value = s.group;
      if (s.status) fStatus.value = s.status;
      if (s.sort) sortBy.value = s.sort;
      if (s.order) order = s.order;
    } catch (e) {}
    orderBtn.textContent = order === 'asc' ? '\\u2191 Asc' : '\\u2193 Desc';
  }
  function save() {
    try {
      localStorage.setItem('autogatus.filters', JSON.stringify({
        q: q.value, group: fGroup.value, status: fStatus.value,
        sort: sortBy.value, order: order
      }));
    } catch (e) {}
  }

  function apply() {
    var term = q.value.trim().toLowerCase();
    var g = fGroup.value, st = fStatus.value, key = sortBy.value;
    var visible = cards.filter(function (c) {
      if (term && c.dataset.name.indexOf(term) === -1) return false;
      if (g !== 'all' && c.dataset.group !== g) return false;
      if (st !== 'all' && c.dataset.status !== st) return false;
      return true;
    });
    visible.sort(function (a, b) {
      var av, bv;
      if (key === 'mem' || key === 'cpu') {
        av = parseFloat(a.dataset[key]); bv = parseFloat(b.dataset[key]);
      } else {
        av = a.dataset[key] || ''; bv = b.dataset[key] || '';
      }
      var cmp = av < bv ? -1 : (av > bv ? 1 : 0);
      return order === 'asc' ? cmp : -cmp;
    });
    cards.forEach(function (c) { c.style.display = 'none'; });
    visible.forEach(function (c) { c.style.display = ''; grid.appendChild(c); });
    if (count) count.textContent = visible.length;
    if (empty) empty.style.display = visible.length ? 'none' : '';
    save();
  }

  q.addEventListener('input', apply);
  fGroup.addEventListener('change', apply);
  fStatus.addEventListener('change', apply);
  sortBy.addEventListener('change', apply);
  orderBtn.addEventListener('click', function () {
    order = order === 'asc' ? 'desc' : 'asc';
    orderBtn.textContent = order === 'asc' ? '\\u2191 Asc' : '\\u2193 Desc';
    apply();
  });

  load();
  apply();
})();
</script>
"""
