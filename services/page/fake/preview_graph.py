"""Write a file-openable harness for the actual graph component (no server).
This is a component QA tool, not the Streamlit application or an integration demo.
"""
import argparse
from html import escape
import json
from pathlib import Path
from services.page import state, workspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/tmp/mrh-graph-preview.html")
    args = parser.parse_args()
    page = Path(__file__).resolve().parents[1]
    rings = json.loads((page.parent / "casewriter/fake_rings.json").read_text())
    payloads = []
    for ring in rings:
        fields = {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in ring.items()}
        view = state.parse_ring("0-0", fields)
        payloads.append({"id": view.ring_id, "graph": workspace.graph_data(view)})
    component = (page / "flow_component/index.html").read_text()
    component = component.replace('<link rel="stylesheet" href="style.css">', '<style>' + (page / "flow_component/style.css").read_text() + '</style>')
    component = component.replace('<script src="main.js"></script>', '<script>' + (page / "flow_component/main.js").read_text() + '</script>')
    markup = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Money flow · component QA</title>
<style>body{margin:24px;background:#f5f7f4;font:15px system-ui;color:#20332e}h1{font-size:24px}button,select{padding:10px;margin:4px;border:1px solid #b6c6bc;border-radius:6px;background:white;color:#20332e}iframe{border:1px solid #d6e0d9;border-radius:8px;display:block;margin-top:16px;height:480px;width:100%}#frame{max-width:100%}#selection{padding:16px;background:#e6f2ec;overflow-wrap:anywhere}button:focus-visible,select:focus-visible{outline:3px solid #146d59}</style>
<h1>Mule Ring Hunter / graph component QA</h1><p>Fixture simulation. This harness renders the actual component; it does not run the Streamlit page or save decisions.</p>
<label>Viewport <select id="width"><option value="1120">Wide · graph 1120px</option><option value="840">Laptop · graph 840px</option><option value="358">Narrow · graph 358px</option></select></label>
<label>Case <select id="case"><option value="0">R-0001 · scatter / gather</option><option value="1">R-0002 · fan out</option><option value="2">R-0003 · cycle</option></select></label>
<button id="theme">Toggle graph theme</button><button id="reset">Clear selection</button><div id="frame" style="width:1120px"><iframe id="component" title="Case money flow" srcdoc="COMPONENT"></iframe><p id="selection">Select an account or transfer.</p></div>
<script>const payloads=PAYLOAD; const frame=document.getElementById('component'); let theme='light',current=0;
function render(){frame.contentWindow.postMessage({type:'streamlit:render',args:{graph:payloads[current].graph,theme}},'*')}
window.addEventListener('message',e=>{if(e.source!==frame.contentWindow)return; if(e.data.type==='streamlit:componentReady')render();if(e.data.type==='streamlit:setFrameHeight')frame.style.height=e.data.height+'px';if(e.data.type==='streamlit:setComponentValue'){const key=e.data.value.key;payloads[current].graph.selection=key;const edge=payloads[current].graph.edges.find(x=>x.key===key);document.getElementById('selection').textContent=edge?edge.label:key;render()}});
document.getElementById('width').onchange=e=>document.getElementById('frame').style.width=e.target.value+'px';document.getElementById('case').onchange=e=>{current=Number(e.target.value);render()};document.getElementById('theme').onclick=()=>{theme=theme==='light'?'dark':'light';render()};document.getElementById('reset').onclick=()=>{payloads[current].graph.selection='overview';document.getElementById('selection').textContent='Select an account or transfer.';render()};frame.onload=render;
</script></html>'''
    markup = markup.replace("COMPONENT", escape(component, quote=True)).replace("PAYLOAD", json.dumps(payloads).replace("</", "<\\/"))
    Path(args.out).write_text(markup)
    print(args.out)


if __name__ == "__main__":
    main()
