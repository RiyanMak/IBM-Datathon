/* Injected once per browser session from a zero-height component frame. It only adds
 * classes in the host document: reveal-on-scroll for `.reveal` blocks and a short page
 * transition after navigation. Reduced-motion viewers get static content immediately. */
(() => {
  "use strict";
  let doc;
  try { doc = window.parent.document; } catch (err) { return; }
  if (!doc || doc.documentElement.dataset.mrhMotion) return;
  doc.documentElement.dataset.mrhMotion = "1";
  const reduce = window.parent.matchMedia("(prefers-reduced-motion: reduce)").matches;
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
    doc.querySelectorAll(".entry-foot strong:not([data-counted])").forEach(countUp);
    if (doc.location.pathname !== path) { path = doc.location.pathname; enter(); }
    const header = doc.querySelector('header[data-testid="stHeader"]');
    if (header) header.classList.toggle("is-scrolled", (doc.scrollingElement || doc.documentElement).scrollTop > 8 ||
      (doc.querySelector('[data-testid="stAppViewContainer"] > section')?.scrollTop || 0) > 8);
  };
  scan();
  new MutationObserver(scan).observe(doc.body, {childList: true, subtree: true});
  setInterval(tick, 200);
})();
