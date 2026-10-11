// The /play page: a person joins with a name and the guest password, and acts through the
// same intents as an AI (see packages/protocol/README.md). The panels and the chat come from
// what this person perceives: their own snapshots and events. The 3D town around them is
// town3d.js in play mode, on a stream the server filters to what this guest can hear; this
// file tells it who joined ("commonroom:joined") and what clicked things can do (actionsFor).
const PROTOCOL = "1.7";
const PACE_MS = 1000; // between steps when walking somewhere far, so others see you pass
const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const server = params.get("server") || `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
const KEY = "commonroom.guest";

const store = {
  get() {
    try { return JSON.parse(sessionStorage.getItem(KEY) || localStorage.getItem(KEY) || "null"); } catch { return null; }
  },
  set(creds, remember) {
    try {
      sessionStorage.setItem(KEY, JSON.stringify(creds));
      if (remember) localStorage.setItem(KEY, JSON.stringify(creds));
    } catch { /* storage blocked: this tab only */ }
  },
  forgetPassword() {
    for (const s of [sessionStorage, localStorage]) {
      try {
        const c = JSON.parse(s.getItem(KEY) || "null");
        if (c) s.setItem(KEY, JSON.stringify({ name: c.name }));
      } catch { /* ignore */ }
    }
  },
};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function button(text, onclick, cls = "btn small") {
  const b = el("button", cls, text);
  b.type = "button";
  b.onclick = onclick;
  b.disabled = walking;
  return b;
}

function colorFor(id) {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `hsl(${h} 55% 48%)`;
}

const words = (s) => String(s).replace(/_/g, " ");
const the = (name) => (/'s /.test(name || "") ? name : `the ${name}`); // "the Café", but "Otto's Kitchen"

// ---------------------------------------------------------------- socket

let ws = null;
let creds = null;
let joined = false;
let stopped = false;
let retry = 1000;
let me = null;
let snap = null;
let places = null; // the `map` result: every room, for "Go to"
let walking = false;
let nextId = 1;
const pending = new Map();

function connect() {
  stopped = false;
  setStatus("connecting…", false);
  ws = new WebSocket(`${server}/v1/ws`);
  ws.onopen = () => ws.send(JSON.stringify({
    type: "register", id: "join", protocol: [PROTOCOL], guest_password: creds.password, name: creds.name,
    client: "commonroom-play",
  }));
  ws.onmessage = (m) => receive(JSON.parse(m.data));
  ws.onclose = () => {
    for (const p of pending.values()) p({ type: "action_failed", reason: "disconnected", message: "Connection lost." });
    pending.clear();
    setStatus("reconnecting…", false);
    if (stopped) return;
    setTimeout(connect, retry);
    retry = Math.min(retry * 2, 15000);
  };
}

function request(type, args = {}) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    return Promise.resolve({ type: "action_failed", reason: "disconnected", message: "Not connected." });
  }
  const id = `p${nextId++}`;
  ws.send(JSON.stringify({ type, id, ...args }));
  return new Promise((resolve) => pending.set(id, resolve));
}

function receive(msg) {
  if (msg.type === "welcome") {
    const first = !joined || me !== msg.entity_id;
    joined = true;
    retry = 1000;
    me = msg.entity_id;
    snap = msg.snapshot;
    $("world-name").textContent = msg.world;
    document.title = `${msg.world} · Join in`;
    $("join").hidden = true;
    $("play").hidden = false;
    $("leave").hidden = false;
    setStatus("here", true);
    window.dispatchEvent(new CustomEvent("commonroom:joined",
      { detail: { name: snap.you.name, password: creds.password, me } }));
    if (first) {
      $("log").replaceChildren();
      for (const ev of snap.recent_events || []) if (ev.entity !== me) logEvent(ev); // not your own arrival
      logLine([["system", `You're in ${the(snap.room.name)}. Say hello!`]]);
    }
    render();
    loadPlaces();
    $("say-text").focus();
  } else if (msg.type === "error") {
    if (msg.reason === "replaced") return stop("You joined from another tab or window, so this one stopped.");
    if (["unauthorized", "bad_name", "name_taken", "protocol_mismatch"].includes(msg.reason)) {
      if (msg.reason === "unauthorized") store.forgetPassword();
      return stop(msg.message);
    }
    logLine([["failed", msg.message || "The server reported an error."]]);
  } else if (msg.type === "ack" || msg.type === "action_failed") {
    const done = pending.get(msg.id);
    pending.delete(msg.id);
    if (done) done(msg);
  } else if (msg.event_id != null || msg.type === "time_tick") {
    onEvent(msg);
  }
}

