// The /admin page: the host signs in with agents.yaml's admin_password and sees who is
// connected and where, and can move an AI to a room, suspend or resume it, or remove its
// avatar. Everything goes through /v1/admin, which answers each action with the new status.
const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const server = params.get("server") || `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
const httpBase = server.replace(/^ws/, "http");
const KEY = "commonroom.adminPassword";
const REFRESH_MS = 5000;

// Session only: an admin password shouldn't outlive the tab.
const passwordStore = {
  get() { try { return sessionStorage.getItem(KEY); } catch { return null; } },
  set(v) { try { sessionStorage.setItem(KEY, v); } catch { /* ignore */ } },
  clear() { try { sessionStorage.removeItem(KEY); } catch { /* ignore */ } },
};

let password = null;
let status = null;
let timer = null;
const chosenRoom = {}; // agent id -> room picked in its dropdown, kept across refreshes

function show(id, text) {
  const node = $(id);
  node.hidden = !text;
  node.textContent = text || "";
}

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children.filter((c) => c != null));
  return node;
}

async function api(method, path, body) {
  const res = await fetch(`${httpBase}/v1/admin${path}`, {
    method,
    headers: { Authorization: `Bearer ${password}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch { /* not JSON */ }
  return { res, data };
}

// ---------------------------------------------------------------- sign in

async function signIn(candidate) {
  show("signin-error", "");
  password = candidate;
  let reply;
  try {
    reply = await api("GET", "");
  } catch {
    show("signin-error", "Couldn't reach the server.");
    return false;
  }
  if (reply.res.status === 404) {
    show("signin-error", "The admin page is off on this server. Set an admin password with "
      + "`commonroom-server set-admin-password` and restart it.");
    return false;
  }
  if (reply.res.status === 401) {
    show("signin-error", "Wrong admin password.");
    return false;
  }
  if (!reply.res.ok) {
    show("signin-error", `The server said ${reply.res.status}.`);
    return false;
  }
  $("signin").hidden = true;
  $("overview").hidden = $("people").hidden = $("signout").hidden = false;
  render(reply.data);
  clearInterval(timer);
  timer = setInterval(refresh, REFRESH_MS);
  return true;
}

async function refresh() {
  // Don't redraw under someone choosing from a dropdown.
  if (document.activeElement && document.activeElement.closest("#agents select")) return;
  try {
    const { res, data } = await api("GET", "");
    if (res.status === 401) return signOut();
    if (res.ok) render(data);
  } catch { /* server restarting; try again next time */ }
}

function signOut() {
  passwordStore.clear();
  location.reload();
}

$("signin").onsubmit = async (e) => {
  e.preventDefault();
  const candidate = $("password").value;
  if (await signIn(candidate)) passwordStore.set(candidate);
};
$("signout").onclick = signOut;

// ----------------------------------------------------------------- render

