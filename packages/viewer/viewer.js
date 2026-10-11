// Commonroom 2D viewer. Renders the world state the server streams over
// /v1/ws/viewer; it never decides anything itself.
(() => {
  "use strict";

  const PROTOCOL_MAJOR = 1; // this viewer speaks protocol 1.x
  const params = new URLSearchParams(location.search);
  const server = params.get("server") || `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
  const httpBase = server.replace(/^ws/, "http"); // avatar URLs are relative to the server
  const wsUrl = `${server}/v${PROTOCOL_MAJOR}/ws/viewer`;

  // The viewer password is sent as a message after connecting (never in the URL), and kept
  // for this tab, or on this device if "remember" was ticked.
  const PASSWORD_KEY = "commonroom.viewerPassword";
  const store = {
    get() {
      try { return sessionStorage.getItem(PASSWORD_KEY) || localStorage.getItem(PASSWORD_KEY); } catch { return null; }
    },
    set(value, remember) {
      try {
        sessionStorage.setItem(PASSWORD_KEY, value);
        if (remember) localStorage.setItem(PASSWORD_KEY, value);
      } catch { /* storage blocked: the password just lasts until the page closes */ }
    },
    clear() {
      try { sessionStorage.removeItem(PASSWORD_KEY); localStorage.removeItem(PASSWORD_KEY); } catch { /* ignore */ }
    },
  };
  let password = store.get();
  if (params.has("token")) {
    // Old-style ?token= links: take the password, then remove it from the address bar.
    password = params.get("token");
    store.set(password, false);
    params.delete("token");
    const rest = params.toString();
    history.replaceState(null, "", `${location.pathname}${rest ? `?${rest}` : ""}`);
  }

  const state = { rooms: new Map(), entities: new Map(), objects: new Map(), areas: new Map(), typing: new Set(), clock: null };
  let selectedRoom = "";

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };

  function colorFor(id) {
    let h = 0;
    for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) % 360;
    return `hsl(${h} 55% 48%)`;
  }

  // ------------------------------------------------------------------ socket

  let retry = 1000;
  let waitingForLogin = false;
  let usedPassword = false;

  function connect() {
    const ws = new WebSocket(wsUrl);
    ws.onopen = () => { retry = 1000; };
    ws.onclose = () => {
      setStatus(false);
      if (waitingForLogin) return; // reconnect when the password form is submitted
      setTimeout(connect, retry);
      retry = Math.min(retry * 2, 15000);
    };
    ws.onmessage = (msg) => {
      const data = JSON.parse(msg.data);
      if (data.type === "auth_required") {
        usedPassword = true;
        if (password) ws.send(JSON.stringify({ type: "viewer_auth", password }));
        else { showLogin(""); ws.close(); }
      } else if (data.type === "world_state") {
        setStatus(true);
        $("logout").hidden = !usedPassword;
        loadState(data);
      } else if (data.type === "update") {
        applyUpdate(data);
      } else if (data.type === "error" && data.reason === "unauthorized") {
        password = null;
        store.clear();
        showLogin("That password didn't work.");
      } else if (data.type === "error") {
        addSystemLine(`Server: ${data.message}`);
      }
    };
  }

  function setStatus(online) {
    const s = $("status");
    s.textContent = online ? "live" : waitingForLogin ? "locked" : "reconnecting…";
    s.className = `status ${online ? "online" : "offline"}`;
  }

  function showLogin(error) {
    waitingForLogin = true;
    $("login").hidden = false;
    $("login-error").hidden = !error;
    $("login-error").textContent = error;
    $("login-password").value = "";
    $("login-password").focus();
    setStatus(false);
  }

  $("login-form").onsubmit = (e) => {
    e.preventDefault();
    password = $("login-password").value;
    store.set(password, $("login-remember").checked);
    $("login").hidden = true;
    waitingForLogin = false;
    retry = 1000;
    connect();
  };

  $("logout").onclick = () => {
    store.clear();
    location.reload();
  };

  // ------------------------------------------------------------------- state

  function loadState(data) {
    const major = parseInt(String(data.protocol).split(".")[0], 10);
    if (major !== PROTOCOL_MAJOR) {
      addSystemLine(`Warning: server speaks protocol ${data.protocol}; this viewer expects ${PROTOCOL_MAJOR}.x.`);
    }
    $("world-name").textContent = data.name;
    document.title = `${data.name} · Commonroom`;
    state.rooms = new Map(data.rooms.map((r) => [r.id, r]));
    state.areas = new Map((data.areas || []).map((a) => [a.id, a])); // protocol 1.2+: the town and each house
    state.entities = new Map(data.entities.map((e) => [e.id, e]));
    state.objects = new Map(data.objects.map((o) => [o.id, o]));
    state.typing.clear();
    setClock(data.clock);
    const filter = $("room-filter");
    filter.length = 1;
    for (const r of state.rooms.values()) filter.add(new Option(r.name, r.id));
    filter.value = selectedRoom;
    $("log").replaceChildren();
    for (const ev of data.recent_events) addEvent(ev);
    renderMap();
  }

  function applyUpdate(data) {
    for (const e of data.entities) state.entities.set(e.id, e);
    for (const id of data.gone || []) { state.entities.delete(id); state.typing.delete(id); } // 1.7: guests who left
    for (const o of data.objects) state.objects.set(o.id, o);
    for (const ev of data.events) {
      if (ev.type === "typing") {
        ev.active ? state.typing.add(ev.entity) : state.typing.delete(ev.entity);
      } else if (ev.type === "speech") {
        state.typing.delete(ev.speaker);
      }
      addEvent(ev);
    }
    if (data.clock) setClock(data.clock);
    renderMap();
  }

  function setClock(clock) {
    state.clock = clock;
    $("clock").textContent = clock.label;
    document.body.dataset.phase = clock.phase;
  }

  // --------------------------------------------------------------------- map

  function renderMap() {
    // One section per area (the town, then each house), each laid out by its rooms' map positions.
    const byArea = new Map();
    for (const room of state.rooms.values()) {
      const area = room.area || "town";
      if (!byArea.has(area)) byArea.set(area, []);
      byArea.get(area).push(room);
    }
    const map = $("map");
    const sections = [...byArea.entries()].map(([areaId, rooms]) => {
      // Compact the coordinates, so gaps in a sparse layout don't become empty columns.
      const xs = [...new Set(rooms.map((r) => r.map[0]))].sort((a, b) => a - b);
      const ys = [...new Set(rooms.map((r) => r.map[1]))].sort((a, b) => a - b);
      const grid = el("div", "area-grid");
      grid.style.gridTemplateColumns = `repeat(${xs.length}, minmax(180px, 1fr))`;
      grid.append(...rooms.map((room) => renderRoom(room, xs, ys)));
      if (byArea.size === 1) return grid;
      const section = el("section", "area");
      section.append(el("h2", "area-title", state.areas.get(areaId)?.name || areaId), grid);
      return section;
    });
    map.replaceChildren(...sections);
  }

  function renderRoom(room, xs, ys) {
    const card = el("article", `room${room.id === selectedRoom ? " selected" : ""}`);
    card.style.gridColumn = String(xs.indexOf(room.map[0]) + 1);
    card.style.gridRow = String(ys.indexOf(room.map[1]) + 1);
    card.title = room.description;
    card.onclick = () => selectRoom(room.id === selectedRoom ? "" : room.id);

    const title = el("h3", null, room.name);
    title.append(el("span", "exits", room.exits.map((x) => x.direction).join(" · ")));
    card.append(title);

    const people = el("div", "people");
    for (const e of state.entities.values()) {
      if (e.room !== room.id) continue;
      const chip = el("span", `person${e.online ? "" : " offline"}`);
      const avatar = el("span", "avatar", e.name.slice(0, 1).toUpperCase());
      avatar.style.background = colorFor(e.id);
      if (e.avatar && e.avatar.thumbnail_url) {
        const img = el("img");
        img.src = httpBase + e.avatar.thumbnail_url;
        img.alt = "";
        img.loading = "lazy";
        img.onerror = () => img.remove(); // fall back to the initial
        avatar.append(img);
      }
      const name = el("span", state.typing.has(e.id) ? "typing" : null, e.kind === "guest" ? `${e.name} (guest)` : e.name);
      chip.append(avatar, name);
      const bits = [];
      if (e.pose) bits.push(`${e.pose.kind === "lying" ? "🛏️ lying on" : "🪑 sitting on"} the ${e.pose.name}`); // protocol 1.3
      else if (e.near) bits.push(`by ${e.near.name}`);
      if (e.holding.length) bits.push(`🤲 ${e.holding.map((h) => h.name).join(", ")}`);
      if (!e.online) bits.push("dozing");
      if (bits.length) chip.append(el("span", "meta", bits.join(" · ")));
      chip.title = [e.description || e.name, avatarCredit(e.avatar)].filter(Boolean).join("\n");
      people.append(chip);
    }
    card.append(people);

    const things = el("ul", "things");
    for (const o of state.objects.values()) {
      if (o.location.kind === "room" && o.location.id === room.id) things.append(renderObject(o));
    }
    card.append(things);
    return card;
  }

  function avatarCredit(avatar) {
    if (!avatar) return "";
    const m = avatar.meta || {};
    const parts = [`Avatar: ${m.title || avatar.kind.toUpperCase()}`];
    if (m.authors && m.authors.length) parts.push(`by ${m.authors.join(", ")}`);
    if (m.license) parts.push(`(${m.license})`);
    return parts.join(" ");
  }

  function renderObject(o) {
    const li = el("li", null, o.name);
    li.title = o.description;
    const flags = Object.entries(o.state)
      .filter(([, v]) => typeof v === "boolean" ? v : true)
      .map(([k, v]) => (typeof v === "boolean" ? k : `${k}: ${v}`));
    if (flags.length) li.append(" ", el("span", "state", `(${flags.join(", ")})`));
    if (o.container && o.container.open) {
      const inner = [...state.objects.values()].filter((c) => c.location.kind === "container" && c.location.id === o.id);
      if (inner.length) {
        const nested = el("ul", "things nested");
        inner.forEach((c) => nested.append(renderObject(c)));
        li.append(nested);
      }
    }
    return li;
  }

  function selectRoom(id) {
    selectedRoom = id;
    $("room-filter").value = id;
    for (const li of $("log").children) li.hidden = id ? li.dataset.room !== id : !!li.dataset.echo;
    renderMap();
  }
  $("room-filter").onchange = (e) => selectRoom(e.target.value);

  // --------------------------------------------------------------------- log

  function describe(ev) {
    const name = (id, fallback) => state.entities.get(id)?.name || fallback || id;
    switch (ev.type) {
      case "speech": {
        const who = name(ev.speaker, ev.speaker_name);
        if (ev.mode === "whisper") {
          const text = ev.text == null ? "…" : `“${ev.text}”`;
          return [["speaker", who], ["whisper", ` whispers to ${name(ev.to, ev.to_name)}: ${text}`]];
        }
        if (ev.mode === "shout") {
          if (ev.muffled) return [["system", `(muffled, from the ${ev.from_room_name}) `], ["speaker", who], ["shout", `: “${ev.text}”`]];
          return [["speaker", who], ["shout", ` shouts: “${ev.text}”`]];
        }
        return [["speaker", who], [null, `: ${ev.text}`]];
      }
      case "entered_room":
        return [["system", `${name(ev.entity, ev.entity_name)} entered${ev.from_name ? ` from the ${ev.from_name}` : ""}.`]];
      case "left_room":
        if (ev.gone) return [["system", `${name(ev.entity, ev.entity_name)} went home.`]]; // 1.7: a guest left
        return [["system", `${name(ev.entity, ev.entity_name)} left for the ${ev.to_name}.`]];
      case "position_changed":
        return [["system", `${name(ev.entity, ev.entity_name)} moved ${ev.near ? `over to ${ev.near.kind === "entity" ? "" : "the "}${ev.near.name}` : "away"}.`]];
      case "presence":
        return [["system", `${name(ev.entity, ev.entity_name)} ${ev.online ? "woke up" : "dozed off"}.`]];
      case "object_changed":
        return ev.message ? [["system", ev.message]] : null;
      case "time_tick":
        return ev.phase_changed ? [["system", `It is now ${ev.clock.label}.`]] : null;
      case "world_changed": // 1.6: a house was built; a fresh world_state follows
        return [["system", ev.message || "The town changed."]];
      default:
        return null; // unknown event types from newer servers are ignored on purpose
    }
  }

  function addEvent(ev) {
    const parts = describe(ev);
    if (!parts) return;
    const li = el("li");
    li.dataset.room = ev.room || "";
    const hh = Math.floor((ev.world_time % 1440) / 60), mm = ev.world_time % 60;
    li.append(el("span", "when", `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`));
    for (const [cls, text] of parts) li.append(el("span", cls, text));
    if (ev.room && state.rooms.has(ev.room)) li.append(" ", el("span", "where", `· ${state.rooms.get(ev.room).name}`));
    // A muffled shout is the echo of one already logged in its own room; show it only when filtering.
    if (ev.type === "speech" && ev.muffled) li.dataset.echo = "1";
    li.hidden = selectedRoom ? li.dataset.room !== selectedRoom : !!li.dataset.echo;
    const log = $("log");
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    log.append(li);
    while (log.children.length > 500) log.firstChild.remove();
    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  function addSystemLine(text) {
    const li = el("li");
    li.append(el("span", "system", text));
    $("log").append(li);
  }

  connect();
})();