function stop(message) {
  stopped = true;
  joined = false;
  if (ws) ws.close();
  window.dispatchEvent(new CustomEvent("commonroom:left"));
  $("play").hidden = true;
  $("leave").hidden = true;
  $("play-status").hidden = true;
  showJoin(message);
}

function setStatus(text, ok) {
  const s = $("play-status");
  s.hidden = false;
  s.textContent = text;
  s.className = `status ${ok ? "online" : "offline"}`;
}

// ------------------------------------------------------------------ join

function showJoin(error) {
  $("join").hidden = false;
  $("join-error").hidden = !error;
  $("join-error").textContent = error || "";
  const saved = store.get();
  if (saved?.name) $("join-name").value = saved.name;
  ($("join-name").value ? $("join-password") : $("join-name")).focus();
}

$("join-form").onsubmit = (e) => {
  e.preventDefault();
  creds = { name: $("join-name").value.trim(), password: $("join-password").value };
  store.set(creds, $("join-remember").checked);
  $("join").hidden = true;
  connect();
};

$("leave").onclick = () => {
  if (!confirm("Leave the town? You'll go home in about a minute; join again with the same name before then to carry on.")) return;
  store.forgetPassword();
  stop("");
};

// ---------------------------------------------------------------- events

let refreshTimer = null;
function refreshSoon() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    const r = await request("look");
    if (r.type === "ack" && r.result.snapshot) { snap = r.result.snapshot; render(); }
  }, 150);
}

function onEvent(ev) {
  if (ev.type === "time_tick") {
    if (snap) snap.clock = ev.clock;
    setClock();
    if (ev.phase_changed) logEvent(ev);
    return;
  }
  if (ev.type === "typing") return;
  logEvent(ev);
  if (ev.type === "world_changed" && ev.snapshot) {
    snap = ev.snapshot;
    render();
    loadPlaces();
  } else if (ev.type !== "speech") {
    refreshSoon(); // someone came or went, or something changed: look again
  }
}

function nameOf(id, fallback) {
  if (id === me) return "You";
  const o = snap?.occupants.find((x) => x.id === id);
  return o?.name || fallback || id;
}

function describe(ev) {
  const who = (id, n) => nameOf(id, n);
  switch (ev.type) {
    case "speech": {
      const speaker = who(ev.speaker, ev.speaker_name);
      const mine = ev.speaker === me;
      if (ev.mode === "whisper") {
        const to = ev.to === me ? "you" : who(ev.to, ev.to_name);
        return [["speaker", speaker], ["whisper", ` ${mine ? "whisper" : "whispers"} to ${to}: ${ev.text == null ? "…" : `“${ev.text}”`}`]];
      }
      if (ev.mode === "shout") {
        if (ev.muffled) return [["system", `(muffled, from ${the(ev.from_room_name)}) `], ["speaker", speaker], ["shout", `: “${ev.text}”`]];
        return [["speaker", speaker], ["shout", ` ${mine ? "shout" : "shouts"}: “${ev.text}”`]];
      }
      return [["speaker", speaker], [null, `: ${ev.text}`]];
    }
    case "entered_room":
      if (ev.entity === me) return ev.from ? [["system", `You walk into ${the(snap?.room?.name || "room")}.`]] : null;
      return [["system", ev.from_name ? `${who(ev.entity, ev.entity_name)} came in from ${the(ev.from_name)}.`
        : `${who(ev.entity, ev.entity_name)} is here.`]];
    case "left_room":
      if (ev.entity === me) return null;
      if (ev.gone) return [["system", `${who(ev.entity, ev.entity_name)} went home.`]];
      return [["system", `${who(ev.entity, ev.entity_name)} left for ${the(ev.to_name)}.`]];
    case "position_changed": {
      const near = ev.near;
      return [["system", `${who(ev.entity, ev.entity_name)} ${ev.entity === me ? "move" : "moved"} ${near ? `over to ${near.kind === "entity" ? "" : "the "}${near.name}` : "away"}.`]];
    }
    case "presence":
      if (ev.entity === me) return null;
      return [["system", `${who(ev.entity, ev.entity_name)} ${ev.online ? "woke up" : "dozed off"}.`]];
    case "object_changed":
      return ev.message ? [["system", ev.message]] : null;
    case "time_tick":
      return ev.phase_changed ? [["system", `It is now ${ev.clock.label}.`]] : null;
    case "world_changed":
      return [["system", ev.message || "The town changed."]];
    default:
      return null; // unknown or uninteresting events are ignored on purpose
  }
}

