// The /admin page: the host signs in with agents.yaml's admin_password and sees who is
// connected and where, and can move an AI to a room, suspend or resume it, or remove its
// avatar, and drop a house model (.glb) on the page to build an AI's house in the town.
// Everything goes through /v1/admin, which answers each action with the new status.
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
  $("houses").hidden = !reply.data.can_add_houses;
  render(reply.data);
  clearInterval(timer);
  timer = setInterval(refresh, REFRESH_MS);
  return true;
}

async function refresh() {
  // Don't redraw under someone choosing from a dropdown.
  if (document.activeElement && document.activeElement.closest("#agents select, #houses select")) return;
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
  renderHouses(data);
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

// ----------------------------------------------------------------- houses

let houseFile = null;
let checked = null; // the check result for houseFile with the owner and plot chosen now

function renderHouses(data) {
  if (!data.can_add_houses) return;
  const names = Object.fromEntries(data.agents.map((a) => [a.id, a.name]));
  $("plots").replaceChildren(...data.plots.map((p) => {
    const label = p.owner ? `${names[p.owner] || p.owner}'s house` : p.house ? `${p.house_name} (shared)` : "Empty plot";
    let remove = null;
    if (p.house) {
      remove = el("button", { type: "button", className: "small-btn danger plot-remove", textContent: "Remove" });
      remove.setAttribute("aria-label", `Remove ${label}`);
      remove.onclick = () => removeHouse(p, label, remove);
    }
    return el("li", { className: p.house ? "" : "free" },
      el("div", { className: "plot-head" }, el("span", { textContent: label }), remove),
      el("div", { className: "sub", textContent: `Plot ${p.id}${p.street_name ? ` · ${p.street_name}` : ""}` }));
  }));

  const owner = $("house-owner");
  const ownerWas = owner.value;
  const houseOf = Object.fromEntries(data.plots.filter((p) => p.owner).map((p) => [p.owner, p.id]));
  // Shared houses belong to no AI: their option values are "shared:<id>", and "shared:" is a new one.
  const sharedPlots = data.plots.filter((p) => p.house && !p.owner);
  for (const p of sharedPlots) houseOf[`shared:${p.house}`] = p.id;
  owner.replaceChildren(
    ...data.agents.filter((a) => a.in_agents_file).map((a) => el("option", {
      value: a.id, textContent: houseOf[a.id] ? `${a.name} (rebuild their house)` : a.name })),
    ...sharedPlots.map((p) => el("option", { value: `shared:${p.house}`, textContent: `${p.house_name} (rebuild the shared house)` })),
    el("option", { value: "shared:", textContent: "A new shared house…" }));
  // Default to the first AI without a house: that's usually who the new model is for.
  owner.value = ownerWas || (data.agents.find((a) => a.in_agents_file && !houseOf[a.id]) || {}).id || owner.value;
  $("house-name-label").hidden = owner.value !== "shared:";

  const plot = $("house-plot");
  const plotWas = plot.value;
  const mine = houseOf[owner.value];
  if (mine) {
    plot.replaceChildren(el("option", { value: "", textContent: `Plot ${mine} (their plot)` }));
  } else {
    const free = data.plots.filter((p) => !p.house);
    plot.replaceChildren(el("option", { value: "", textContent: free.length ? "First empty plot" : "No empty plots" }),
      ...free.map((p) => el("option", { value: p.id, textContent: `Plot ${p.id}${p.street_name ? ` · ${p.street_name}` : ""}` })));
    if ([...plot.options].some((o) => o.value === plotWas)) plot.value = plotWas;
  }
  plot.disabled = Boolean(mine);
}

async function removeHouse(p, label, button) {
  const out = p.street_name || "the street";
  if (!confirm(`Take down ${label} on plot ${p.id}?\n\nAnyone inside walks out to ${out}, the house's own things `
    + "go with it, and the plot becomes empty. Its files are kept in houses/removed/ in the world folder.")) return;
  clearHouseMessages();
  button.disabled = true;
  try {
    const { res, data } = await api("DELETE", `/houses/${encodeURIComponent(p.house)}`);
    if (res.status === 401) return signOut();
    if (!res.ok) {
      show("house-error", data.message || data.detail || `The server said ${res.status}.`);
      button.disabled = false;
      return;
    }
    render(data.status);
    show("house-done", `Took down ${label}. Plot ${p.id} is empty now.`);
  } catch {
    show("house-error", "Couldn't reach the server.");
    button.disabled = false;
  }
}

function houseQuery(check) {
  const q = new URLSearchParams();
  if ($("house-plot").value) q.set("plot", $("house-plot").value);
  if (check) q.set("check", "true");
  let id = $("house-owner").value;
  if (id.startsWith("shared:")) {
    q.set("shared", "true");
    id = id.slice("shared:".length);
    if (!id) {
      q.set("name", sharedName());
      id = sharedId();
    }
  }
  return `/houses/${encodeURIComponent(id)}?${q}`;
}

function sharedName() {
  return $("house-name").value.trim();
}

/** A new shared house's id, from its name: "The Old Library" -> "the_old_library". */
function sharedId() {
  return sharedName().toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

async function sendHouse(check) {
  const res = await fetch(`${httpBase}/v1/admin${houseQuery(check)}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${password}`, "Content-Type": "model/gltf-binary" },
    body: houseFile,
  });
  let data = {};
  try { data = await res.json(); } catch { /* not JSON */ }
  if (res.status === 401) signOut();
  return { res, data };
}