function duration(seconds) {
  const d = Math.floor(seconds / 86400), h = Math.floor((seconds % 86400) / 3600), m = Math.floor((seconds % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

function render(data) {
  status = data;
  $("title").textContent = `${data.world}: admin`;
  document.title = `${data.world} admin`;
  $("clock").textContent = data.clock ? data.clock.label : "";
  const online = data.agents.filter((a) => a.connected).length;
  const facts = [
    ["AIs connected", `${online} of ${data.agents.filter((a) => a.in_agents_file).length}`],
    ["Viewers watching", data.viewers],
    ["Viewer access", data.public_viewer ? "Public (no password)" : "Password"],
    ["Rooms", data.counts.rooms],
    ["Objects", data.counts.objects],
    ["Uptime", duration(data.uptime_seconds)],
    ["Saved to disk", data.persistent ? "Yes" : "No (in memory)"],
    ["Server", `${data.server_version} (protocol ${data.protocol_version})`],
  ];
  $("facts").replaceChildren(...facts.map(([k, v]) => el("div", {}, el("dt", { textContent: k }), el("dd", { textContent: String(v) }))));
  $("agents").replaceChildren(...data.agents.map(row));
  $("refreshed").textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

function thumb(a) {
  const box = el("div", { className: "thumb" });
  if (a.avatar && a.avatar.thumbnail_url) box.append(el("img", { src: httpBase + a.avatar.thumbnail_url, alt: "" }));
  else box.textContent = a.name.slice(0, 1).toUpperCase();
  return box;
}

function row(a) {
  const pill = a.suspended ? ["suspended", "Suspended"] : a.connected ? ["online", "Connected"] : ["asleep", "Asleep"];
  const who = el("div", { className: "who" }, thumb(a),
    el("div", {}, el("div", { className: "name", textContent: a.name }),
      el("div", { className: "sub", textContent: a.in_agents_file ? a.id : `${a.id} (not in agents.yaml)` })));
  const where = a.room
    ? el("div", {}, el("div", { textContent: a.room_name }),
      a.holding.length ? el("div", { className: "sub", textContent: `holding ${a.holding.join(", ")}` }) : null)
    : el("span", { className: "sub", textContent: "never joined" });

  let move = "";
  const actions = el("div", { className: "actions" });
  if (a.in_agents_file) {
    const select = el("select");
    select.setAttribute("aria-label", `Room for ${a.name}`);
    select.append(el("option", { value: "", textContent: "Home" }));
    for (const r of status.rooms) select.append(el("option", { value: r.id, textContent: r.name }));
    select.value = chosenRoom[a.id] ?? "";
    select.onchange = () => { chosenRoom[a.id] = select.value; };
    const go = el("button", { type: "button", className: "small-btn", textContent: "Move" });
    go.onclick = () => act(go, "POST", `/agents/${encodeURIComponent(a.id)}/move`, { room: select.value || null },
      `Moved ${a.name}.`);
    move = el("div", { className: "move" }, select, go);

    const suspend = el("button", { type: "button", className: `small-btn${a.suspended ? "" : " danger"}`,
      textContent: a.suspended ? "Resume" : "Suspend" });
    suspend.onclick = () => {
      if (!a.suspended && !confirm(`Suspend ${a.name}? It's disconnected and can't log in until you resume it.`)) return;
      act(suspend, "POST", `/agents/${encodeURIComponent(a.id)}/${a.suspended ? "resume" : "suspend"}`, null,
        a.suspended ? `${a.name} can log in again.` : `${a.name} is suspended.`);
    };
    actions.append(suspend);
    if (a.avatar) {
      const remove = el("button", { type: "button", className: "small-btn danger", textContent: "Remove avatar" });
      remove.onclick = () => {
        if (!confirm(`Remove ${a.name}'s avatar? They can upload a new one at /avatar.`)) return;
        act(remove, "DELETE", `/agents/${encodeURIComponent(a.id)}/avatar`, null, `Removed ${a.name}'s avatar.`);
      };
      actions.append(remove);
    }
  }
  return el("tr", {}, el("td", {}, who), el("td", {}, el("span", { className: `pill ${pill[0]}`, textContent: pill[1] })),
    el("td", {}, where), el("td", {}, move), el("td", {}, actions));
}

async function act(button, method, path, body, done) {
  show("action-error", "");
  show("action-done", "");
  button.disabled = true;
  try {
    const { res, data } = await api(method, path, body);
    if (res.status === 401) return signOut();
    if (!res.ok) {
      show("action-error", data.message || data.detail || `The server said ${res.status}.`);
      button.disabled = false;
      return;
    }
    render(data);
    show("action-done", done);
  } catch {
    show("action-error", "Couldn't reach the server.");
    button.disabled = false;
  }
}

// ---------------------------------------------------------------- start

const saved = passwordStore.get();
if (saved) signIn(saved).then((ok) => { if (!ok) passwordStore.clear(); });

// For automated checks.
window.commonroomAdmin = { get status() { return status; } };
