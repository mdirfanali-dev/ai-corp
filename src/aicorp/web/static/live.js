/* AICorp live dashboard. Plain JavaScript, no external libraries.
   All data is inserted with textContent / setAttribute — never as HTML. */
(() => {
  "use strict";
  const SVG = "http://www.w3.org/2000/svg";
  const $ = (id) => document.getElementById(id);
  const el = (tag, props = {}, ...kids) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid) node.append(kid);
    return node;
  };
  const svg = (tag, attrs = {}, text) => {
    const node = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  const url = (path, params) => path + "?" + new URLSearchParams(params).toString();
  const hhmmss = (iso) => (iso || "").slice(11, 19);

  const shown = {};              // last rendered JSON per section: skip redraws when nothing changed
  const changed = (key, value) => {
    const text = JSON.stringify(value);
    if (shown[key] === text) return false;
    shown[key] = text;
    return true;
  };

  // ------------------------------------------------------------------ cards
  const STATE_LABEL = { working: "working", blocked: "blocked", idle: "idle", paused: "paused" };

  function card(c) {
    const task = c.task;
    const body = [];
    if (task && task.status === "running") {
      const pct = task.progress == null ? 0 : task.progress;
      body.push(
        el("div", { class: "ctask" }, el("a", { href: "/tasks/" + task.task_id, text: clip(task.title, 44) })),
        el("div", { class: "bar-track", title: pct + "%" }, el("div", { class: "bar-fill", style: "width:" + pct + "%" })),
        el("div", { class: "hint", text: "attempt " + task.attempts + (task.progress == null ? "" : " · " + pct + "%") }),
      );
    } else if (task && task.status === "blocked") {
      body.push(
        el("div", { class: "ctask" }, el("a", { href: "/tasks/" + task.task_id, text: clip(task.title, 44) })),
        el("div", { class: "blockedmsg", text: "Blocked: " + task.block.reason.replace(/_/g, " ").toLowerCase() }),
      );
    } else {
      body.push(el("div", { class: "hint", text: c.waiting ? c.waiting + " task(s) waiting" : "nothing to do" }));
    }
    for (const t of c.needs_you) {
      body.push(el("div", { class: "hint" }, el("a", { href: "/tasks/" + t.task_id, text: t.status.replace(/_/g, " ") + ": " + clip(t.title, 32) })));
    }
    const actions = el("div", { class: "cactions" },
      el("a", { class: "mini", href: url("/logs", { actor: c.name }), text: "Log" }),
      el("a", { class: "mini", href: url("/tasks", { owner: c.emp_id }), text: "Tasks" }),
      task && ["running", "assigned", "blocked"].includes(task.status)
        ? el("a", { class: "mini", href: "/tasks/" + task.task_id + "/takeover", title: "Stop the agent and work in this task yourself", text: "Take over" })
        : el("button", { class: "mini", type: "button", disabled: true, title: "Nothing to take over", text: "Take over" }),
      el("a", { class: "mini", href: "/impersonate?emp=" + encodeURIComponent(c.emp_id), title: "Act as this person (logged)", text: "Impersonate" }),
    );
    return el("div", { class: "card s-" + c.state, "data-emp": c.emp_id, "data-name": c.name },
      el("div", { class: "chead" },
        el("a", { class: "cname", href: "/employees/" + c.emp_id, text: c.name }),
        el("span", { class: "badge state-" + c.state, text: STATE_LABEL[c.state] || c.state }),
        c.kind === "temporary" ? el("span", { class: "badge temporary", text: "temp" }) : null,
      ),
      el("div", { class: "hint", text: (c.roles.join(", ") || "no role") + (c.reports_to ? " · under " + c.reports_to : "") }),
      ...body, actions);
  }

  function renderOrg(data) {
    const key = { cards: data.cards, teams: data.teams, leaders: data.leaders, bench: data.bench };
    if (!changed("org", key)) return;
    const leaders = $("leaders");
    leaders.replaceChildren(...data.leaders.map((id) => card(data.cards[id])));
    const lanes = $("lanes");
    const out = [];
    for (const team of data.teams) {
      out.push(el("section", { class: "lane" },
        el("div", { class: "lane-head" },
          el("a", { href: "/teams/" + team.team_id, class: "lane-title", text: team.name }),
          el("span", { class: "muted", text: team.head ? "head: " + team.head : "no head" }),
          el("span", { class: "badge " + (team.working ? "on" : ""), text: team.working + " working" }),
          team.blocked ? el("span", { class: "badge archived", text: team.blocked + " blocked" }) : null),
        el("div", { class: "cards" }, ...team.members.map((id) => card(data.cards[id])))));
    }
    if (data.bench.length) {
      out.push(el("section", { class: "lane" },
        el("div", { class: "lane-head" }, el("span", { class: "lane-title", text: "No team" })),
        el("div", { class: "cards" }, ...data.bench.map((id) => card(data.cards[id])))));
    }
    if (!out.length) out.push(el("p", { class: "muted", text: "No teams yet. Create a team and add employees." }));
    lanes.replaceChildren(...out);
  }

  // ------------------------------------------------------------------ stats
  function renderStats(data) {
    const s = data.stats;
    $("st-running").textContent = s.running + (s.cap == null ? "" : " / " + s.cap);
    $("st-ram").textContent = s.ram + "%";
    $("st-queue").textContent = s.queue;
    $("st-blocked").textContent = s.blocked;
    $("st-need").textContent = s.needs_you;
    const e = data.engine;
    let note;
    if (data.kill) note = "Kill switch engaged: nothing will start.";
    else if (!e.running) note = "The task engine is not running, so tasks will wait. Start it with: python -m aicorp.engine";
    else note = "Engine running" + (e.mode === "simulated" ? " in DEMO mode: work is simulated." : ".");
    $("engine-note").textContent = note;
  }

  // ------------------------------------------------------------------ dependency graph
  const NODE_W = 190, NODE_H = 44, GAP_X = 70, GAP_Y = 18;

  function layout(nodes, edges) {
    const level = {};
    const parents = {};
    for (const n of nodes) parents[n.task_id] = [];
    for (const e of edges) parents[e.to].push(e.from);
    const depth = (id, seen = new Set()) => {
      if (level[id] !== undefined) return level[id];
      if (seen.has(id)) return 0;               // defensive: cycles are rejected server-side
      seen.add(id);
      level[id] = parents[id].length ? 1 + Math.max(...parents[id].map((p) => depth(p, seen))) : 0;
      return level[id];
    };
    nodes.forEach((n) => depth(n.task_id));
    const columns = {};
    for (const n of nodes) (columns[level[n.task_id]] ||= []).push(n);
    const pos = {};
    let maxY = 0;
    for (const [lvl, list] of Object.entries(columns)) {
      list.forEach((n, i) => {
        pos[n.task_id] = { x: lvl * (NODE_W + GAP_X), y: i * (NODE_H + GAP_Y) };
        maxY = Math.max(maxY, pos[n.task_id].y + NODE_H);
      });
    }
    const maxX = Math.max(...Object.values(pos).map((p) => p.x)) + NODE_W;
    return { pos, width: maxX, height: maxY };
  }

  function renderGraph(data) {
    const { nodes, edges } = data.graph;
    if (!changed("graph", data.graph)) return;
    $("graph-empty").hidden = nodes.length > 0;
    if (!nodes.length) { $("graph").replaceChildren(); return; }
    const { pos, width, height } = layout(nodes, edges);
    const root = svg("svg", { viewBox: `0 0 ${width + 4} ${height + 4}`, width: Math.min(width + 4, 1060), class: "graph-svg", role: "img" });
    root.append(svg("title", {}, "Task dependency graph"));
    const defs = svg("defs");
    const marker = svg("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" });
    marker.append(svg("path", { d: "M0 0 L10 5 L0 10 z", class: "arrow" }));
    defs.append(marker);
    root.append(defs);
    for (const e of edges) {
      const a = pos[e.from], b = pos[e.to];
      if (!a || !b) continue;
      const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2, mid = (x1 + x2) / 2;
      root.append(svg("path", { d: `M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2 - 2} ${y2}`, class: "edge", "marker-end": "url(#arrow)" }));
    }
    for (const n of nodes) {
      const p = pos[n.task_id];
      const link = svg("a", { href: "/tasks/" + n.task_id });
      link.append(
        svg("rect", { x: p.x, y: p.y, width: NODE_W, height: NODE_H, rx: 8, class: "node s-" + n.status }),
        svg("text", { x: p.x + 10, y: p.y + 19, class: "node-title" }, clip(n.title, 24)),
        svg("text", { x: p.x + 10, y: p.y + 35, class: "node-sub" }, (n.owner || "unassigned") + " · " + n.status.replace(/_/g, " ")),
      );
      link.prepend(svg("title", {}, n.title + " (" + n.status + ")"));
      root.append(link);
    }
    $("graph").replaceChildren(root);
  }

  // ------------------------------------------------------------------ live feed
  const FEED_MAX = 40;
  const seenEvents = new Set();

  function who(e) {
    return e.actor_name + (e.acting_as_name ? " ▶ acting as " + e.acting_as_name : "");
  }
  function feedRow(e) {
    const link = e.task_id ? url("/logs", { task: e.task_id }) : url("/logs", { type: e.type });
    return el("tr", { "data-event": e.id },
      el("td", { class: "muted", text: hhmmss(e.ts) }),
      el("td", { text: who(e) }),
      el("td", {}, el("span", { class: "badge", text: e.type }), " ", el("a", { href: link, text: e.summary })));
  }
  function addEvents(list, replace = false) {
    const body = $("feed");
    if (replace) { body.replaceChildren(); seenEvents.clear(); }
    for (const e of [...list].sort((a, b) => a.id - b.id)) {
      if (seenEvents.has(e.id)) continue;
      seenEvents.add(e.id);
      body.prepend(feedRow(e));
    }
    while (body.children.length > FEED_MAX) body.lastElementChild.remove();
  }

  // ------------------------------------------------------------------ connection handling
  let lastRefresh = 0, fetching = false, again = false, sse = null, sseUp = false, timer = null, downSince = 0;

  function setConn() {
    const node = $("conn");
    node.className = "conn " + (sseUp ? "on" : "off");
    node.textContent = sseUp ? "● live" : "○ reconnecting — refreshing every few seconds";
  }

  async function refresh() {
    if (fetching) { again = true; return; }
    fetching = true;
    try {
      const response = await fetch("/api/live", { cache: "no-store" });
      if (!response.ok) throw new Error(response.status);
      const data = await response.json();
      renderStats(data); renderOrg(data); renderGraph(data);
      if (!seenEvents.size) addEvents(data.feed, true);
      lastRefresh = Date.now();
    } catch (err) {
      sseUp = false; setConn();
    } finally {
      fetching = false;
      if (again) { again = false; setTimeout(refresh, 100); }
    }
  }

  function schedule() {            // many events in a burst -> one refresh
    clearTimeout(timer);
    timer = setTimeout(refresh, 150);
  }

  function connect() {
    if (sse) sse.close();
    sse = new EventSource("/api/events/stream");
    sse.onopen = () => { sseUp = true; downSince = 0; setConn(); refresh(); };
    sse.onerror = () => { sseUp = false; downSince = downSince || Date.now(); setConn(); };   // the browser usually retries by itself
    sse.addEventListener("audit", (message) => {
      addEvents([JSON.parse(message.data)]);
      schedule();
    });
  }

  // Safety net: poll when the stream is down, and slowly even when it is up.
  // After a laptop sleep timers stall; a big gap means "reconnect and redraw now".
  let lastTick = Date.now();
  setInterval(() => {
    const now = Date.now();
    const slept = now - lastTick > 8000;
    lastTick = now;
    // Browsers sometimes give up on a stream for good (e.g. after being offline): open a new one.
    if (slept || (!sseUp && (!sse || sse.readyState === 2 || now - downSince > 6000))) { downSince = now; connect(); }
    if (!sseUp || slept || now - lastRefresh > 5000) refresh();
  }, 1500);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { refresh(); if (!sseUp) connect(); } });

  refresh().then(connect);
})();
