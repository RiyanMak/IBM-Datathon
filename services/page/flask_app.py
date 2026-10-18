"""Step 6, the Flask fallback for the investigator page.

Streamlit needs pyarrow, which has no s390x build, so on the VM the page runs
through this file instead. It is a thin view: every piece of logic is the same
module the Streamlit page uses (state.py reads the streams, graph.py draws a
ring as Graphviz text, decisions.py validates and writes a decision, fmt.py
formats numbers). The browser polls two JSON routes every two seconds and
renders the Graphviz text with viz.js.

    python -m services.page.flask_app --port 8501        # what docker compose runs

Routes:
    GET  /                 the page
    GET  /api/state        counters, the ring list, decisions: one snapshot
    GET  /api/ring/<id>    one ring in full: picture (DOT), reasons, facts, case, history
    GET  /api/ring/<id>/packet.md   the case packet (Markdown draft for filing) as a download
    POST /api/decide       {"ring_id", "version", "decision", "note"} -> the decisions stream
    GET  /healthz          "ok"
"""

import argparse
import time

from flask import Flask, Response, jsonify, request
from redis.backoff import NoBackoff
from redis.retry import Retry

from common import bus
from services.page import decisions, fmt, granite_requests, graph, packet_export, state
from services.page.models import PageState, decision_label, shape_label

REFRESH_MS = 2000
_client = None


def redis_client():
    """One client for the process; a down Redis shows a banner within a second."""
    global _client
    if _client is None:
        _client = bus.connect(socket_connect_timeout=1, socket_timeout=2, retry=Retry(NoBackoff(), 0))
    return _client


def ring_summary(snap: PageState, ring) -> dict:
    d = snap.decisions.get(ring.ring_id)
    case = snap.cases.get(ring.ring_id)
    return {
        "ring_id": ring.ring_id, "version": ring.version, "shape": ring.shape,
        "shape_label": ring.shape_label, "n_accounts": ring.n_accounts,
        "xgb_score": fmt.score(ring.xgb_score), "gnn_score": fmt.score(ring.gnn_score),
        "first_seen": fmt.data_time(ring.first_seen), "last_seen": fmt.data_time(ring.last_seen),
        "span": fmt.span_hours(ring.first_seen, ring.last_seen),
        "money": graph.money(ring.total_usd),
        "decision": {"decision": d.decision, "label": d.label, "note": d.note,
                     "decided_at": fmt.wall_time(d.decided_at)} if d else None,
        "has_case": case is not None,
    }


def snapshot_json(snap: PageState) -> dict:
    return {
        "redis_ok": snap.redis_ok,
        "redis": "%s:%d" % (bus.redis_host(), bus.redis_port()),
        "errors": snap.errors,
        "skipped": snap.skipped,
        "tiles": [{"label": t.label, "value": t.value, "help": t.help} for t in fmt.top_bar(snap.counters)],
        "rings": [ring_summary(snap, ring) for ring in snap.rings],
        "open_rings": snap.counters.open_rings,
        "updated": time.strftime("%H:%M:%S UTC", time.gmtime()),
    }


_granite_throttle = granite_requests.Throttle()   # one request per ring version per 45 s


def ring_json(r, snap: PageState, ring) -> dict:
    case = snap.cases.get(ring.ring_id)
    if case is None:
        try:
            case = state.latest_case(r, ring.ring_id)   # outside the stream window: the hash
        except Exception:
            case = None
    if granite_requests.wanted(case):
        try:
            _granite_throttle.request(r, ring.ring_id, ring.version, "flask")
        except Exception:
            pass
    pic = graph.build(ring, case.facts if case else None, "light")
    try:
        history = state.case_history(r, ring.ring_id)
    except Exception:
        history = []
    out = ring_summary(snap, ring)
    out.update({
        "dot": pic.dot,
        "picture_caption": pic.caption or "",
        "roles_known": bool(pic.roles),
        "reasons": list(ring.reasons),
        "facts": [[label, value] for label, value in fmt.fact_rows(case.facts if case else None)],
        "case": {
            "source": case.source, "source_label": case.source_label, "is_granite": case.is_granite,
            "check_note": case.check_note, "written_at": fmt.wall_time(case.written_at),
            "version": case.version, "parts": [{"label": p.label, "text": p.text} for p in case.parts],
            "granite_writing": granite_requests.granite_expected(case),
        } if case else None,
        "history": [{"version": c.version, "source_label": c.source_label,
                     "written_at": fmt.wall_time(c.written_at), "check_note": c.check_note}
                    for c in reversed(history)],
    })
    return out


