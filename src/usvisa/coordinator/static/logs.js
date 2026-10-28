"use strict";
(() => {
  const state = {view: "activity", cursor: null, older: false, generation: 0, cache: new Map(), hovered: null, listed: new Set()};
  const localTime = (value) => new Date(value).toLocaleString("en-US", {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit"});
  const title = (name) => name.replaceAll("_", " ").replace(/^./, c => c.toUpperCase());
  const eventTitle = (row) => row.isTrace ? `Booking attempt · ${title(row.event)}` : title(row.event);
  function el(tag, text, className) {
    const node = document.createElement(tag); if (text !== undefined) node.textContent = text;
    if (className) node.className = className; return node;
  }
  function tone(row) {
    if (row.level === "error" || row.status === "failed") return "error";
    if (row.level === "warning" || row.status === 429 || row.status === "rate_limited") return "warning";
    return row.count > 0 || row.category === "finding" || row.category === "reschedule" ? "slots" : "empty";
  }
  function summary(row) {
    const data = row.details || row;
    if (data.message) return data.message;
    if (data.reason) return String(data.reason).replaceAll("_", " ");
    if (["rate_limited", "session_expired", "failed"].includes(data.status)) return title(data.status);
    if (data.candidates) return `${data.candidates.length} candidate(s) from ${data.available?.length || 0} dates · ${data.booking_disabled ? "Automatic booking is blocked; monitoring continues" : data.candidates.length ? "Within the configured date range" : "No dates match the configured range or exclusions"}`;
    if (data.dates) return data.dates.length ? `${data.dates.length} date(s) · earliest ${data.dates[0]}` : "Portal returned empty array";
    if (data.times) return data.times.length ? `${data.times.length} time(s) · ${data.times.join(", ")}` : "No available appointment times";
    if (row.event === "booking_no_times") return `${data.date} · No available appointment times`;
    if (row.event === "booking_verification") return `${data.date} ${data.time || ""} · ${data.verified ? "Booking verified" : "Booking not verified"}`;
    if (data.outcome) return data.outcome;
    if (data.status !== undefined) return `Status: ${data.status}`;
    if (data.date) return `${data.date}${data.time ? ` at ${data.time}` : ""}`;
    return data.earliest ? `Earliest: ${data.earliest}` : title(row.event);
  }
  async function details(id) {
    if (!state.cache.has(id)) {
      if (state.cache.size > 500) state.cache.delete(state.cache.keys().next().value);
      state.cache.set(id, api(`/logs/${id}`).catch(error => { state.cache.delete(id); throw error; }));
    }
    return state.cache.get(id);
  }
  async function inspect(id) {
    const dialog = $("log-detail");
    $("log-detail-title").textContent = "Loading trace…";
    $("log-detail-meta").textContent = "";
    $("log-trace").replaceChildren();
    if (!dialog.open) dialog.showModal();
    try {
      const data = await details(id), row = data.event;
      $("log-detail-title").textContent = title(row.event);
      $("log-detail-meta").textContent = `${row.nodeName} · ${row.owner} · ${localTime(row.occurredAt)}${row.traceId ? ` · Trace ${row.traceId}` : ""}`;
      for (const entry of data.trace) {
        const step = el("section", undefined, "trace-step");
        const heading = el("div", undefined, "trace-heading");
        heading.append(el("strong", title(entry.event)), el("span", localTime(entry.occurredAt), "muted text-xs"));
        step.append(heading, el("p", `${entry.level.toUpperCase()} · ${entry.category}`, `log-level ${entry.level}`),
                    el("p", summary(entry), "log-summary muted text-sm"));
        const disclosure = el("details", undefined, "trace-data");
        const json = JSON.stringify(entry.details, null, 2);
        disclosure.append(el("summary", "View event data"), el("pre", json));
        disclosure.open = entry.level === "error" || (entry.id === row.id && json.length < 1200);
        step.append(disclosure);
        $("log-trace").append(step);
      }
    } catch (error) { $("log-detail-title").textContent = "Could not load event"; $("log-detail-meta").textContent = error.message; }
  }
  async function tooltip(row, x, y) {
    state.hovered = row.id;
    const tip = $("log-tooltip");
    tip.replaceChildren(el("strong", `${row.nodeName} · ${row.owner}`), el("p", localTime(row.occurredAt)), el("p", eventTitle(row)));
    tip.classList.remove("hidden");
    tip.style.left = `${Math.max(8, Math.min(x + 14, innerWidth - 350))}px`;
    tip.style.top = `${Math.max(8, Math.min(y + 14, innerHeight - 330))}px`;
    try {
      const data = await details(row.id);
      if (state.hovered !== row.id) return;
      tip.append(el("p", summary(data.event)), el("pre", JSON.stringify(data.event.details, null, 2).slice(0, 1600)), el("p", "Select to inspect the full trace", "muted text-xs"));
    } catch (error) { if (state.hovered === row.id) tip.append(el("p", error.message)); }
  }
  function hideTip() { state.hovered = null; $("log-tooltip").classList.add("hidden"); }
  function svg(tag, attributes, text) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function chart(data) {
    const container = $("log-chart"); container.replaceChildren();
    const points = data.points;
    if (!points.length) { container.append(el("p", "No events in this time range. New activity appears automatically.", "empty-state")); return; }
    const ids = [...new Set(points.map(point => point.nodeId))];
    const width = 1000, left = 220, right = 24, top = 30, step = 62, height = top + ids.length * step + 40;
    const start = Date.parse(data.from), end = Date.parse(data.to);
    const root = svg("svg", {viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Events by node over time"});
    for (let index = 0; index <= 5; index++) {
      const x = left + (width - left - right) * index / 5;
      root.append(svg("line", {x1: x, x2: x, y1: 12, y2: height - 32, class: "chart-grid"}));
      const tick = new Date(start + (end - start) * index / 5);
      const options = end - start > 86400000 ? {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"} : {hour: "2-digit", minute: "2-digit"};
      root.append(svg("text", {x, y: height - 10, "text-anchor": index === 0 ? "start" : index === 5 ? "end" : "middle", class: "chart-label"}, tick.toLocaleString("en-US", options)));
    }
    ids.forEach((id, index) => {
      const point = points.find(part => part.nodeId === id), y = top + index * step + step / 2;
      const label = `${point.nodeName} · ${point.owner}`;
      const name = svg("text", {x: 0, y: y - 4, class: "chart-node"}, label.length > 25 ? label.slice(0, 23) + "…" : label);
      name.append(svg("title", {}, label)); root.append(name);
      root.append(svg("line", {x1: left, x2: width - right, y1: y, y2: y, class: "chart-lane"}));
    });
    // SVG paints in DOM order. Keep every non-empty point above empty points,
    // including when zoomed out and their circles overlap on the same lane.
    const emptyLayer = svg("g", {"data-layer": "empty"}), activeLayer = svg("g", {"data-layer": "active"});
    root.append(emptyLayer, activeLayer);
    for (const point of points) {
      const x = left + (Date.parse(point.occurredAt) - start) / (end - start) * (width - left - right);
      const y = top + ids.indexOf(point.nodeId) * step + step / 2;
      const dot = svg("circle", {cx: x, cy: y, r: 4.5, class: `chart-dot ${tone(point)}`, tabindex: 0, role: "button", "aria-label": `${point.nodeName}: ${title(point.event)}, ${localTime(point.occurredAt)}`});
      dot.addEventListener("pointerenter", event => tooltip(point, event.clientX, event.clientY));
      dot.addEventListener("focus", () => { const rect = dot.getBoundingClientRect(); tooltip(point, rect.x, rect.y); });
      dot.addEventListener("pointerleave", hideTip); dot.addEventListener("blur", hideTip);
      dot.addEventListener("click", () => { hideTip(); inspect(point.id); });
      dot.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); inspect(point.id); } });
      (tone(point) === "empty" ? emptyLayer : activeLayer).append(dot);
    }
    container.append(root);
  }
  function list(events, append) {
    const container = $("log-list"); if (!append) { container.replaceChildren(); state.listed.clear(); }
    if (!events.length && !append) { container.append(el("p", state.view === "errors" ? "No serious errors in this time range. Expected retries such as HTTP 429 are excluded." : "No events to display yet.", "empty-state")); return; }
    for (const row of events) {
      const key = row.isTrace ? `${row.nodeId}:${row.traceId}` : row.id;
      if (state.listed.has(key)) continue;
      state.listed.add(key);
      const button = el("button", undefined, "log-row");
      const heading = el("div", undefined, "log-row-heading");
      heading.append(el("span", eventTitle(row), "font-semibold"), el("span", localTime(row.occurredAt), "muted text-xs"));
      const meta = el("div", undefined, "log-row-meta");
      meta.append(el("span", row.level.toUpperCase(), `log-level ${row.level}`), el("span", `${row.nodeName} · ${row.owner}`, "muted text-xs"));
      if (row.isTrace) meta.append(el("span", `${row.stepCount} group step(s) · one trace`, "muted text-xs"));
      button.append(heading, meta, el("p", summary(row), "log-summary muted text-sm"));
      button.onclick = () => inspect(row.id); container.append(button);
    }
  }
  async function load(append = false) {
    const generation = ++state.generation;
    const params = new URLSearchParams({hours: $("log-hours").value, view: state.view});
    if ($("log-node").value) params.set("nodeId", $("log-node").value);
    const selectedType = $("log-event-type").value;
    if (selectedType) params.set("eventType", selectedType);
    if (append && state.cursor) params.set("before", state.cursor);
    $("log-refresh").disabled = true; $("log-more").disabled = true;
    try {
      const data = await api(`/logs?${params}`);
      if (generation !== state.generation) return;
      const selected = $("log-node").value;
      $("log-node").replaceChildren(el("option", "All nodes")); $("log-node").firstChild.value = "";
      for (const node of data.nodes) { const option = el("option", `${node.name} · ${node.owner}`); option.value = node.nodeId; $("log-node").append(option); }
      $("log-node").value = selected;
      const types = [...data.eventTypes];
      if (selectedType && !types.includes(selectedType)) types.push(selectedType);
      $("log-event-type").replaceChildren(el("option", "All event types")); $("log-event-type").firstChild.value = "";
      for (const type of types) { const option = el("option", title(type)); option.value = type; $("log-event-type").append(option); }
      $("log-event-type").value = selectedType;
      $("log-total-label").textContent = state.view === "reschedule" ? "Booking attempts in range" : "Events in range";
      $("log-total").textContent = data.total.toLocaleString("en-US");
      $("log-node-count").textContent = new Set(data.points.map(row => row.nodeId)).size;
      $("log-latest").textContent = data.points.length ? localTime(data.points[data.points.length - 1].occurredAt) : "No events yet";
      $("log-chart-panel").classList.toggle("hidden", state.view === "errors");
      $("log-group-section").classList.toggle("hidden", state.view !== "reschedule");
      $("log-chart-title").textContent = state.view === "reschedule" ? "Reschedule traces" : "Activity timeline";
      $("log-chart-hint").textContent = state.view === "reschedule" ? "One point per booking attempt. Select it to inspect every step. Observations and dry runs are excluded." : "Hover for details. Select a point to inspect its trace.";
      $("log-list-title").textContent = state.view === "errors" ? "Serious errors" : state.view === "reschedule" ? "Booking attempts" : "Event log";
      $("log-chart-note").textContent = `${data.chartLimited ? "Showing the latest 3,000 entries. Narrow the time range for more detail. " : ""}History is kept for 7 days. Times are shown in your browser’s local time.`;
      chart(data); list(data.events, append);
      state.older = append;
      state.cursor = data.nextCursor; $("log-more").classList.toggle("hidden", !state.cursor);
      $("log-updated").textContent = `Updated ${new Date().toLocaleTimeString("en-US")} · live`;
      $("notice").classList.add("hidden");
    } catch (error) { notice(error.message || "Could not load diagnostics.", true); }
    finally { if (generation === state.generation) { $("log-refresh").disabled = false; $("log-more").disabled = false; } }
  }
  async function initializeLogs() {
    const user = await api("/me"); $("log-scope").textContent = user.canViewAllLogs ? "All nodes · admin view" : "Your nodes";
    $("log-node").onchange = () => load(); $("log-hours").onchange = () => load();
    $("log-event-type").onchange = () => load();
    $("log-refresh").onclick = () => load(); $("log-more").onclick = () => load(true);
    $("log-detail-close").onclick = () => $("log-detail").close();
    $("log-detail").addEventListener("click", event => { if (event.target === $("log-detail")) { const rect = event.target.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) event.target.close(); } });
    for (const tab of document.querySelectorAll("[data-log-view]")) tab.onclick = () => {
      state.view = tab.dataset.logView; state.cursor = null;
      $("log-event-type").value = "";
      for (const other of document.querySelectorAll("[data-log-view]")) other.setAttribute("aria-selected", String(other === tab));
      hideTip(); load();
    };
    await load();
    setInterval(() => { if (!document.hidden && !$("log-detail").open && !$("log-refresh").disabled && !state.older) load(); }, 30000);
  }
  initializeLogs().catch(error => notice(error.message || "Could not load diagnostics.", true));
})();