function logEvent(ev) {
  const parts = describe(ev);
  if (!parts) return;
  logLine(parts, ev.world_time, ev.speaker === me ? "mine" : null);
}

function logLine(parts, worldTime = snap?.clock?.minutes, cls = null) {
  const li = el("li", cls);
  if (worldTime != null) {
    const t = Math.floor(worldTime);
    li.append(el("span", "when", `${String(Math.floor((t % 1440) / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`));
  }
  for (const [c, text] of parts) li.append(el("span", c, text));
  const log = $("log");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  log.append(li);
  while (log.children.length > 400) log.firstChild.remove();
  if (atBottom) log.scrollTop = log.scrollHeight;
}

// --------------------------------------------------------------- acting

function cameraOnMe() {
  const view = window.commonroom3d;
  if (view && me && view.following !== me) view.follow(me); // and lift the roof where you go
}

async function act(type, args) {
  if (type === "move") cameraOnMe();
  const r = await request(type, args);
  if (r.type === "action_failed") {
    logLine([["failed", `Couldn't do that: ${r.message}`]]);
    return r;
  }
  if (r.result?.snapshot) snap = r.result.snapshot;
  if (r.result?.reveal) logLine([["private", r.result.reveal]]);
  if (type !== "say" && type !== "shout" && type !== "whisper" && type !== "typing") refreshSoon();
  render();
  return r;
}

async function loadPlaces() {
  const r = await request("map");
  if (r.type !== "ack") return; // an older server without `map`
  places = r.result.map;
  renderPlaces();
}

async function walkTo(place, near = null) {
  const r = await request("map", { to: place });
  if (r.type !== "ack") return logLine([["failed", r.message]]);
  const route = r.result.route;
  if (route == null) return logLine([["failed", `There's no way from here to ${r.result.destination.name}.`]]);
  if (!route.length) {
    if (near) return act("move", { near });
    return logLine([["system", `You're already in ${the(r.result.destination.name)}.`]]);
  }
  walking = true;
  cameraOnMe();
  $("walking").hidden = false;
  $("walking").textContent = `Walking to ${r.result.destination.name}…`;
  render();
  try {
    for (const [i, step] of route.entries()) {
      if (i) await new Promise((ok) => setTimeout(ok, PACE_MS));
      const last = i === route.length - 1;
      const moved = await act("move", near && last ? { to: step.to, near } : { to: step.to });
      if (moved.type !== "ack") break;
    }
  } finally {
    walking = false;
    $("walking").hidden = true;
    render();
  }
}

$("goto").onsubmit = (e) => {
  e.preventDefault();
  if ($("goto-place").value && !walking) walkTo($("goto-place").value);
};

// Speaking, with "typing…" shown to the room while you write.
let typingOn = false;
let typingTimer = null;
function setTyping(on) {
  clearTimeout(typingTimer);
  if (on) typingTimer = setTimeout(() => setTyping(false), 5000);
  if (on === typingOn) return;
  typingOn = on;
  request("typing", { active: on });
}
$("say-text").addEventListener("input", () => setTyping($("say-text").value.trim() !== ""));
$("say-text").addEventListener("blur", () => setTyping(false));

$("say").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("say-text").value.trim();
  if (!text) return;
  const mode = $("say-mode").value;
  $("say-text").value = "";
  setTyping(false);
  const r = mode.startsWith("whisper:")
    ? await act("whisper", { to: mode.slice("whisper:".length), text })
    : await act(mode, { text });
  if (r.type === "action_failed") $("say-text").value = text; // give it back to edit
};

// ---------------------------------------------------------------- render

function setClock() {
  if (!snap?.clock) return;
  $("clock").textContent = snap.clock.label;
  document.body.dataset.phase = snap.clock.phase;
}

function stateText(state) {
  return Object.entries(state || {})
    .map(([k, v]) => (typeof v === "boolean" ? (v ? words(k) : `not ${words(k)}`) : `${words(k)}: ${v}`))
    .join(", ");
}

function poseText(p) {
  return `${p.kind === "lying" ? "lying on" : "sitting on"} the ${p.name}`;
}

function render() {
  if (!snap) return;
  setClock();
  const room = snap.room;
  $("room-name").textContent = room.name;
  const area = places?.areas.find((a) => a.id === room.area);
  $("area-name").textContent = area && area.kind === "house" ? area.name : "";
  $("room-description").textContent = room.description;
  const you = snap.you;
  const bits = [`You are ${you.name}`];
  if (you.pose) bits.push(poseText(you.pose));
  else if (you.near) bits.push(`by ${you.near.kind === "entity" ? "" : "the "}${you.near.name}`);
  $("you").textContent = `${bits.join(", ")}.`;

  $("exits").replaceChildren(...room.exits.map((x) =>
    button(`${words(x.direction)} → ${x.to_name}`, () => act("move", { to: x.to }), "btn")));
  if (!room.exits.length) $("exits").append(el("span", "muted", "None."));
  $("goto").querySelector("button").disabled = walking;

  renderPeople();
  renderThings();
  renderHolding();
  renderComposer();
  renderPlaces();
  window.commonroom3d?.refreshInspect?.();
}

