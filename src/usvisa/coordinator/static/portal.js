"use strict";
let csrf = "";
const $ = (id) => document.getElementById(id);
function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.remove("hidden");
  $("notice").dataset.error = String(error);
}
async function api(path, method = "GET", body, key) {
  const headers = {"X-CSRF-Token": csrf};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (key) headers["Idempotency-Key"] = key;
  const response = await fetch(`/api/v1${path}`, {method, headers, body: body === undefined ? undefined : JSON.stringify(body)});
  const result = response.status === 204 ? {} : await response.json();
  if (!response.ok) {
    if (response.status === 401 && document.body.dataset.page !== "login") location.assign("/login");
    throw new Error(typeof result.detail === "string" ? result.detail : "Check the information you entered.");
  }
  return result;
}
async function busy(button, action) {
  button.disabled = true;
  try { await action(); } catch (error) { notice(error.message || "Could not connect to the server.", true); }
  finally { button.disabled = false; }
}
function showToken(result) {
  if (!result.token) { notice("This request has already completed. If you did not save the token, generate a new one."); return; }
  $("configuration").value = `MODE=coordinated\nSERVER_URL=${result.connection.serverUrl}\nNODE_TOKEN=${result.token}\nNODE_MIN_DATE=YYYY-MM-DD\nUSER_CONSULATE=${document.body.dataset.city}\n`;
  $("token-panel").classList.remove("hidden");
  $("token-panel").scrollIntoView({behavior: "smooth", block: "nearest"});
}
async function loadNodes() {
  const {nodes} = await api("/nodes");
  const container = $("nodes");
  container.replaceChildren();
  if (!nodes.length) {
    const empty = document.createElement("p"); empty.className = "empty-state";
    empty.textContent = "No nodes yet. Create your first node to get its connection configuration.";
    container.append(empty); return;
  }
  for (const node of nodes) {
    const card = document.createElement("article"); card.className = "panel flex flex-wrap items-center justify-between gap-5 p-6";
    const content = document.createElement("div"); content.className = "min-w-0";
    const title = document.createElement("h2"); title.className = "mb-2 text-lg font-semibold break-words"; title.textContent = node.name;
    const detail = document.createElement("p"); detail.className = "muted text-sm";
    const status = node.lifecycle === "revoked" ? "Access revoked" : node.active ? "In schedule" : node.lifecycle === "provisioning" ? "Setup incomplete" : "Waiting for heartbeat";
    detail.textContent = status + (node.lastSeenAt ? ` · ${new Date(node.lastSeenAt * 1000).toLocaleString("en-US")}` : "");
    const id = document.createElement("p"); id.className = "muted mt-2 break-all font-mono text-xs"; id.textContent = node.nodeId;
    content.append(title, detail, id); card.append(content);
    if (node.lifecycle !== "revoked") {
      const actions = document.createElement("div"); actions.className = "flex flex-wrap gap-2";
      const rotate = document.createElement("button"); rotate.className = "button secondary text-sm"; rotate.textContent = "New token";
      rotate.onclick = () => busy(rotate, async () => { showToken(await api(`/nodes/${node.nodeId}/token`, "POST", undefined, crypto.randomUUID())); await loadNodes(); });
      const revoke = document.createElement("button"); revoke.className = "button danger text-sm"; revoke.textContent = "Revoke access";
      revoke.onclick = () => { if (confirm(`Revoke access for node “${node.name}”?`)) busy(revoke, async () => { await api(`/nodes/${node.nodeId}/revoke`, "POST"); await loadNodes(); notice("Access revoked."); }); };
      actions.append(rotate, revoke); card.append(actions);
    }
    container.append(card);
  }
}
async function initialize() {
  if (document.body.dataset.page === "login") {
    csrf = (await api("/csrf")).csrfToken;
    $("login").onsubmit = (event) => { event.preventDefault(); busy(event.submitter, async () => {
      const form = new FormData(event.target);
      await api("/session", "POST", {username: form.get("username"), password: form.get("password")});
      location.assign("/");
    }); };
  } else {
    const user = await api("/me"); csrf = user.csrfToken; $("account").textContent = user.username;
    await loadNodes();
    $("create").onsubmit = (event) => { event.preventDefault(); busy(event.submitter, async () => {
      // Retain the key if provisioning failed: the next submit resumes the same node.
      const key = event.target.dataset.key || crypto.randomUUID(); event.target.dataset.key = key;
      const result = await api("/nodes", "POST", {name: $("name").value}, key);
      delete event.target.dataset.key; event.target.reset(); showToken(result); await loadNodes();
    }); };
    $("logout").onclick = (event) => busy(event.target, async () => { await api("/session", "DELETE"); location.assign("/login"); });
    setInterval(() => loadNodes().catch(() => {}), 30000);
  }
  $("copy").onclick = (event) => busy(event.target, async () => { await navigator.clipboard.writeText($("configuration").value); notice("Configuration copied."); });
  $("hide-token").onclick = () => { $("configuration").value = ""; $("token-panel").classList.add("hidden"); };
}
initialize().catch((error) => notice(error.message, true));
