/* Local Streamlit component: SVG only, no remote assets or JavaScript packages.
 * Uses the public v1 component postMessage protocol. */
(() => {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.getElementById("graph");
  const viewport = document.getElementById("viewport");
  let data, zoom = 1, previous = "", selected = "overview";
  const send = (type, extra = {}) => window.parent.postMessage({isStreamlitMessage:true,type,...extra}, "*");
  const el = (tag, attrs = {}, text = null) => {
    const item = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([key, val]) => item.setAttribute(key, String(val)));
    if (text !== null) item.textContent = text;
    return item;
  };
  const choose = key => {
    selected = key;
    highlight();
    send("streamlit:setComponentValue", {value:{key, nonce:Date.now()+":"+Math.random()},dataType:"json"});
  };
  const activate = (item, key) => {
    item.addEventListener("click", () => choose(key));
    item.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(key); }
    });
  };
  function highlight() {
    const edge = data.edges.find(e => e.key === selected);
    const account = selected.startsWith("account:") ? selected.slice(8) : null;
    const related = new Set(edge ? [edge.src,edge.dst] : account ? [account] : []);
    data.edges.forEach(e => { if (account && (e.src === account || e.dst === account)) {related.add(e.src);related.add(e.dst);} });
    svg.querySelectorAll(".node").forEach(n => {
      const active = "account:"+n.dataset.id === selected;
      n.classList.toggle("selected", active);
      n.classList.toggle("related", related.has(n.dataset.id));
      n.classList.toggle("dim", selected !== "overview" && !related.has(n.dataset.id));
      n.setAttribute("aria-pressed", String(active));
    });
    svg.querySelectorAll(".edge").forEach((n, i) => {
      const e = data.edges[i];
      const active = e.key === selected;
      const linked = active || (account && (e.src === account || e.dst === account));
      n.classList.toggle("selected", active);
      n.classList.toggle("related", !!linked);
      n.classList.toggle("dim", selected !== "overview" && !linked);
      n.setAttribute("aria-pressed", String(active));
      n.querySelector(".line").setAttribute("marker-end", linked ? "url(#arrow-active)" : "url(#arrow)");
    });
  }
  function size() {
    svg.style.width = zoom === 1 ? "100%" : Math.max(viewport.clientWidth, 700)*zoom+"px";
    svg.style.height = 360*zoom+"px";
    send("streamlit:setFrameHeight", {height:document.body.scrollHeight});
  }
  function render(next) {
    data = next;
    selected = data.selection || "overview";
    const signature = JSON.stringify([data.nodes,data.edges]);
    if (signature !== previous) {
      const focused = document.activeElement?.dataset?.key;
      svg.replaceChildren();
      svg.setAttribute("viewBox", `0 0 ${data.width} ${data.height}`);
      const defs = el("defs");
      for (const [id,color] of [["arrow","var(--line)"],["arrow-active","var(--accent)"]]) {
        const marker = el("marker", {id,viewBox:"0 0 10 10",refX:9,refY:5,markerWidth:7,markerHeight:7,orient:"auto-start-reverse",markerUnits:"userSpaceOnUse"});
        marker.append(el("path", {d:"M 0 0 L 10 5 L 0 10 z",fill:color})); defs.append(marker);
      }
      svg.append(defs);
      const byId = new Map(data.nodes.map(n => [n.id,n]));
      const pairs = new Map();
      for (const e of data.edges) {
        const a = byId.get(e.src), b = byId.get(e.dst);
        const pair = JSON.stringify([e.src,e.dst]);
        const nth = pairs.get(pair) || 0; pairs.set(pair,nth+1);
        let path;
        if (a.id === b.id) {
          path = `M ${a.x-12} ${a.y-15} C ${a.x-65-nth*12} ${a.y-88} ${a.x+65+nth*12} ${a.y-88} ${a.x+12} ${a.y-15}`;
        } else {
          const dx = b.x-a.x, dy = b.y-a.y, length = Math.hypot(dx,dy);
          const ax = a.x+dx/length*21, ay = a.y+dy/length*21;
          const bx = b.x-dx/length*23, by = b.y-dy/length*23;
          const bend = nth*22 + (data.edges.some(other => other.src === e.dst && other.dst === e.src) ? 26 : 0);
          path = `M ${ax} ${ay} Q ${(ax+bx)/2-dy/length*bend} ${(ay+by)/2+dx/length*bend} ${bx} ${by}`;
        }
        const group = el("g", {class:"edge",tabindex:0,role:"button","aria-label":"Inspect transfer: "+e.label,"data-key":e.key});
        group.append(el("title",{},e.label), el("path",{class:"line",d:path,"marker-end":"url(#arrow)"}),el("path",{class:"hit",d:path}));
        activate(group,e.key); svg.append(group);
      }
      for (const n of data.nodes) {
        const group = el("g",{class:"node",tabindex:0,role:"button","aria-label":"Inspect account "+n.id,"data-id":n.id,"data-key":"account:"+n.id,transform:`translate(${n.x} ${n.y})`});
        group.append(el("title",{},n.id),el("circle",{class:"outer",r:24,stroke:"none"}),el("circle",{r:19}),el("text",{y:4},n.label),el("text",{class:"account-label",y:42},n.short));
        activate(group,"account:"+n.id); svg.append(group);
      }
      if (focused) [...svg.querySelectorAll("[data-key]")].find(n => n.dataset.key === focused)?.focus({preventScroll:true});
      previous = signature;
    }
    document.getElementById("scope").textContent = data.scope + (data.limited ? " · limited view; all transfers available below" : "");
    highlight(); size();
  }
  window.addEventListener("message", event => {
    if (event.source !== window.parent || event.data?.type !== "streamlit:render") return;
    document.body.classList.toggle("dark", event.data.args.theme === "dark");
    render(event.data.args.graph);
  });
  document.getElementById("zoom-in").onclick = () => {zoom=Math.min(3,zoom+.25);size();};
  document.getElementById("zoom-out").onclick = () => {zoom=Math.max(1,zoom-.25);size();};
  document.getElementById("fit").onclick = () => {zoom=1;viewport.scrollTo(0,0);size();};
  new ResizeObserver(size).observe(viewport);
  send("streamlit:componentReady", {apiVersion:1});
  send("streamlit:setFrameHeight", {height:460});
})();
