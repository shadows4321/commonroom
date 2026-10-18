// Commonroom replay (protocol 1.8), shared by the 2D map and the 3D view: pick a moment in
// the past and play the world from there at any speed. The server rebuilds the world_state
// at that moment (/v1/api/history/state) and hands over every recorded change after it
// (/v1/api/history/frames); the page applies them exactly as it would the live stream.
//
//   const replay = commonroomReplay.create({ httpBase, password: () => …, load(state, {seek}),
//                                            update(update), live() });
//   replay.ready()   once the live world has loaded: shows the Rewind button
//   replay.active    true while showing the past: the page ignores the live socket
//   replay.timeScale how much faster than real time things happen on screen (1 when live)
(() => {
  "use strict";

  const SPEEDS = [1, 3, 10, 30, 60, 300];
  const QUIET_GAP = 4; // seconds on screen: longer pauses are skipped when "Skip quiet" is on
  const BUFFER_SECONDS = 120; // keep this much playback (on screen) fetched ahead
  const DEFAULT_BACK = 8 * 3600; // Rewind starts eight hours ago: overnight
  const SPEED_KEY = "commonroom.replaySpeed";
  const SKIP_KEY = "commonroom.replaySkipQuiet";

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };
  const saved = (key, fallback) => { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } };
  const save = (key, value) => { try { localStorage.setItem(key, value); } catch { /* fine */ } };
  const pad = (n) => String(n).padStart(2, "0");
  const localInput = (t) => {
    const d = new Date(t * 1000);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  const label = (t) => new Date(t * 1000).toLocaleString(undefined,
    { weekday: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  // A frame that shows something happening, not just the clock moving on.
  const busy = (f) => f.world_state || f.update.events.length || f.update.entities.length
    || f.update.objects.length || (f.update.gone || []).length;

  function create(hooks) {
    const r = {
      active: false, playing: false, cursor: 0, info: null,
      speed: Number(saved(SPEED_KEY, 3)) || 3, skipQuiet: saved(SKIP_KEY, "1") === "1",
      get timeScale() { return this.active && this.playing ? this.speed : 1; },
    };
    let frames = [];
    let next = 0; // id of the last recorded change fetched
    let more = false;
    let fetching = null;
    let generation = 0; // bumped on every seek, so answers to an older one are dropped
    let last = performance.now();

    // ------------------------------------------------------------------ UI

    const button = el("button", "link-button", "Rewind");
    button.type = "button";
    button.hidden = true;
    button.title = "Watch what happened earlier";
    const status = document.getElementById("status");
    if (status) status.before(button);
    else document.querySelector("header").append(button);

    const bar = el("div", "replay-bar");
    bar.hidden = true;
    bar.setAttribute("role", "region");
    bar.setAttribute("aria-label", "Replay");
    const play = el("button", "replay-play", "▶");
    play.type = "button";
    play.setAttribute("aria-label", "Play");
    const speed = el("select", "replay-speed");
    speed.setAttribute("aria-label", "Speed");
    for (const s of SPEEDS) speed.add(new Option(`${s}×`, s));
    if (!SPEEDS.includes(r.speed)) r.speed = 3;
    speed.value = r.speed;
    const when = el("input", "replay-when");
    when.type = "datetime-local";
    when.setAttribute("aria-label", "Go to a time");
    const track = el("div", "replay-track");
    const activity = el("canvas", "replay-activity");
    activity.setAttribute("aria-hidden", "true");
    const scrub = el("input", "replay-scrub");
    scrub.type = "range";
    scrub.step = "1";
    scrub.setAttribute("aria-label", "Time");
    track.append(activity, scrub);
    const now = el("span", "replay-now");
    const skip = el("label", "replay-skip");
    const skipBox = el("input");
    skipBox.type = "checkbox";
    skipBox.checked = r.skipQuiet;
    skip.append(skipBox, " Skip quiet");
    skip.title = "Jump over stretches where nothing happens";
    const live = el("button", "replay-live", "Back to live");
    live.type = "button";
    const message = el("span", "replay-message");
    bar.append(play, speed, when, track, now, skip, live, message);
    document.body.append(bar);

    button.onclick = () => (bar.hidden ? open() : goLive());
    live.onclick = () => goLive();
    play.onclick = () => setPlaying(!r.playing);
    speed.onchange = () => { r.speed = Number(speed.value); save(SPEED_KEY, r.speed); render(); };
    skipBox.onchange = () => { r.skipQuiet = skipBox.checked; save(SKIP_KEY, r.skipQuiet ? "1" : "0"); };
    when.onchange = () => { const t = new Date(when.value).getTime() / 1000; if (t) seek(t); };
    scrub.oninput = () => { now.textContent = label(Number(scrub.value)); };
    scrub.onchange = () => seek(Number(scrub.value));
    window.addEventListener("resize", drawActivity);
    document.addEventListener("keydown", (e) => {
      if (bar.hidden || e.target.closest("input, select, textarea")) return;
      if (e.key === " ") { e.preventDefault(); setPlaying(!r.playing); }
    });

    function say(text) { message.textContent = text || ""; }

    function setPlaying(on) {
      r.playing = on && r.active;
      play.textContent = r.playing ? "❚❚" : "▶";
      play.setAttribute("aria-label", r.playing ? "Pause" : "Play");
      last = performance.now();
    }

    function drawActivity() {
      const info = r.info;
      const w = activity.clientWidth, h = activity.clientHeight;
      if (!info || !w || !h) return;
      const dpr = window.devicePixelRatio || 1;
      activity.width = w * dpr;
      activity.height = h * dpr;
      const ctx = activity.getContext("2d");
      ctx.scale(dpr, dpr);
      ctx.fillStyle = getComputedStyle(bar).getPropertyValue("--accent").trim() || "#3f6fd8";
      const max = Math.max(1, ...info.buckets);
      const bw = w / info.buckets.length;
      info.buckets.forEach((n, i) => {
        if (!n) return;
        const bh = Math.max(2, (Math.sqrt(n) / Math.sqrt(max)) * h);
        ctx.globalAlpha = 0.45;
        ctx.fillRect(i * bw, h - bh, Math.max(1, bw - 0.5), bh);
      });
    }

    function render() {
      if (bar.hidden) return;
      const end = Date.now() / 1000;
      scrub.max = Math.floor(end);
      if (document.activeElement !== scrub) {
        scrub.value = Math.floor(r.cursor);
        now.textContent = `${label(r.cursor)}${fetching && r.playing && !frames.length ? " · loading…" : ""}`;
      }
      if (document.activeElement !== when) when.value = localInput(r.cursor);
      when.max = localInput(end);
    }

    // ------------------------------------------------------------ fetching

    async function api(path) {
      const pw = hooks.password();
      const res = await fetch(`${hooks.httpBase}/v1/api/history${path}`,
        { headers: pw ? { Authorization: `Bearer ${pw}` } : {} });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.message || `The server said ${res.status}.`);
      }
      return res.json();
    }

    async function open() {
      say("");
      try {
        r.info = await api("?buckets=240");
      } catch (err) {
        bar.hidden = false;
        say(`Can't replay: ${err.message}`);
        return;
      }
      if (!r.info.available) {
        bar.hidden = false;
        say("Nothing recorded yet: replay starts from when the server was updated.");
        return;
      }
      bar.hidden = false;
      button.textContent = "Live";
      document.body.classList.add("replaying");
      scrub.min = Math.floor(r.info.start);
      when.min = localInput(r.info.start);
      drawActivity();
      await seek(Math.max(r.info.start, r.info.end - DEFAULT_BACK));
    }

    async function seek(t) {
      const gen = ++generation;
      try {
        const state = await api(`/state?at=${t}`);
        if (gen !== generation) return;
        r.active = true;
        if (status) { status.textContent = "replay"; status.className = "status replay"; }
        r.cursor = state.at;
        frames = [];
        next = state.history_id;
        more = true;
        fetching = null;
        hooks.load(state, { seek: true });
        say("");
        render();
        fill();
      } catch (err) {
        if (gen === generation) say(err.message);
      }
    }

    function fill() {
      if (fetching || !more) return;
      const gen = generation;
      fetching = api(`/frames?after=${next}&limit=500`).then((data) => {
        if (gen !== generation) return;
        frames.push(...data.frames);
        if (data.frames.length) next = data.frames[data.frames.length - 1].id;
        more = data.more;
      }).catch((err) => { if (gen === generation) { say(err.message); setPlaying(false); } })
        .finally(() => { if (gen === generation) fetching = null; });
    }

    function goLive() {
      generation++;
      setPlaying(false);
      bar.hidden = true;
      button.textContent = "Rewind";
      document.body.classList.remove("replaying");
      frames = [];
      if (r.active) { r.active = false; hooks.live(); }
    }

    // --------------------------------------------------------------- playing

    function step() {
      const t = performance.now();
      const dt = (t - last) / 1000;
      last = t;
      if (!r.active) return;
      if (r.playing) {
        let target = r.cursor + dt * r.speed;
        if (r.skipQuiet) {
          const ahead = frames.find((f) => f.t > r.cursor && busy(f));
          if (ahead && ahead.t - target > QUIET_GAP * r.speed) target = ahead.t - r.speed * 0.5;
          else if (!ahead && !more && !fetching) target = Date.now() / 1000; // nothing more happened: catch up
        }
        // Don't run past what has been fetched: wait for it instead.
        if (more || fetching) target = Math.min(target, frames.length ? frames[frames.length - 1].t : r.cursor);
        target = Math.max(target, r.cursor);
        while (frames.length && frames[0].t <= target) {
          const f = frames.shift();
          if (f.world_state) hooks.load(f.world_state, { seek: false });
          else hooks.update(f.update);
        }
        r.cursor = target;
        if (!more && !fetching && !frames.length && r.cursor >= Date.now() / 1000 - 1) {
          goLive(); // caught up with the present
          return;
        }
      }
      if (more && (frames.length < 100 || frames[frames.length - 1].t - r.cursor < BUFFER_SECONDS * r.speed)) fill();
      render();
    }
    setInterval(step, 100);

    r.ready = () => { button.hidden = false; };
    r.close = goLive;
    return r;
  }

  window.commonroomReplay = { create };
})();
