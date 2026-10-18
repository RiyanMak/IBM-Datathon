/* One host-document controller for reveals, page transitions and the Start backdrop.
 * Reduced-motion viewers get static content immediately. */
(() => {
  "use strict";
  let doc;
  try { doc = window.parent.document; } catch (err) { return; }
  if (!doc || doc.documentElement.dataset.mrhMotion) return;
  doc.documentElement.dataset.mrhMotion = "1";
  const reduce = window.parent.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const host = window.parent;
  const motionPreference = host.matchMedia("(prefers-reduced-motion: reduce)");
  let field, ctx, frame = 0, lastFrame = 0, width = 0, height = 0;
  let points = [], columns = 0, rows = 0, palette = "";
  const cursor = {x: 0, y: 0, active: false};
  const spacing = 64;
  const visible = () => field?.isConnected && doc.location.pathname === "/" && !doc.hidden;
  const wake = () => {
    if (visible() && !frame) frame = host.requestAnimationFrame(drawField);
  };
  // A shared mesh gives dots and grid lines the same soft, local displacement.
  // No ambient motion: the field sleeps once it has settled.
  const drawField = now => {
    frame = 0;
    if (!visible()) return;
    const dt = Math.min((now - (lastFrame || now - 16)) / 1000, .05);
    lastFrame = now;
    const ease = 1 - Math.exp(-dt * 10);
    let moving = false;
    points.forEach(point => {
      const vx = point.x - cursor.x, vy = point.y - cursor.y;
      const distance = Math.hypot(vx, vy);
      const near = cursor.active && !motionPreference.matches ? Math.max(0, 1 - distance / 160) : 0;
      const push = near * near * 28;
      const tx = distance ? vx / distance * push : push;
      const ty = distance ? vy / distance * push : 0;
      point.dx += (tx - point.dx) * ease;
      point.dy += (ty - point.dy) * ease;
      if (Math.abs(tx - point.dx) + Math.abs(ty - point.dy) > .02) moving = true;
      else { point.dx = tx; point.dy = ty; }
      point.near = near;
    });
    ctx.clearRect(0, 0, width, height);
    const colors = palette.split("|");
    ctx.strokeStyle = colors[0];
    ctx.lineWidth = 1;
    ctx.globalAlpha = 1;
    ctx.beginPath();
    const vertex = (point, first) => {
      const x = point.x + point.dx, y = point.y + point.dy;
      if (first) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    };
    for (let row = 0; row < rows; row++) {
      for (let col = 0; col < columns; col++) vertex(points[row * columns + col], col === 0);
    }
    for (let col = 0; col < columns; col++) {
      for (let row = 0; row < rows; row++) vertex(points[row * columns + col], row === 0);
    }
    ctx.stroke();
    ctx.fillStyle = colors[1];
    points.forEach((point, index) => {
      // Sparse, tiny translucent beads, anchored at alternating intersections.
      if ((Math.floor(index / columns) + index % columns) % 2) return;
      ctx.globalAlpha = .20 + point.near * .30;
      ctx.beginPath();
      ctx.arc(point.x + point.dx, point.y + point.dy, 1.3 + point.near * .4, 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.globalAlpha = 1;
    if (moving) wake(); else lastFrame = 0;
  };
  const syncField = () => {
    const entry = doc.location.pathname === "/";
    const app = doc.querySelector(".stApp");
    if (entry && app && !field?.isConnected) {
      field = doc.createElement("canvas");
      field.className = "entry-field";
      field.setAttribute("aria-hidden", "true");
      ctx = field.getContext("2d");
      if (!ctx) return;
      app.appendChild(field);
      app.classList.add("has-entry-field");
      width = 0;
    }
    if (!field || !ctx) return;
    field.hidden = !entry;
    if (!visible()) {
      host.cancelAnimationFrame(frame);
      frame = 0;
      lastFrame = 0;
      cursor.active = false;
      return;
    }
    const ratio = Math.min(host.devicePixelRatio || 1, 2);
    if (width !== host.innerWidth || height !== host.innerHeight || field.width !== Math.round(width * ratio)) {
      width = host.innerWidth; height = host.innerHeight;
      field.width = Math.round(width * ratio); field.height = Math.round(height * ratio);
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      columns = Math.ceil(width / spacing) + 3;
      rows = Math.ceil(height / spacing) + 3;
      points = Array.from({length: columns * rows}, (_, i) => ({
        x: (i % columns - 1) * spacing, y: (Math.floor(i / columns) - 1) * spacing,
        dx: 0, dy: 0, near: 0,
      }));
      wake();
    }
    const style = host.getComputedStyle(app);
    const nextPalette = `${style.getPropertyValue("--grid").trim()}|${style.getPropertyValue("--accent").trim()}`;
    if (palette !== nextPalette) { palette = nextPalette; wake(); }
  };
  doc.addEventListener("pointermove", event => {
    if (event.pointerType === "touch" || motionPreference.matches) return;
    cursor.x = event.clientX;
    cursor.y = event.clientY;
    cursor.active = true;
    wake();
  }, {passive: true});
  const release = () => { cursor.active = false; wake(); };
  doc.addEventListener("pointerout", event => { if (!event.relatedTarget) release(); });
  host.addEventListener("blur", release);
  host.addEventListener("resize", syncField);
  doc.addEventListener("visibilitychange", () => { syncField(); wake(); });
  motionPreference.addEventListener("change", () => {
    if (motionPreference.matches) points.forEach(point => { point.dx = 0; point.dy = 0; });
    release();
  });
  const show = node => node.classList.add("is-in");
  const io = reduce ? null : new IntersectionObserver(entries => entries.forEach(entry => {
    if (entry.isIntersecting) { show(entry.target); io.unobserve(entry.target); }
  }), {rootMargin: "0px 0px -6% 0px", threshold: 0.08});
  const scan = () => doc.querySelectorAll(".reveal:not([data-seen])").forEach(node => {
    node.dataset.seen = "1";
    if (io) io.observe(node); else show(node);
  });
  let path = doc.location.pathname;
  const enter = () => {
    const main = doc.querySelector(".block-container");
    if (!main || reduce) return;
    main.classList.remove("page-enter");
    void main.offsetWidth;
    main.classList.add("page-enter");
  };
  const countUp = node => {
    const target = Number(String(node.textContent).replace(/,/g, ""));
    if (reduce || !Number.isFinite(target) || target === 0 || node.dataset.counted) return;
    node.dataset.counted = "1";
    const start = performance.now(), ms = 700;
    const step = now => {
      const t = Math.min(1, (now - start) / ms), eased = 1 - Math.pow(1 - t, 3);
      node.textContent = Math.round(target * eased).toLocaleString();
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };
  const tick = () => {
    doc.body.classList.toggle("is-entry", doc.location.pathname === "/");
    syncField();
    doc.querySelectorAll(".entry-foot strong:not([data-counted])").forEach(countUp);
    if (doc.location.pathname !== path) { path = doc.location.pathname; enter(); }
    const header = doc.querySelector('header[data-testid="stHeader"]');
    if (header) header.classList.toggle("is-scrolled", (doc.scrollingElement || doc.documentElement).scrollTop > 8 ||
      (doc.querySelector('[data-testid="stAppViewContainer"] > section')?.scrollTop || 0) > 8);
  };
  scan();
  tick();
  new MutationObserver(scan).observe(doc.body, {childList: true, subtree: true});
  setInterval(tick, 200);
})();