// ------------------------------------------------- clicking things in 3D
// What a thing clicked in the town (town3d.js's info card) lets you do: use it if it's here,
// otherwise walk there.

function findHere(id) {
  for (const o of snap.you.holding || []) if (o.id === id) return { o, held: true };
  for (const o of snap.objects) {
    if (o.id === id) return { o, held: false };
    for (const c of o.contents || []) if (c.id === id) return { o: c, held: false };
  }
  return null;
}

function roomOfObject(id, world, depth = 0) {
  const o = world.objects.get(id);
  if (!o || depth > 8) return null;
  if (o.location.kind === "room") return o.location.id;
  if (o.location.kind === "entity") return world.entities.get(o.location.id)?.room || null;
  return roomOfObject(o.location.id, world, depth + 1);
}

function actionsFor(t) {
  if (!snap || !joined) return [];
  const world = window.commonroom3d?.state;
  const here = snap.room.id;
  const go = (room, label, near = null) => (room && (room !== here || near)
    ? [{ label, primary: true, disabled: walking, run: () => walkTo(room, near) }] : []);
  if (t.kind === "object") {
    const found = findHere(t.id);
    if (!found) return world ? go(roomOfObject(t.id, world), "Walk over there", t.id) : [];
    const { o, held } = found;
    const list = (o.verbs || []).map((v) => ({ label: words(v.verb), disabled: walking,
      run: () => act("interact", { object: o.id, verb: v.verb }) }));
    if (held) list.push({ label: "Put down", disabled: walking, run: () => act("put_down", { object: o.id }) });
    else {
      if (o.portable) list.push({ label: "Pick up", disabled: walking, run: () => act("pick_up", { object: o.id }) });
      if (snap.objects.some((x) => x.id === o.id)) {
        list.push({ label: "Stand by it", disabled: walking, run: () => act("move", { near: o.id }) });
      }
    }
    return list;
  }
  if (t.kind === "person") {
    if (t.id === me) return [];
    const p = snap.occupants.find((x) => x.id === t.id);
    if (p) {
      return [
        { label: "Whisper", run: () => { $("say-mode").value = `whisper:${p.id}`; $("say-text").focus(); } },
        { label: "Stand by them", disabled: walking, run: () => act("move", { near: p.id }) },
      ];
    }
    const e = world?.entities.get(t.id);
    return e ? go(e.room, `Go to ${e.name}`, e.id) : [];
  }
  if (t.kind === "room") return go(t.id, "Walk here");
  if (t.kind === "house") {
    const area = places?.areas.find((a) => a.id === t.id);
    if (t.room) return go(t.room, "Walk to this room");
    return go(area?.entrance, area?.entrance === here ? "" : "Walk to the door");
  }
  return [];
}

function renderPeople() {
  const people = [...snap.occupants].sort((a, b) => a.name.localeCompare(b.name));
  const items = people.map((p) => {
    const li = el("li");
    const title = el("div", "title");
    const dot = el("span", "dot");
    dot.style.background = colorFor(p.id);
    title.append(dot, p.name);
    title.append(el("span", "tag", p.kind === "guest" ? "guest" : "AI"));
    if (!p.online) title.append(el("span", "muted", p.kind === "guest" ? " · away" : " · dozing"));
    li.append(title);
    const status = [];
    if (p.pose) status.push(poseText(p.pose));
    else if (p.near) status.push(`by ${p.near.kind === "entity" ? (p.near.id === me ? "you" : p.near.name) : `the ${p.near.name}`}`);
    if (p.holding?.length) status.push(`holding ${p.holding.map((h) => h.name).join(", ")}`);
    if (p.description) li.append(el("div", "desc", p.description));
    if (status.length) li.append(el("div", "desc", status.join(" · ")));
    const row = el("div", "row");
    row.append(
      button("Whisper", () => { $("say-mode").value = `whisper:${p.id}`; $("say-text").focus(); }),
      button("Stand by", () => act("move", { near: p.id })));
    li.append(row);
    return li;
  });
  $("people-here").replaceChildren(...(items.length ? items : [el("li", "empty", "Nobody else is here.")]));
}