function clearHouseMessages() {
  for (const id of ["house-report", "house-error", "house-done"]) show(id, "");
  $("house-warnings").hidden = true;
  $("house-warnings").replaceChildren();
}

async function checkHouse() {
  clearHouseMessages();
  checked = null;
  $("house-send").disabled = true;
  if (!houseFile) return;
  if ($("house-owner").value === "shared:" && !sharedId()) {
    show("house-error", "Give the shared house a name first.");
    return;
  }
  if (!/\.glb$/i.test(houseFile.name) && houseFile.type !== "model/gltf-binary") {
    show("house-error", "That isn't a .glb file. Export the house from Blender as glTF Binary (.glb).");
    return;
  }
  show("house-report", "Checking the model…");
  let reply;
  try {
    reply = await sendHouse(true);
  } catch {
    show("house-report", "");
    show("house-error", "Couldn't reach the server.");
    return;
  }
  show("house-report", "");
  if (!reply.res.ok) {
    show("house-error", reply.data.message || reply.data.detail || `The server said ${reply.res.status}.`);
    return;
  }
  const h = reply.data.house;
  const who = h.shared ? (sharedName() || $("house-owner").selectedOptions[0]?.textContent.replace(/ \(.*\)$/, "") || h.id)
    : $("house-owner").selectedOptions[0]?.textContent.replace(/ \(.*\)$/, "") || h.owner;
  const house = h.shared ? who : `${who}'s house`;
  show("house-report", `${houseFile.name}: ${h.rooms} room${h.rooms === 1 ? "" : "s"} and ${h.objects} `
    + `object${h.objects === 1 ? "" : "s"}, ${h.shared ? `the shared house ${who}` : `for ${who}`} on plot ${h.plot}.`
    + (h.new ? "" : ` This replaces ${h.shared ? "the" : "their"} current house; names, descriptions and object types you edited are kept.`));
  if (h.warnings.length) {
    $("house-warnings").replaceChildren(...h.warnings.map((w) => el("li", { textContent: w })));
    $("house-warnings").hidden = false;
  }
  checked = h;
  $("house-send").textContent = h.new ? `Build ${house}` : `Rebuild ${house}`;
  $("house-send").disabled = false;
}

function chooseHouse(file) {
  houseFile = file;
  $("house-drop-text").textContent = file ? file.name : "Drop a house .glb here, or click to choose one";
  checkHouse();
}

$("house-file").onchange = () => chooseHouse($("house-file").files[0] || null);
$("house-owner").onchange = () => { renderHouses(status); checkHouse(); };
$("house-plot").onchange = () => checkHouse();
let nameTimer = null;
$("house-name").oninput = () => { // re-check once typing pauses (not on blur, which would swallow a click on Build)
  $("house-send").disabled = true;
  clearTimeout(nameTimer);
  nameTimer = setTimeout(checkHouse, 400);
};

// Drop a .glb anywhere on the page once signed in.
let dragDepth = 0;
const draggingFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
document.addEventListener("dragenter", (e) => {
  if (!draggingFiles(e) || $("houses").hidden) return;
  dragDepth += 1;
  document.body.classList.add("dragging");
});
document.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) document.body.classList.remove("dragging");
});
document.addEventListener("dragover", (e) => { if (draggingFiles(e) && !$("houses").hidden) e.preventDefault(); });
document.addEventListener("drop", (e) => {
  if ($("houses").hidden || !e.dataTransfer?.files.length) return;
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove("dragging");
  chooseHouse(e.dataTransfer.files[0]);
  $("houses").scrollIntoView({ behavior: "smooth", block: "start" });
});

$("house-send").onclick = async () => {
  if (!houseFile || !checked) return;
  const button = $("house-send");
  button.disabled = true;
  show("house-error", "");
  show("house-report", "Building…");
  let reply;
  try {
    reply = await sendHouse(false);
  } catch {
    show("house-report", "");
    show("house-error", "Couldn't reach the server.");
    button.disabled = false;
    return;
  }
  show("house-report", "");
  if (!reply.res.ok) {
    show("house-error", reply.data.message || reply.data.detail || `The server said ${reply.res.status}.`);
    button.disabled = false;
    return;
  }
  const h = reply.data.house;
  render(reply.data.status);
  if (h.shared) { // now an existing shared house: select it, so dropping another model rebuilds it
    $("house-owner").value = `shared:${h.id}`;
    $("house-name").value = "";
    renderHouses(status);
  }
  show("house-done", `${h.new ? "Built" : "Rebuilt"} the house on plot ${h.plot}. Everyone watching sees it now.`);
  houseFile = null;
  checked = null;
  $("house-file").value = "";
  $("house-drop-text").textContent = "Drop a house .glb here, or click to choose one";
  $("house-warnings").hidden = true;
  button.textContent = "Build house";
};

// ---------------------------------------------------------------- start

const saved = passwordStore.get();
if (saved) signIn(saved).then((ok) => { if (!ok) passwordStore.clear(); });

// For automated checks.
window.commonroomAdmin = { get status() { return status; } };
