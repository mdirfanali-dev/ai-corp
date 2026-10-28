/* Notification bell: unread count (refreshed every few seconds) and a dropdown with the latest items.
   Text is inserted with textContent only. */
(() => {
  "use strict";
  const btn = document.getElementById("bell-btn"), count = document.getElementById("bell-count"), panel = document.getElementById("bell-panel");
  if (!btn) return;
  const csrf = document.querySelector('meta[name="csrf"]').content;
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text) n.textContent = text; return n; };

  async function summary() {
    try {
      const r = await fetch("/api/notifications/summary", { cache: "no-store" });
      if (!r.ok) return;
      const d = await r.json();
      count.textContent = d.unread; count.hidden = d.unread === 0;
      count.classList.toggle("critical", d.critical > 0);
    } catch (e) { /* offline: keep the last number */ }
  }

  async function openPanel() {
    panel.replaceChildren(el("div", "muted", "Loading…")); panel.hidden = false; btn.setAttribute("aria-expanded", "true");
    try {
      const r = await fetch("/api/notifications?state=unread&limit=6", { cache: "no-store" });
      const items = await r.json();
      const nodes = items.map((i) => {
        const a = el("a", "bellitem sev-" + i.severity); a.href = i.link;
        a.append(el("b", "", i.title), el("span", "muted", i.category + " · " + i.created_at.slice(11, 16) + " UTC"));
        return a;
      });
      if (!nodes.length) nodes.push(el("div", "muted", "Nothing new."));
      const foot = el("div", "bellfoot");
      const all = el("a", "", "See all"); all.href = "/inbox";
      const mark = el("button", "mini", "Mark all read"); mark.type = "button";
      mark.addEventListener("click", async () => {
        await fetch("/inbox/action", { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
                                       body: new URLSearchParams({ csrf_token: csrf, action: "read_all", f_state: "unread" }) });
        panel.hidden = true; summary();
      });
      foot.append(all, mark);
      panel.replaceChildren(...nodes, foot);
    } catch (e) { panel.replaceChildren(el("div", "muted", "Could not load.")); }
  }

  btn.addEventListener("click", () => { if (panel.hidden) openPanel(); else { panel.hidden = true; btn.setAttribute("aria-expanded", "false"); } });
  document.addEventListener("click", (e) => { if (!panel.hidden && !e.target.closest(".bellwrap")) panel.hidden = true; });
  setInterval(summary, 6000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) summary(); });
})();
