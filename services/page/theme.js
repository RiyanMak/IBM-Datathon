/* The palette changes in the existing document. No navigation, reconnect, or note loss.
 * Streamlit's stored theme is also updated for the next visit to any page. */
(() => {
  "use strict";
  const host = window.parent;
  let doc;
  try { doc = host.document; } catch (_) { return; }
  const keys = __THEME_KEYS__;
  const apply = mode => {
    doc.documentElement.dataset.mrhTheme = mode;
    doc.documentElement.style.colorScheme = mode;
    try {
      keys.forEach(key => host.localStorage.setItem(key, JSON.stringify(mode === "dark" ? "Dark" : "Light")));
    } catch (_) { /* Storage may be disabled; the current session still works. */ }
  };
  apply(__ACTIVE_THEME__);
  // Keep one host listener across Streamlit reruns and page navigation.
  if (host.mrhThemeListener) doc.removeEventListener("change", host.mrhThemeListener, true);
  host.mrhThemeListener = event => {
    const input = event.target;
    if (!input.matches('.st-key-utility input[type="checkbox"]')) return;
    apply(input.checked ? "dark" : "light");
  };
  doc.addEventListener("change", host.mrhThemeListener, true);
})();
