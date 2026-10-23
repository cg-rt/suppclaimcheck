// Architecture map viewer (P0.4). Reads window.ARCHITECTURE_MAP from map-data.js, which
// tools/map_build.py generates from docs/architecture/architecture.yaml. No libraries, no network.
(() => {
  "use strict";

  const map = window.ARCHITECTURE_MAP;
  if (!map) {
    document.getElementById("detail").textContent =
      "map-data.js is missing. Run `uv run python tools/map_build.py`.";
    return;
  }

  const SVG_NS = "http://www.w3.org/2000/svg";
  const system = map.system || {};
  const modules = map.modules || [];
  const steps = map.claim_path || [];
  const entry = system.entry || null;
  const byId = new Map(modules.map((m) => [m.id, m]));
  const internet = new Set((map.networks || []).filter((n) => n.internet).map((n) => n.id));

  // A dependency may name a module or one of its containers.
  const owner = new Map();
  for (const m of modules) {
    owner.set(m.id, m.id);
    for (const c of m.containers || []) owner.set(c, m.id);
  }
  const depsOf = (m) => [...new Set((m.depends_on || []).map((d) => owner.get(d)).filter((d) => d && d !== m.id))];

  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else node.setAttribute(key, value);
    }
    for (const child of children) if (child != null) node.append(child);
    return node;
  };

  const state = { selected: null, step: null };

  // ---- Header ---------------------------------------------------------------

  document.getElementById("summary").textContent = (system.summary || "").trim();
  document.getElementById("meta").textContent =
    `${modules.length} modules · map schema ${map.schema_version} · last updated ${map.updated}`;

  // ---- Diagram --------------------------------------------------------------

  // Order the main row by distance from the entry module along dependencies.
  const rank = new Map();
  const walk = (id, depth) => {
    if ((rank.get(id) ?? -1) >= depth || depth > modules.length) return;
    rank.set(id, depth);
    for (const d of depthDeps(id)) walk(d, depth + 1);
  };
  const depthDeps = (id) => depsOf(byId.get(id)).filter((d) => byId.get(d).kind !== "datastore");
  if (entry && byId.has(entry.module)) walk(entry.module, 0);
  const isStore = (m) => m.kind === "datastore";
  const mainRow = modules
    .filter((m) => !isStore(m))
    .sort((a, b) => (rank.get(a.id) ?? 99) - (rank.get(b.id) ?? 99));
  const storeRow = modules.filter(isStore);

  const diagram = document.getElementById("diagram");
  const wires = document.getElementById("wires");
  const nodes = new Map();

  const moduleNode = (m) => {
    const egress = (m.networks || []).some((n) => internet.has(n));
    const showContainers = JSON.stringify(m.containers || [m.id]) !== JSON.stringify([m.id]);
    const button = el(
      "button",
      { type: "button", class: "node" + (egress ? " is-egress" : ""), "aria-pressed": "false", "data-id": m.id },
      el("span", { class: "node-name", text: m.id }),
      m.role ? el("span", { class: "node-role", text: m.role }) : null,
      showContainers ? el("span", { class: "node-containers", text: m.containers.join(" + ") }) : null,
    );
    button.addEventListener("click", () => select(m.id, true));
    nodes.set(m.id, button);
    return button;
  };

  const extNode = (key, name, detail, many = false) => {
    const node = el(
      "div",
      { class: "node ext" + (many ? " is-many" : ""), "data-id": key },
      el("span", { class: "node-name", text: name }),
      detail ? el("span", { class: "node-role", text: detail }) : null,
    );
    nodes.set(key, node);
    return node;
  };

  const left = el("div", { class: "lane lane-in" });
  if (entry) {
    left.append(extNode("~client", entry.client), extNode("~via", entry.via));
  }

  const host = el(
    "div",
    { class: "host" },
    el("p", { class: "host-label", text: system.host_label || "Host" }),
    el("div", { class: "host-row" }, ...mainRow.map(moduleNode)),
    storeRow.length ? el("div", { class: "host-row stores" }, ...storeRow.map(moduleNode)) : null,
  );

  const right = el("div", { class: "lane lane-out" });
  const outside = [];
  for (const m of modules) {
    const apis = m.external_apis || [];
    if (!apis.length || (entry && m.id === entry.module)) continue;
    const key = `~ext-${m.id}`;
    right.append(extNode(key, "Outside services", apis.join(", "), true));
    outside.push([m.id, key]);
  }

  diagram.append(left, host, right);

  const links = [];
  if (entry) {
    links.push(["~client", "~via"], ["~via", entry.module]);
  }
  for (const m of modules) for (const d of depsOf(m)) links.push([m.id, d]);
  for (const [from, key] of outside) links.push([from, key]);

  // Arrowheads, one plain and one for lit wires.
  const defs = document.createElementNS(SVG_NS, "defs");
  for (const [id, cls] of [["arrow", ""], ["arrow-lit", "lit"]]) {
    const marker = document.createElementNS(SVG_NS, "marker");
    marker.setAttribute("id", id);
    if (cls) marker.setAttribute("class", cls);
    marker.setAttribute("viewBox", "0 0 10 10");
    marker.setAttribute("refX", "9");
    marker.setAttribute("refY", "5");
    marker.setAttribute("markerWidth", "7");
    marker.setAttribute("markerHeight", "7");
    marker.setAttribute("orient", "auto-start-reverse");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
    marker.append(path);
    defs.append(marker);
  }
  wires.append(defs);
  const lines = links.map(([from, to]) => {
    const line = document.createElementNS(SVG_NS, "polyline");
    line.dataset.from = from;
    line.dataset.to = to;
    line.setAttribute("marker-end", "url(#arrow)");
    wires.append(line);
    return line;
  });

  // Where the line from a box's centre towards a point leaves the box, plus a small gap.
  const edgePoint = (box, towards, gap) => {
    const cx = box.x + box.w / 2;
    const cy = box.y + box.h / 2;
    const dx = towards.x - cx;
    const dy = towards.y - cy;
    if (!dx && !dy) return { x: cx, y: cy };
    const scale = Math.min(Math.abs(box.w / 2 / dx) || Infinity, Math.abs(box.h / 2 / dy) || Infinity);
    const len = Math.hypot(dx, dy);
    return { x: cx + dx * scale + (dx / len) * gap, y: cy + dy * scale + (dy / len) * gap };
  };

  // Whether the segment p-q passes through box (Liang-Barsky clipping).
  const crosses = (p, q, box) => {
    let t0 = 0;
    let t1 = 1;
    const dx = q.x - p.x;
    const dy = q.y - p.y;
    const edges = [[-dx, p.x - box.x], [dx, box.x + box.w - p.x], [-dy, p.y - box.y], [dy, box.y + box.h - p.y]];
    for (const [pk, qk] of edges) {
      if (pk === 0) {
        if (qk < 0) return false;
      } else {
        const t = qk / pk;
        if (pk < 0) t0 = Math.max(t0, t);
        else t1 = Math.min(t1, t);
      }
      if (t0 > t1) return false;
    }
    return true;
  };

  const drawWires = () => {
    const origin = diagram.getBoundingClientRect();
    const boxOf = (key) => {
      const r = nodes.get(key).getBoundingClientRect();
      return { x: r.left - origin.left, y: r.top - origin.top, w: r.width, h: r.height };
    };
    const boxes = new Map([...nodes.keys()].map((key) => [key, boxOf(key)]));
    let detours = 0;
    for (const line of lines) {
      const a = boxes.get(line.dataset.from);
      const b = boxes.get(line.dataset.to);
      const ca = { x: a.x + a.w / 2, y: a.y + a.h / 2 };
      const cb = { x: b.x + b.w / 2, y: b.y + b.h / 2 };
      const start = edgePoint(a, cb, 3);
      const end = edgePoint(b, ca, 4);
      const blocked = [...boxes].some(
        ([key, box]) =>
          key !== line.dataset.from &&
          key !== line.dataset.to &&
          crosses(start, end, { x: box.x + 2, y: box.y + 2, w: box.w - 4, h: box.h - 4 }),
      );
      let points = [start, end];
      if (blocked) {
        // Go round the right-hand side instead of through the box in the way.
        const x = Math.max(a.x + a.w, b.x + b.w) + 8 + 6 * detours++;
        const y1 = a.y + a.h / 2;
        const y2 = b.y + b.h / 2;
        points = [{ x: a.x + a.w + 3, y: y1 }, { x, y: y1 }, { x, y: y2 }, { x: b.x + b.w + 4, y: y2 }];
      }
      line.setAttribute("points", points.map((p) => `${p.x},${p.y}`).join(" "));
    }
  };
  new ResizeObserver(drawWires).observe(diagram);
  document.fonts?.ready.then(drawWires);

  // ---- Module detail --------------------------------------------------------

  const detail = document.getElementById("detail");
  const listText = (items) => (items && items.length ? items.join(", ") : "none");

  const fact = (term, value, code = false) => [
    el("dt", { text: term }),
    el("dd", {}, code ? el("code", { text: value }) : value),
  ];

  const renderDetail = (m) => {
    const egress = (m.networks || []).some((n) => internet.has(n));
    const ports = m.ports || {};
    const portText = Object.keys(ports).length
      ? Object.entries(ports).map(([k, v]) => `${k.replace("_", " ")} ${v}`).join(", ")
      : "none";
    const facts = el(
      "dl",
      { class: "facts" },
      ...fact("Status", `${m.status}, version ${m.version}, built in phase ${m.phase_built}`),
      ...fact("Technology", m.technology || "not recorded"),
      ...fact("Containers", listText(m.containers), true),
      ...fact("Networks", `${listText(m.networks)}${egress ? " (can reach the internet)" : " (no internet)"}`),
      ...fact("Depends on", listText(m.depends_on)),
      ...fact("Data", listText(m.data_stores)),
      ...fact("Ports", portText),
      ...fact("Talks to", listText(m.external_apis)),
    );

    const spot = el("div", { class: "spot" }, el("h3", { text: "How to spot it" }));
    spot.append(
      el("p", {}, "In ", el("code", { text: "docker compose ps" }), ", as ", el("code", { text: (m.containers || [m.id]).join(", ") }), "."),
    );
    if (ports.dev_host) {
      spot.append(
        el("p", {}, "On the dev machine, its health check answers at ", el("code", { text: `http://${ports.dev_host}/health` }), "."),
      );
    }
    spot.append(el("p", {}, "Its code lives in ", el("code", { text: `${m.folder}/` }), m.status === "planned" ? " once it is built." : "."));

    detail.replaceChildren(
      el("h2", { id: "detail-title", text: m.name || m.id }),
      el("p", { class: "detail-role", text: m.role || "" }),
      el("h3", { text: "What it does" }),
      el("p", { text: (m.purpose || "").trim() }),
      el("h3", { text: "Why it exists" }),
      el("p", { text: (m.why_it_exists || "").trim() }),
      facts,
      spot,
    );
  };

  // ---- Claim path -----------------------------------------------------------

  const stepList = document.getElementById("steps");
  const stepNow = document.getElementById("step-now");
  const stepCount = document.getElementById("step-count");
  const prev = document.getElementById("prev");
  const next = document.getElementById("next");
  const clear = document.getElementById("clear");

  const stepButtons = steps.map((s, i) => {
    const button = el(
      "button",
      { type: "button" },
      el("span", { class: "num", text: String(s.step) }),
      el("span", { text: s.name }),
    );
    button.addEventListener("click", () => setStep(i));
    stepList.append(el("li", {}, button));
    return button;
  });

  const renderStep = () => {
    const i = state.step;
    prev.disabled = i === null || i === 0;
    next.disabled = i !== null && i === steps.length - 1;
    next.textContent = i === null ? "Start" : "Next";
    clear.hidden = i === null;
    stepButtons.forEach((b, j) => (j === i ? b.setAttribute("aria-current", "step") : b.removeAttribute("aria-current")));

    const lit = new Set(i === null ? [] : steps[i].modules || []);
    if (i !== null && entry && lit.has(entry.module) && i === 0) {
      lit.add("~client");
      lit.add("~via");
    }
    // A lit module's outside services light with it: they are its reason to be on the path.
    for (const [from, key] of outside) if (lit.has(from)) lit.add(key);
    for (const [key, node] of nodes) {
      node.classList.toggle("is-lit", i !== null && lit.has(key));
      node.classList.toggle("is-dim", i !== null && !lit.has(key));
    }
    for (const line of lines) {
      const on = i !== null && lit.has(line.dataset.from) && lit.has(line.dataset.to);
      line.classList.toggle("is-lit", on);
      line.classList.toggle("is-dim", i !== null && !on);
      line.setAttribute("marker-end", on ? "url(#arrow-lit)" : "url(#arrow)");
    }

    if (i === null) {
      stepCount.textContent = `${steps.length} steps`;
      stepNow.replaceChildren(
        el("p", { text: "Walk through what happens to one claim, from submission to result. Each step lights up the modules involved." }),
      );
      return;
    }
    const s = steps[i];
    stepCount.textContent = `Step ${s.step} of ${steps.length}`;
    stepNow.replaceChildren(
      el("p", {}, el("strong", { text: `${s.name}. ` }), s.what),
      el("span", { class: "step-modules", text: `Modules: ${(s.modules || []).join(", ")}` }),
    );
  };

  const setStep = (i) => {
    state.step = i;
    renderStep();
    writeHash();
  };

  prev.addEventListener("click", () => state.step > 0 && setStep(state.step - 1));
  next.addEventListener("click", () => setStep(state.step === null ? 0 : Math.min(state.step + 1, steps.length - 1)));
  clear.addEventListener("click", () => setStep(null));
  document.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, select")) return;
    if (event.key === "Escape" && state.step !== null) setStep(null);
  });

  // ---- Selection and the URL ------------------------------------------------

  const select = (id, fromClick = false) => {
    if (!byId.has(id)) return;
    state.selected = id;
    for (const [key, node] of nodes) {
      if (node.tagName === "BUTTON") node.setAttribute("aria-pressed", String(key === id));
    }
    renderDetail(byId.get(id));
    if (fromClick) writeHash();
  };

  const writeHash = () => {
    const params = new URLSearchParams();
    if (state.selected) params.set("module", state.selected);
    if (state.step !== null) params.set("step", String(steps[state.step].step));
    history.replaceState(null, "", `#${params}`);
  };

  const readHash = () => {
    const params = new URLSearchParams(location.hash.slice(1));
    const stepNo = Number(params.get("step"));
    const index = steps.findIndex((s) => s.step === stepNo);
    state.step = index >= 0 ? index : null;
    select(params.get("module") || (entry ? entry.module : modules[0]?.id));
    renderStep();
  };

  // ---- Merged or deferred ---------------------------------------------------

  document
    .getElementById("deferred")
    .append(...(map.deferred || []).flatMap((d) => [el("dt", { text: d.id }), el("dd", { text: d.note })]));

  window.addEventListener("hashchange", readHash);
  readHash();
  drawWires();
})();