def create_app(client_factory=redis_client) -> Flask:
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz():
        return "ok"

    @app.get("/api/state")
    def api_state():
        return jsonify(snapshot_json(state.load(client_factory())))

    @app.get("/api/ring/<ring_id>")
    def api_ring(ring_id):
        r = client_factory()
        snap = state.load(r)
        ring = snap.ring(ring_id)
        if ring is None:
            return jsonify({"error": "no ring %s" % ring_id}), 404
        return jsonify(ring_json(r, snap, ring))

    @app.get("/api/ring/<ring_id>/packet.md")
    def api_packet(ring_id):
        """The case packet as a file. `?version=` picks the reviewed version when given."""
        try:
            found = packet_export.build(client_factory(), ring_id, request.args.get("version"))
        except Exception:   # build never raises; a broken client factory still must not 500
            found = None
        if found is None:
            return jsonify({"error": "no case yet for %s; the case packet is available once the "
                                     "case writer has published an explanation" % ring_id}), 404
        name, markdown = found
        return Response(markdown, mimetype="text/markdown", headers={
            "Content-Disposition": 'attachment; filename="%s"' % name.replace('"', "")})

    @app.post("/api/decide")
    def api_decide():
        body = request.get_json(silent=True) or {}
        try:
            msg_id = decisions.write(client_factory(), body.get("ring_id", ""), body.get("version"),
                                     body.get("decision", ""), body.get("note", ""))
        except decisions.DecisionError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:   # Redis down between load and click
            return jsonify({"error": "could not save the decision: %s" % exc}), 503
        return jsonify({"ok": True, "id": msg_id, "label": decision_label(body.get("decision", ""))})

    @app.get("/")
    def index():
        return PAGE

    return app


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Mule Ring Hunter</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/@viz-js/viz@3.11.0/lib/viz-standalone.js"></script>
<style>
  :root { --bg:#fff; --fg:#1f2937; --muted:#6b7280; --line:#e5e7eb; --card:#f9fafb; --accent:#2563eb; --red:#dc2626; --amber:#d97706; --green:#059669; }
  body { margin:0; font:15px/1.45 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color:var(--fg); background:var(--bg); }
  header { padding:16px 24px 8px; } h1 { margin:0; font-size:22px; } header p { margin:4px 0 0; color:var(--muted); }
  .banner { margin:8px 24px; padding:10px 14px; border-radius:8px; background:#fef3c7; color:#92400e; display:none; }
  .error { background:#fee2e2; color:#991b1b; }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:12px; margin:8px 24px; }
  .tile { border:1px solid var(--line); border-radius:10px; padding:10px 14px; background:var(--card); }
  .tile .l { color:var(--muted); font-size:13px; } .tile .v { font-size:22px; font-weight:600; }
  .caption { color:var(--muted); font-size:13px; margin:0 24px 8px; }
  main { display:grid; grid-template-columns:minmax(260px, 1fr) 2.6fr; gap:24px; margin:0 24px 24px; }
  @media (max-width: 900px) { main { grid-template-columns:1fr; } }
  .list { border:1px solid var(--line); border-radius:10px; max-height:70vh; overflow:auto; }
  .item { padding:10px 12px; border-bottom:1px solid var(--line); cursor:pointer; }
  .item:hover { background:var(--card); } .item.sel { background:#eff6ff; border-left:3px solid var(--accent); }
  .item .t { font-weight:600; } .item .c { color:var(--muted); font-size:13px; }
  .detail h2 { margin:0 0 6px; font-size:20px; }
  .badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; font-weight:600; color:#fff; background:var(--accent); vertical-align:middle; margin-left:8px; }
  .badge.freeze { background:var(--red); } .badge.dismiss { background:var(--muted); } .badge.more_info { background:var(--amber); } .badge.granite { background:var(--green); } .badge.template { background:var(--muted); }
  .metrics { display:grid; grid-template-columns:repeat(4, 1fr); gap:12px; margin:10px 0; }
  .pic { border:1px solid var(--line); border-radius:10px; padding:10px; overflow:auto; min-height:120px; }
  .pic svg { max-width:100%; height:auto; }
  .cols { display:grid; grid-template-columns:2fr 3fr; gap:24px; margin-top:14px; }
  @media (max-width: 900px) { .cols { grid-template-columns:1fr; } }
  table { border-collapse:collapse; width:100%; font-size:14px; } td { padding:4px 6px; border-bottom:1px solid var(--line); vertical-align:top; } td:first-child { color:var(--muted); width:45%; }
  .form { margin-top:16px; border:1px solid var(--line); border-radius:10px; padding:12px; background:var(--card); }
  textarea { width:100%; box-sizing:border-box; min-height:70px; font:inherit; padding:8px; border:1px solid var(--line); border-radius:8px; }
  .buttons { display:flex; gap:10px; margin-top:10px; flex-wrap:wrap; }
  button { font:inherit; padding:8px 14px; border-radius:8px; border:1px solid var(--line); background:#fff; cursor:pointer; }
  button.primary { background:var(--red); color:#fff; border-color:var(--red); }
  a.button { display:inline-block; font:inherit; padding:8px 14px; border-radius:8px; border:1px solid var(--line); background:#fff; color:var(--fg); text-decoration:none; }
  a.button:hover { border-color:var(--fg); }
  .packet { margin-top:12px; padding-top:10px; border-top:1px solid var(--line); }
  .flash { color:var(--green); margin-top:8px; min-height:1.2em; } .flash.bad { color:var(--red); }
  label.chk { color:var(--muted); font-size:13px; margin-left:12px; }
  .muted { color:var(--muted); }
</style></head>
<body>
<header><h1>Mule Ring Hunter</h1><p>Investigator page. The AI recommends and explains; a person decides, and every decision is logged. Most recruited mules are young: 64 % of UK mule-indicative cases in H1 2023 involved people aged 30 or under (Cifas).</p></header>
<div id="banner" class="banner"></div>
<div id="tiles" class="tiles"></div>
<p id="updated" class="caption"></p>
<main>
  <section>
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
      <strong>Rings</strong>
      <span><label class="chk"><input type="checkbox" id="openOnly"> open only</label>
      <label class="chk"><input type="checkbox" id="byScore"> sort by score</label></span>
    </div>
    <div id="list" class="list"><div class="item muted">Waiting for data…</div></div>
  </section>
  <section class="detail" id="detail"><p class="muted">Select a ring on the left.</p></section>
</main>
<script>
const REFRESH = __REFRESH_MS__;
let selected = null, rings = [], viz = null, lastDot = "";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
Viz.instance().then(v => { viz = v; if (lastDot) drawDot(lastDot); });

async function getJSON(url, opts) { const r = await fetch(url, opts); return r.json(); }

function renderTiles(s) {
  $("tiles").innerHTML = s.tiles.map(t => `<div class="tile" title="${esc(t.help)}"><div class="l">${esc(t.label)}</div><div class="v">${esc(t.value)}</div></div>`).join("");
  const notes = ["Updated " + s.updated];
  const skipped = Object.entries(s.skipped || {});
  if (skipped.length) notes.push("skipped malformed messages: " + skipped.map(([k, v]) => k + " " + v).join(", "));
  $("updated").textContent = notes.join(" · ");
  const b = $("banner");
  if (!s.redis_ok) { b.className = "banner"; b.style.display = "block"; b.textContent = "Waiting for Redis at " + s.redis + ". The page will fill in once it is reachable."; }
  else if (s.errors && s.errors.length) { b.className = "banner error"; b.style.display = "block"; b.textContent = s.errors.join(" · "); }
  else b.style.display = "none";
}

function visibleRings() {
  let out = rings.slice();
  if ($("openOnly").checked) out = out.filter(r => !r.decision);
  if ($("byScore").checked) out.sort((a, b) => (parseFloat(b.xgb_score) || 0) - (parseFloat(a.xgb_score) || 0));
  return out;
}

function renderList() {
  const out = visibleRings();
  if (!out.length) { $("list").innerHTML = `<div class="item muted">${rings.length ? "No rings match the filters." : "No rings yet. They appear here as the ring service finds them."}</div>`; return; }
  if (!selected || !out.some(r => r.ring_id === selected)) { selected = out[0].ring_id; loadDetail(); }
  $("list").innerHTML = out.map(r => `<div class="item ${r.ring_id === selected ? "sel" : ""}" data-id="${esc(r.ring_id)}">
      <div class="t">${esc(r.ring_id)} · ${esc(r.shape_label)}</div>
      <div class="c">${r.n_accounts} accounts · XGB ${esc(r.xgb_score)} · GNN ${esc(r.gnn_score)} · ${r.decision ? esc(r.decision.label) : "Open"}</div></div>`).join("");
  document.querySelectorAll(".item[data-id]").forEach(el => el.onclick = () => { selected = el.dataset.id; renderList(); loadDetail(); });
}

function drawDot(dot) {
  lastDot = dot;
  if (!viz) return;
  const pic = $("pic"); if (!pic) return;
  try { pic.replaceChildren(viz.renderSVGElement(dot)); } catch (e) { pic.textContent = "Could not draw the ring: " + e; }
}

async function loadDetail() {
  if (!selected) return;
  const d = await getJSON("/api/ring/" + encodeURIComponent(selected));
  if (d.error || d.ring_id !== selected) return;
  const dec = d.decision ? `<span class="badge ${esc(d.decision.decision)}" title="Decided ${esc(d.decision.decided_at)}">${esc(d.decision.label)}</span>` : `<span class="badge">Open</span>`;
  const noteBox = $("note"); const keepNote = noteBox && noteBox.dataset.ring === d.ring_id ? noteBox.value : "";
  const facts = d.facts.length ? `<p><strong>Numbers behind the summary</strong></p><table>${d.facts.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("")}</table>
      <p class="muted" style="font-size:13px">Computed by our code from the ring. The fact check rejects any Granite summary that uses a number not in this set.</p>` : "";
  const c = d.case;
  const summary = c ? `<span class="badge ${c.is_granite ? "granite" : "template"}" title="${esc(c.check_note || "no note")} · written ${esc(c.written_at)}">${esc(c.source_label)}</span>
      ${c.granite_writing ? `<p class="muted" style="font-size:13px">Granite is writing an explanation; the template and the numbers below stand in meanwhile.</p>` : ""}
      ${c.version !== d.version ? `<p class="muted" style="font-size:13px">Written for version ${c.version}; the ring is now at version ${d.version}.</p>` : ""}
      ${c.parts.map(p => `<p><strong>${esc(p.label)}</strong><br>${esc(p.text)}</p>`).join("")}
      ${d.history.length > 1 ? `<details><summary class="muted">Case history (${d.history.length})</summary>${d.history.map(h => `<div class="muted" style="font-size:13px">Version ${h.version} · ${esc(h.source_label)} · ${esc(h.written_at)} · ${esc(h.check_note || "no note")}</div>`).join("")}</details>` : ""}`
    : `<p class="muted">Summary coming. The case writer has not written this ring yet.</p>`;
  $("detail").innerHTML = `
    <h2>${esc(d.ring_id)} · ${esc(d.shape_label)} ${dec}</h2>
    <div class="metrics">
      <div class="tile"><div class="l">XGBoost score</div><div class="v">${esc(d.xgb_score)}</div></div>
      <div class="tile" title="${d.gnn_score === "–" ? "No GNN score sent for this ring." : ""}"><div class="l">GNN score</div><div class="v">${esc(d.gnn_score)}</div></div>
      <div class="tile"><div class="l">Accounts</div><div class="v">${d.n_accounts}</div></div>
      <div class="tile"><div class="l">Money moved</div><div class="v">${esc(d.money)}</div></div>
    </div>
    <p class="caption" style="margin:0 0 8px">Version ${d.version} · data time ${esc(d.first_seen)} to ${esc(d.last_seen)}${d.span ? " (" + esc(d.span) + ")" : ""}</p>
    <div id="pic" class="pic"></div>
    <p class="caption" style="margin:6px 0 0">${esc(d.picture_caption)} ${d.case && d.roles_known ? "" : "Key account, origin and collector are highlighted once the case arrives."} Hover an account or arrow for details.</p>
    <div class="cols">
      <div><p><strong>Why it was flagged</strong></p>${d.reasons.length ? "<ol>" + d.reasons.map(x => `<li>${esc(x)}</li>`).join("") + "</ol>" : `<p class="muted">No reasons were sent with this ring.</p>`}${facts}</div>
      <div><p><strong>Case summary</strong> ${summary}</p></div>
    </div>
    <div class="form">
      <strong>Your decision on ${esc(d.ring_id)}</strong>
      ${d.decision ? `<p class="muted" style="font-size:13px">Last decision: ${esc(d.decision.label)}, ${esc(d.decision.decided_at)}${d.decision.note ? ". Note: " + esc(d.decision.note) : ""}</p>` : ""}
      <textarea id="note" data-ring="${esc(d.ring_id)}" maxlength="1000" placeholder="Optional. Why? This goes into the audit log.">${esc(keepNote)}</textarea>
      <div class="buttons">
        <button class="primary" onclick="decide('freeze')">Freeze accounts</button>
        <button onclick="decide('more_info')">Needs more info</button>
        <button onclick="decide('dismiss')">Dismiss</button>
      </div>
      <div id="flash" class="flash"></div>
      <div class="packet">
        ${d.case ? `<a class="button" href="/api/ring/${encodeURIComponent(d.ring_id)}/packet.md?version=${d.version}" download>Download case packet (.md)</a>
        <p class="muted" style="font-size:13px;margin:6px 0 0">A Markdown draft of the case for filing: summary, facts, accounts, transfers with transaction IDs, decision log. Every number traces to clean.csv.</p>`
        : `<button disabled>Download case packet (.md)</button>
        <p class="muted" style="font-size:13px;margin:6px 0 0">The case packet is available once the case writer has published an explanation.</p>`}
      </div>
    </div>`;
  drawDot(d.dot);
}

async function decide(decision) {
  const ring = rings.find(r => r.ring_id === selected); if (!ring) return;
  const res = await getJSON("/api/decide", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ring_id: selected, version: ring.version, decision, note: $("note").value})});
  const f = $("flash");
  if (res.ok) { f.className = "flash"; f.textContent = "Saved: " + res.label + " for " + selected + " (version " + ring.version + ")."; $("note").value = ""; refresh(); }
  else { f.className = "flash bad"; f.textContent = res.error || "Could not save the decision."; }
}

async function refresh() {
  try {
    const s = await getJSON("/api/state");
    renderTiles(s); rings = s.rings; renderList();
    if (selected) loadDetail();
  } catch (e) { const b = $("banner"); b.className = "banner error"; b.style.display = "block"; b.textContent = "The page cannot reach its own server: " + e; }
}
$("openOnly").onchange = renderList; $("byScore").onchange = renderList;
refresh(); setInterval(refresh, REFRESH);
</script></body></html>
""".replace("__REFRESH_MS__", str(REFRESH_MS))


def main(argv=None):
    p = argparse.ArgumentParser(description="Investigator page, Flask fallback")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8501)
    args = p.parse_args(argv)
    create_app().run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