function objectItem(o, { held = false } = {}) {
  const li = el("li");
  const title = el("div", "title", o.name);
  const st = stateText(o.state);
  if (st) title.append(el("span", "muted", ` (${st})`));
  li.append(title);
  if (o.description) li.append(el("div", "desc", o.description));
  const row = el("div", "row");
  for (const v of o.verbs || []) {
    const b = button(words(v.verb), () => act("interact", { object: o.id, verb: v.verb }));
    b.title = v.description + (v.needs ? ` (needs ${words(v.needs)})` : "");
    row.append(b);
  }
  if (!held && o.portable) row.append(button("pick up", () => act("pick_up", { object: o.id })));
  if (!held) row.append(button("stand by", () => act("move", { near: o.id })));
  if (held) {
    row.append(button("put down", () => act("put_down", { object: o.id })));
    const containers = snap.objects.filter((c) => c.container && c.container.open && c.id !== o.id);
    if (containers.length) row.append(pickAndDo("put in…", containers.map((c) => [c.id, c.name]),
      (id) => act("put_down", { object: o.id, into: id })));
    if (snap.occupants.length) row.append(pickAndDo("give to…", snap.occupants.map((p) => [p.id, p.name]),
      (id) => act("give", { object: o.id, to: id })));
  }
  if (row.children.length) li.append(row);
  if (o.container) {
    if (!o.container.open) li.append(el("div", "inside muted", "It's closed."));
    else if (!o.contents?.length) li.append(el("div", "inside muted", "It's empty."));
    else {
      const inside = el("ul", "items inside");
      for (const c of o.contents) {
        const ci = el("li");
        const crow = el("div", "row");
        crow.append(el("span", null, c.name));
        if (c.portable) crow.append(button("take", () => act("pick_up", { object: c.id })));
        ci.append(crow);
        inside.append(ci);
      }
      li.append(inside);
    }
  }
  return li;
}

function pickAndDo(label, options, run) {
  const select = el("select");
  select.append(el("option", null, label));
  select.options[0].value = "";
  for (const [value, text] of options) {
    const opt = el("option", null, text);
    opt.value = value;
    select.append(opt);
  }
  select.disabled = walking;
  select.onchange = () => { if (select.value) run(select.value); };
  return select;
}

function renderThings() {
  const items = snap.objects.map((o) => objectItem(o));
  $("things").replaceChildren(...(items.length ? items : [el("li", "empty", "Nothing here.")]));
}

function renderHolding() {
  const held = snap.you.holding || [];
  $("carry").textContent = snap.max_carry ? `(${held.length} of ${snap.max_carry})` : "";
  const items = held.map((o) => objectItem(o, { held: true }));
  $("holding").replaceChildren(...(items.length ? items : [el("li", "empty", "Nothing.")]));
}

function renderComposer() {
  const select = $("say-mode");
  const was = select.value;
  select.replaceChildren(el("option", null, "Say"), el("option", null, "Shout"));
  select.options[0].value = "say";
  select.options[1].value = "shout";
  for (const p of snap.occupants) {
    const opt = el("option", null, `Whisper to ${p.name}`);
    opt.value = `whisper:${p.id}`;
    select.append(opt);
  }
  select.value = [...select.options].some((o) => o.value === was) ? was : "say";
}

function renderPlaces() {
  if (!places || !snap) return;
  const select = $("goto-place");
  const was = select.value;
  select.replaceChildren(el("option", null, "Somewhere else…"));
  select.options[0].value = "";
  const rooms = Object.fromEntries(places.rooms.map((r) => [r.id, r]));
  for (const area of places.areas) {
    const group = document.createElement("optgroup");
    group.label = area.name;
    for (const id of area.rooms) {
      if (id === snap.room.id) continue;
      const opt = el("option", null, rooms[id].name);
      opt.value = id;
      group.append(opt);
    }
    if (group.children.length) select.append(group);
  }
  select.value = [...select.options].some((o) => o.value === was) ? was : "";
}

// ---------------------------------------------------------------- start

creds = store.get();
if (creds?.name && creds?.password) {
  $("join").hidden = true;
  connect();
} else {
  showJoin("");
}

// For automated checks.
window.commonroomPlay = { get snap() { return snap; }, get me() { return me; }, act, request, walkTo, actionsFor };

$("panel-toggle").onclick = () => {
  const hidden = document.body.classList.toggle("panel-hidden");
  $("panel-toggle").textContent = hidden ? "Show panel" : "Hide panel";
  $("panel-toggle").setAttribute("aria-expanded", String(!hidden));
};
