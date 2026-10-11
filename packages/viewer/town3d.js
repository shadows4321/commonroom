// Commonroom 3D viewer. Renders the same /v1/ws/viewer stream as the 2D map, placing
// characters using the markers in the town and house models (see docs/3d-spec.md).
// It never decides anything itself: the server is the source of truth.
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";
import { VRMLoaderPlugin, VRMUtils } from "@pixiv/three-vrm";
import { createMinimap } from "./minimap.js";

const PROTOCOL_MAJOR = 1;
const WALK_SPEED = 2.2; // m/s
const MAX_WALK_SECONDS = 5; // long walks (down a 40 m street) are sped up to fit
const BUBBLE_SECONDS = 7;
const POSE_SECONDS = 0.8; // sitting down, lying down or getting up

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};
const colorFor = (id) => {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return new THREE.Color(`hsl(${h}, 55%, 52%)`);
};

// ------------------------------------------------------------ connection

const params = new URLSearchParams(location.search);
const server = params.get("server") || `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
const httpBase = server.replace(/^ws/, "http");
const PASSWORD_KEY = "commonroom.viewerPassword";
const store = {
  get() { try { return sessionStorage.getItem(PASSWORD_KEY) || localStorage.getItem(PASSWORD_KEY); } catch { return null; } },
  set(v, remember) { try { sessionStorage.setItem(PASSWORD_KEY, v); if (remember) localStorage.setItem(PASSWORD_KEY, v); } catch { /* ignore */ } },
  clear() { try { sessionStorage.removeItem(PASSWORD_KEY); localStorage.removeItem(PASSWORD_KEY); } catch { /* ignore */ } },
};
let password = store.get();
let usedPassword = false;
let waitingForLogin = false;
let retry = 1000;

function connect() {
  const ws = new WebSocket(`${server}/v${PROTOCOL_MAJOR}/ws/viewer`);
  ws.onopen = () => { retry = 1000; };
  ws.onclose = () => {
    setStatus(false);
    if (waitingForLogin) return;
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
    }
  };
}

function setStatus(online) {
  $("status").textContent = online ? "live" : waitingForLogin ? "locked" : "reconnecting…";
  $("status").className = `status ${online ? "online" : "offline"}`;
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
  connect();
};
$("logout").onclick = () => { store.clear(); location.reload(); };

function notice(text) {
  $("notice").hidden = !text;
  $("notice").textContent = text || "";
}

// ------------------------------------------------------------------ scene

const host = $("canvas-host");
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFShadowMap;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
host.append(renderer.domElement);
const labelRenderer = new CSS2DRenderer();
labelRenderer.domElement.className = "labels";
host.append(labelRenderer.domElement);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 2000);
camera.position.set(0, 70, 90);
const controls = new OrbitControls(camera, labelRenderer.domElement);
controls.enableDamping = true;
controls.maxPolarAngle = Math.PI * 0.47;
controls.minDistance = 3;
controls.maxDistance = 400;

const hemi = new THREE.HemisphereLight(0xffffff, 0x55664a, 1.2);
const sun = new THREE.DirectionalLight(0xffffff, 2.5);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
sun.shadow.bias = -0.0005;
sun.shadow.normalBias = 0.04;
Object.assign(sun.shadow.camera, { left: -120, right: 120, top: 120, bottom: -120, near: 1, far: 400 });
scene.add(hemi, sun, sun.target);

new ResizeObserver(() => {
  const { clientWidth: w, clientHeight: h } = host;
  if (!w || !h) return;
  renderer.setSize(w, h);
  labelRenderer.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}).observe(host);

const PHASES = {
  night: { sky: 0x0d1430, sun: 0x8090c0, sunI: 0.12, hemiI: 0.22 },
  dawn: { sky: 0xe9b39c, sun: 0xffc4a0, sunI: 1.2, hemiI: 0.7 },
  morning: { sky: 0xa8d3f2, sun: 0xffffff, sunI: 2.6, hemiI: 1.15 },
  afternoon: { sky: 0x9ccbee, sun: 0xfff2dc, sunI: 2.8, hemiI: 1.2 },
  evening: { sky: 0xe9a06a, sun: 0xffa060, sunI: 1.0, hemiI: 0.55 },
};

function applyTime(clock) {
  const p = PHASES[clock.phase] || PHASES.afternoon;
  scene.background = new THREE.Color(p.sky);
  sun.color.set(p.sun);
  sun.intensity = p.sunI;
  hemi.intensity = p.hemiI;
  // The sun travels east to west between 06:00 and 18:00 (low and dim outside those hours).
  const t = ((clock.hour * 60 + clock.minute) - 360) / 720;
  const angle = Math.PI * Math.min(Math.max(t, 0.03), 0.97);
  sun.position.set(Math.cos(angle) * 120, Math.max(Math.sin(angle) * 140, 25), 60);
  for (const { node, phases } of model.timeNodes) node.visible = phases.includes(clock.phase);
  $("clock").textContent = clock.label;
  document.body.dataset.phase = clock.phase;
}

// ---------------------------------------------------------- world models

const state = { rooms: new Map(), entities: new Map(), objects: new Map(), areas: new Map(), clock: null };
const model = {
  floors: new Map(), // room id -> [mesh]
  exits: new Map(), // room id -> Map(label -> Object3D)
  objects: new Map(), // object id -> { node, room, parent, position, quaternion }
  spots: new Map(), // object id -> Object3D
  seats: new Map(), // object id -> [Object3D], in name order (seat:sofa, seat:sofa.001, ...)
  spawns: new Map(), // room id -> [Object3D]
  whenNodes: [], // { node, objectId, conds }
  timeNodes: [], // { node, phases }
  hideInside: new Map(), // room or area id -> [Object3D] hidden while looking inside it
  walls: [], // wall meshes, faded while they're between the camera and who you're following
  plots: new Map(), // plot -> Object3D (town)
  gates: new Map(), // plot -> Object3D (town)
  houseRoots: new Map(), // owner -> Object3D
  loaded: false,
};

const gltfLoader = new GLTFLoader();
const vrmLoader = new GLTFLoader();
vrmLoader.register((parser) => new VRMLoaderPlugin(parser));

const nameOf = (node) => (node.userData.name || node.name || "").replace(/\.\d{3,}$/, "");
const push = (map, key, value) => (map.get(key) || map.set(key, []).get(key)).push(value);
const worldPos = (node) => node.getWorldPosition(new THREE.Vector3());

function ancestorArg(node, kind) {
  for (let n = node; n; n = n.parent) {
    const name = nameOf(n);
    if (name.startsWith(`${kind}:`)) return name.slice(kind.length + 1).split(":")[0];
  }
  return null;
}

function parseConds(text) {
  return text.split(",").map((part) => {
    const [key, raw = "true"] = part.split("=");
    let value = raw;
    if (raw === "true" || raw === "false") value = raw === "true";
    else if (raw !== "" && !Number.isNaN(Number(raw))) value = Number(raw);
    return [key.trim(), value];
  });
}

/** Read the spec's markers out of a loaded model. Room/object ids get the house owner's prefix. */
function indexModel(root, areaId, prefix) {
  root.traverse((node) => {
    if (node.isMesh) { node.castShadow = true; node.receiveShadow = true; }
    const name = nameOf(node);
    const colon = name.indexOf(":");
    if (colon < 0) return;
    const kind = name.slice(0, colon);
    const rest = name.slice(colon + 1);
    const args = rest.split(":");
    const roomId = (local) => prefix + local;
    switch (kind) {
      case "floor":
        node.traverse((m) => { if (m.isMesh) push(model.floors, roomId(args[0]), m); });
        break;
      case "exit":
        if (!model.exits.has(roomId(args[0]))) model.exits.set(roomId(args[0]), new Map());
        model.exits.get(roomId(args[0])).set(args[1], node);
        break;
      case "obj": {
        const room = ancestorArg(node, "room");
        model.objects.set(prefix + args[0], {
          node, room: room && roomId(room), parent: node.parent,
          position: node.position.clone(), quaternion: node.quaternion.clone(),
        });
        break;
      }
      case "spot": model.spots.set(prefix + args[0], node); break;
      case "seat": push(model.seats, prefix + args[0], node); break;
      case "spawn": push(model.spawns, roomId(args[0]), node); break;
      case "when": {
        const obj = ancestorArg(node.parent, "obj");
        if (obj) model.whenNodes.push({ node, objectId: prefix + obj, conds: parseConds(rest) });
        break;
      }
      case "time": model.timeNodes.push({ node, phases: rest.split("+") }); break;
      case "ext":
      case "ceiling": {
        // Inside a house: hide all of its roofs and ceilings. In town (a café): just that room's,
        // and an ext: that isn't inside any room: is never hidden.
        const room = kind === "ceiling" ? args[0] : ancestorArg(node, "room");
        if (prefix || room) push(model.hideInside, prefix ? areaId : roomId(room), node);
        break;
      }
      case "wall":
        node.traverse((m) => {
          if (!m.isMesh) return;
          m.material = m.material.clone(); // so fading one wall doesn't fade every wall sharing its material
          model.walls.push(m);
        });
        break;
      case "plot": if (!prefix) model.plots.set(args[0], node); break;
      case "gate": if (!prefix) model.gates.set(args[0], node); break;
      default: break;
    }
  });
}

async function loadModel(url) {
  gltfLoader.setRequestHeader(password ? { Authorization: `Bearer ${password}` } : {});
  const gltf = await gltfLoader.loadAsync(httpBase + url);
  return gltf.scene;
}

/** Rooms without modelled floors get simple tiles, so the 3D view works before models exist. */
function addTiles(rooms, origin, spacing, size) {
  for (const room of rooms) {
    if (model.floors.has(room.id)) continue;
    const tile = new THREE.Mesh(
      new THREE.BoxGeometry(size, 0.1, size),
      new THREE.MeshStandardMaterial({ color: room.area === "town" ? 0x9a9488 : 0xc9b79c }),
    );
    tile.receiveShadow = true;
    tile.position.set(origin.x + room.map[0] * spacing, -0.05, origin.z + room.map[1] * spacing);
    scene.add(tile);
    push(model.floors, room.id, tile);
    const tag = new CSS2DObject(el("div", "room-tag", room.name));
    tag.position.set(0, 0.2, size / 2 - 0.4);
    tile.add(tag);
  }
}

function placeholderHouse() {
  const group = new THREE.Group();
  const body = new THREE.Mesh(new THREE.BoxGeometry(10, 3, 8), new THREE.MeshStandardMaterial({ color: 0xd8d0c4 }));
  body.position.y = 1.5;
  const roof = new THREE.Mesh(new THREE.ConeGeometry(7.5, 2.5, 4), new THREE.MeshStandardMaterial({ color: 0x9a4a3a }));
  roof.position.y = 4.25;
  roof.rotation.y = Math.PI / 4;
  roof.scale.set(1, 1, 0.8);
  group.add(body, roof);
  group.traverse((m) => { if (m.isMesh) { m.castShadow = true; m.receiveShadow = true; } });
  return group;
}

async function buildWorld() {
  const town = [...state.areas.values()].find((a) => a.kind === "town");
  const rooms = [...state.rooms.values()];
  if (town && town.model_url) {
    try {
      const root = await loadModel(town.model_url);
      scene.add(root);
      root.updateMatrixWorld(true);
      indexModel(root, "town", "");
    } catch (err) {
      notice(`Couldn't load the town model: ${err.message || err}`);
    }
  } else {
    notice("This world has no 3D model yet, so rooms are shown as tiles. See docs/3d-spec.md.");
  }
  addTiles(rooms.filter((r) => r.area === "town"), new THREE.Vector3(-40, 0, -20), 14, 12);

  for (const area of state.areas.values()) {
    if (area.kind !== "house") continue;
    const plot = model.plots.get(String(area.plot));
    const holder = new THREE.Group();
    if (plot) {
      plot.updateWorldMatrix(true, false);
      plot.matrixWorld.decompose(holder.position, holder.quaternion, holder.scale);
    }
    scene.add(holder);
    model.houseRoots.set(area.owner, holder);
    const houseRooms = rooms.filter((r) => r.area === area.id);
    let loaded = false;
    if (area.model_url) {
      try {
        const root = await loadModel(area.model_url);
        holder.add(root);
        holder.updateMatrixWorld(true);
        indexModel(root, area.id, `${area.owner}.`);
        loaded = true;
      } catch (err) {
        notice(`Couldn't load ${area.name}: ${err.message || err}`);
      }
    }
    if (!loaded) {
      if (plot) holder.add(placeholderHouse());
      // Small tiles in front of the placeholder, so its rooms still have somewhere to stand.
      const origin = holder.localToWorld(new THREE.Vector3(-3, 0, 5.5));
      addTiles(houseRooms, origin, 3.2, 3);
    }
  }
  const rawName = (n) => n.userData.name || n.name || "";
  for (const list of model.seats.values()) list.sort((a, b) => rawName(a).localeCompare(rawName(b)));
  model.loaded = true;
  minimap.build([...model.floors].map(([id, meshes]) => {
    const room = state.rooms.get(id);
    const area = state.areas.get(room?.area);
    return { id, name: room?.name || id, area: room?.area, house: area?.kind === "house" ? (state.entities.get(area.owner)?.name || area.name) : null, meshes };
  }));
  frameAll();
  if (state.clock) applyTime(state.clock);
  for (const o of state.objects.values()) applyObject(o);
  applyWhen();
  for (const e of state.entities.values()) syncCharacter(e, { teleport: true });
}

// --------------------------------------------------------- object states

function stateMatches(stateObj, conds) {
  if (!stateObj) return conds.every(([, v]) => v === false);
  return conds.every(([k, v]) => stateObj[k] === v);
}

function applyWhen() {
  for (const w of model.whenNodes) w.node.visible = stateMatches(state.objects.get(w.objectId)?.state, w.conds);
}

function floorCentre(roomId) {
  const box = new THREE.Box3();
  for (const m of model.floors.get(roomId) || []) box.expandByObject(m);
  return box.isEmpty() ? null : box.getCenter(new THREE.Vector3());
}

function applyObject(o) {
  const rec = model.objects.get(o.id);
  if (!rec) return;
  const loc = o.location;
  if (loc.kind === "entity") {
    rec.node.visible = false; // carried: shown in the person's label instead
    return;
  }
  if (loc.kind === "container") {
    const box = state.objects.get(loc.id);
    rec.node.visible = !!(box && box.container && box.container.open);
    return;
  }
  rec.node.visible = true;
  if (loc.id === rec.room) {
    if (rec.node.parent !== rec.parent) {
      rec.parent.add(rec.node);
      rec.node.position.copy(rec.position);
      rec.node.quaternion.copy(rec.quaternion);
    }
  } else {
    // Put down somewhere other than where it was modelled: set it on that room's floor.
    const centre = floorCentre(loc.id);
    if (!centre) { rec.node.visible = false; return; }
    scene.attach(rec.node);
    const spread = hashAngle(o.id);
    rec.node.position.set(centre.x + Math.cos(spread) * 1.5, floorHeight(loc.id, centre.x, centre.z), centre.z + Math.sin(spread) * 1.5);
  }
}

function hashAngle(text) {
  let h = 0;
  for (const ch of text) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return (h % 360) * (Math.PI / 180);
}

const ray = new THREE.Raycaster();
function floorHeight(roomId, x, z) {
  const floors = model.floors.get(roomId);
  if (!floors) return 0;
  ray.set(new THREE.Vector3(x, 100, z), new THREE.Vector3(0, -1, 0));
  const hit = ray.intersectObjects(floors, false)[0];
  return hit ? hit.point.y : worldPos(floors[0]).y;
}

// ------------------------------------------------------------- characters

const characters = new Map(); // entity id -> Character

class Character {
  constructor(entity) {
    this.id = entity.id;
    this.group = new THREE.Group();
    this.group.visible = false;
    this.body = null;
    this.vrm = null;
    this.avatarKey = undefined;
    this.path = [];
    this.facing = null;
    this.walkPhase = 0;
    this.moving = false;
    this.room = null;
    this.target = null;
    this.lookAt = null;
    this.speed = WALK_SPEED;
    this.seat = null; // where the server says they sit or lie: { key, kind, pos, facing }
    this.posed = null; // the seat they are on, or getting onto or off
    this.from = null; // where they stood before sitting down
    this.blend = 0; // 0 standing .. 1 fully seated
    this.hipHeight = 0.86; // standing hip height; bodies are lowered by this much to sit

    const label = el("div", "char-label");
    this.bubble = el("div", "bubble");
    this.bubble.hidden = true;
    this.nameTag = el("div", "char-name");
    label.append(this.bubble, this.nameTag);
    this.label = new CSS2DObject(label);
    this.label.position.set(0, 2.05, 0);
    this.group.add(this.label);
    scene.add(this.group);
    this.setBody(this.placeholder(entity));
  }

  placeholder(entity) {
    const g = new THREE.Group();
    const mat = new THREE.MeshStandardMaterial({ color: colorFor(entity.id), roughness: 0.6 });
    const body = new THREE.Mesh(new THREE.CapsuleGeometry(0.28, 0.9, 6, 12), mat);
    body.position.y = 0.73;
    const head = new THREE.Mesh(new THREE.SphereGeometry(0.22, 16, 12), mat);
    head.position.y = 1.5;
    const nose = new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.08, 0.12), new THREE.MeshStandardMaterial({ color: 0x222222 }));
    nose.position.set(0, 1.52, 0.22);
    g.add(body, head, nose);
    g.traverse((m) => { if (m.isMesh) m.castShadow = true; });
    return g;
  }

  setBody(obj, vrm = null) {
    if (this.body) {
      this.group.remove(this.body);
      if (this.vrm) VRMUtils.deepDispose(this.vrm.scene);
    }
    this.body = obj;
    this.vrm = vrm;
    this.group.add(obj);
    this.hipHeight = 0.86;
    const hips = vrm && vrm.humanoid.getNormalizedBoneNode("hips");
    if (hips) {
      obj.updateMatrixWorld(true);
      this.hipHeight = obj.worldToLocal(hips.getWorldPosition(new THREE.Vector3())).y || 0.86;
    }
  }

  async setAvatar(entity) {
    const avatar = entity.avatar;
    const key = avatar ? avatar.sha256 : null;
    if (key === this.avatarKey) return;
    this.avatarKey = key;
    try {
      if (!avatar) this.setBody(this.placeholder(entity));
      else if (avatar.kind === "png") this.setBody(await pngBody(httpBase + avatar.url));
      else if (avatar.kind === "vrm") {
        const gltf = await vrmLoader.loadAsync(httpBase + avatar.url);
        const vrm = gltf.userData.vrm;
        if (!vrm) throw new Error("not a VRM");
        VRMUtils.removeUnnecessaryVertices(gltf.scene);
        VRMUtils.combineSkeletons(gltf.scene);
        VRMUtils.rotateVRM0(vrm);
        vrm.scene.traverse((m) => { if (m.isMesh) { m.castShadow = true; m.frustumCulled = false; } });
        relaxArms(vrm);
        if (this.avatarKey === key) this.setBody(vrm.scene, vrm);
      } else {
        const gltf = await gltfLoader.loadAsync(httpBase + avatar.url);
        if (this.avatarKey === key) this.setBody(normalised(gltf.scene));
      }
    } catch (err) {
      console.warn(`avatar for ${entity.id} failed to load`, err);
      this.setBody(this.placeholder(entity));
    }
  }

  update(dt) {
    let speed = 0;
    const leaving = this.posed && (!this.seat || this.seat.key !== this.posed.key || this.path.length);
    if (leaving) {
      // Get up before walking anywhere.
      this.blend = Math.max(0, this.blend - dt / POSE_SECONDS);
      if (this.blend === 0) {
        this.group.position.copy(this.from);
        this.posed = null;
      }
    } else if (this.path.length) {
      const next = this.path[0];
      const pos = this.group.position;
      const to = new THREE.Vector3().subVectors(next, pos);
      const dist = to.length();
      const step = this.speed * dt;
      if (dist <= step || dist < 0.01) {
        pos.copy(next);
        this.path.shift();
      } else {
        pos.addScaledVector(to.normalize(), step);
        this.facing = Math.atan2(to.x, to.z);
        speed = this.speed;
      }
    }
    this.moving = speed > 0;
    if (!this.posed && this.seat && !this.path.length) {
      this.posed = this.seat;
      this.from = this.group.position.clone();
    }
    if (this.posed) {
      if (!leaving) this.blend = Math.min(1, this.blend + dt / POSE_SECONDS);
      const t = this.blend * this.blend * (3 - 2 * this.blend);
      this.group.position.lerpVectors(this.from, this.posed.pos, t);
      this.facing = this.posed.facing;
    } else if (!this.path.length && this.lookAt) {
      const pos = this.group.position;
      this.facing = Math.atan2(this.lookAt.x - pos.x, this.lookAt.z - pos.z);
    }
    if (this.facing != null) {
      const diff = Math.atan2(Math.sin(this.facing - this.group.rotation.y), Math.cos(this.facing - this.group.rotation.y));
      this.group.rotation.y += diff * Math.min(1, dt * 8);
    }
    this.walkPhase += dt * (this.moving ? 9 : 0);
    const swing = this.moving ? Math.sin(this.walkPhase) : 0;
    const t = this.posed ? this.blend * this.blend * (3 - 2 * this.blend) : 0;
    const flat = this.body && !this.body.isSprite; // a sprite always faces the camera, so it can only sit
    const lie = this.posed && this.posed.kind === "lying" && flat ? t : 0;
    const sit = this.posed ? t - lie : 0;
    if (this.vrm) {
      const bone = (n) => this.vrm.humanoid.getNormalizedBoneNode(n);
      // Sitting: thighs forward, shins down. Lying: the whole body tips back, legs straight.
      const legs = [["leftUpperLeg", 0.5], ["rightUpperLeg", -0.5]];
      for (const [b, s] of legs) { const n = bone(b); if (n) n.rotation.x = swing * s - (Math.PI / 2) * sit; }
      for (const b of ["leftLowerLeg", "rightLowerLeg"]) { const n = bone(b); if (n) n.rotation.x = (Math.PI / 2) * sit; }
      const arms = [["leftUpperArm", -0.35], ["rightUpperArm", 0.35]];
      for (const [b, s] of arms) { const n = bone(b); if (n) n.rotation.x = swing * s; }
      const chest = bone("chest");
      const breath = lie > 0.5 ? 1600 : 900; // slower while lying down
      if (chest) chest.rotation.x = Math.sin(performance.now() / breath) * 0.02;
      this.vrm.update(dt);
    }
    if (this.body) {
      // The group stands on the seat; lower the body so the hips land there (or lay it on its back).
      const drop = this.vrm ? this.hipHeight : 0.35; // a stiff placeholder just sinks in a little
      const bob = this.moving && !this.vrm ? Math.abs(Math.sin(this.walkPhase)) * 0.06 : 0;
      this.body.position.set(0, bob - drop * sit + 0.12 * lie, this.hipHeight * lie);
      this.body.rotation.x = -(Math.PI / 2) * lie;
      this.label.position.y = 2.05 - 0.6 * sit - 1.3 * lie;
    }
  }

  setSeat(seat, teleport) {
    this.seat = seat;
    if (teleport && seat) {
      this.posed = seat;
      this.from = this.group.position.clone();
      this.blend = 1;
      this.group.rotation.y = seat.facing;
    } else if (teleport) {
      this.posed = null;
      this.blend = 0;
    }
  }

  walkTo(points, teleport) {
    if (!points.length) return;
    if (teleport || !this.group.visible) {
      this.group.position.copy(points[points.length - 1]);
      this.path = [];
      this.group.visible = true;
      return;
    }
    // A new destination replaces the rest of an old walk, starting from wherever they are now.
    let total = 0;
    let prev = this.group.position;
    for (const p of points) { total += prev.distanceTo(p); prev = p; }
    this.speed = Math.max(WALK_SPEED, total / MAX_WALK_SECONDS);
    this.path = points.map((p) => p.clone());
  }

  say(text, kind) {
    this.bubble.textContent = text;
    this.bubble.className = `bubble ${kind}`;
    this.bubble.hidden = false;
    clearTimeout(this.bubbleTimer);
    this.bubbleTimer = setTimeout(() => { this.bubble.hidden = true; }, BUBBLE_SECONDS * 1000);
  }
}

function relaxArms(vrm) {
  const l = vrm.humanoid.getNormalizedBoneNode("leftUpperArm");
  const r = vrm.humanoid.getNormalizedBoneNode("rightUpperArm");
  if (l) l.rotation.z = -1.2;
  if (r) r.rotation.z = 1.2;
}

function normalised(object) {
  // Scale any model to about 1.7 m tall, standing on y = 0.
  const box = new THREE.Box3().setFromObject(object);
  const height = box.max.y - box.min.y || 1;
  const s = 1.7 / height;
  object.scale.setScalar(s);
  object.position.y = -box.min.y * s;
  const holder = new THREE.Group();
  holder.add(object);
  holder.traverse((m) => { if (m.isMesh) m.castShadow = true; });
  return holder;
}

function pngBody(url) {
  return new Promise((resolve, reject) => {
    new THREE.TextureLoader().load(url, (tex) => {
      tex.colorSpace = THREE.SRGBColorSpace;
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true }));
      const aspect = tex.image.width / tex.image.height;
      sprite.center.set(0.5, 0);
      sprite.scale.set(1.7 * aspect, 1.7, 1);
      resolve(sprite);
    }, undefined, reject);
  });
}

// ---------------------------------------------------------- positioning

function exitMarker(fromRoom, toRoom) {
  const room = state.rooms.get(fromRoom);
  const markers = model.exits.get(fromRoom);
  if (!room || !markers) return null;
  for (const x of room.exits) if (x.to === toRoom && markers.has(x.direction)) return markers.get(x.direction);
  return null;
}

function gateBetween(a, b) {
  for (const id of [a, b]) {
    const area = state.areas.get(state.rooms.get(id)?.area);
    if (area && area.kind === "house") {
      const other = id === a ? b : a;
      if (state.rooms.get(other)?.area !== area.id) return model.gates.get(String(area.plot)) || null;
    }
  }
  return null;
}

function onFloor(roomId, v) {
  return new THREE.Vector3(v.x, floorHeight(roomId, v.x, v.z), v.z);
}

/**
 * Where someone sits or lies (protocol 1.3 `pose`): a seat:<obj> marker, facing the marker's
 * -Y in Blender (+Z here), or else the top of the object, facing the middle of the room.
 */
function seatPlace(entity) {
  const pose = entity.pose;
  if (!pose) return null;
  const key = `${pose.kind}:${pose.object}:${pose.seat}`;
  const markers = model.seats.get(pose.object) || [];
  if (markers.length) {
    const node = markers[pose.seat % markers.length];
    const forward = new THREE.Vector3(0, 0, 1).applyQuaternion(node.getWorldQuaternion(new THREE.Quaternion()));
    const pos = worldPos(node);
    const extra = Math.floor(pose.seat / markers.length); // more seats in the YAML than markers: squeeze up
    if (extra) pos.add(new THREE.Vector3(forward.z, 0, -forward.x).multiplyScalar(0.6 * extra));
    return { key, kind: pose.kind, pos, facing: Math.atan2(forward.x, forward.z) };
  }
  const obj = model.objects.get(pose.object);
  if (!obj || !obj.node.visible) return null;
  const box = new THREE.Box3().setFromObject(obj.node);
  if (box.isEmpty()) return null;
  const centre = box.getCenter(new THREE.Vector3());
  const room = floorCentre(entity.room) || centre;
  const away = Math.atan2(room.x - centre.x, room.z - centre.z);
  const side = (pose.seat % 2 ? 1 : -1) * Math.ceil(pose.seat / 2) * 0.6;
  const pos = new THREE.Vector3(centre.x + Math.cos(away) * side, pose.kind === "lying" ? box.max.y : Math.min(box.max.y, box.min.y + 0.45), centre.z - Math.sin(away) * side);
  return { key, kind: pose.kind, pos, facing: away };
}

/** Where someone in a room stands, and which way they face. */
function standingPlace(entity, depth = 0) {
  const roomId = entity.room;
  const near = entity.near;
  if (near && depth < 3) {
    if (near.kind === "object") {
      const spot = model.spots.get(near.id);
      const obj = model.objects.get(near.id);
      if (spot) return { pos: onFloor(roomId, worldPos(spot)), look: obj ? worldPos(obj.node) : null };
      if (obj) {
        const at = worldPos(obj.node);
        const centre = floorCentre(roomId) || at;
        const dir = new THREE.Vector3(centre.x - at.x, 0, centre.z - at.z).normalize().multiplyScalar(0.9);
        return { pos: onFloor(roomId, at.clone().add(dir)), look: at };
      }
    } else if (near.kind === "entity" && state.entities.has(near.id)) {
      const other = standingPlace(state.entities.get(near.id), depth + 1);
      if (other) {
        const a = hashAngle(entity.id);
        const pos = other.pos.clone().add(new THREE.Vector3(Math.cos(a) * 0.9, 0, Math.sin(a) * 0.9));
        return { pos: onFloor(roomId, pos), look: other.pos };
      }
    }
  }
  // Otherwise: the room's spawn points in order, then a ring around the middle of the floor.
  const others = [...state.entities.values()].filter((e) => e.room === roomId && !e.near).map((e) => e.id).sort();
  const index = Math.max(0, others.indexOf(entity.id));
  const spawns = model.spawns.get(roomId) || [];
  if (index < spawns.length) return { pos: onFloor(roomId, worldPos(spawns[index])), look: null };
  const centre = floorCentre(roomId);
  if (!centre) return null;
  const k = index - spawns.length;
  const radius = k === 0 ? 0 : 1.2 + Math.floor((k - 1) / 6) * 0.9;
  const angle = (k * Math.PI) / 3;
  return { pos: onFloor(roomId, centre.clone().add(new THREE.Vector3(Math.cos(angle) * radius, 0, Math.sin(angle) * radius))), look: null };
}

function syncCharacter(entity, { teleport = false } = {}) {
  if (!model.loaded) return;
  let ch = characters.get(entity.id);
  if (!ch) {
    ch = new Character(entity);
    characters.set(entity.id, ch);
    teleport = true;
  }
  ch.setAvatar(entity);
  ch.nameTag.textContent = entity.name;
  ch.nameTag.classList.toggle("offline", !entity.online);
  const place = standingPlace(entity);
  if (!place) { ch.group.visible = false; ch.room = entity.room; return; }
  const points = [];
  if (!teleport && ch.room && ch.room !== entity.room) {
    const out = exitMarker(ch.room, entity.room);
    if (out) points.push(onFloor(ch.room, worldPos(out)));
    const gate = gateBetween(ch.room, entity.room);
    if (gate) points.push(worldPos(gate));
    const inn = exitMarker(entity.room, ch.room);
    if (inn) points.push(onFloor(entity.room, worldPos(inn)));
  }
  if (!ch.target || ch.target.distanceTo(place.pos) > 0.05 || ch.room !== entity.room) {
    points.push(place.pos);
    ch.walkTo(points, teleport);
  }
  ch.target = place.pos;
  ch.room = entity.room;
  ch.lookAt = place.look;
  const seat = seatPlace(entity);
  if (teleport || (seat && seat.key) !== (ch.seat && ch.seat.key)) ch.setSeat(seat, teleport);
}

// ----------------------------------------------------------------- camera

let following = null;
const overviewBox = new THREE.Box3();

function frameAll() {
  overviewBox.makeEmpty();
  for (const list of model.floors.values()) for (const m of list) overviewBox.expandByObject(m);
  if (overviewBox.isEmpty()) overviewBox.set(new THREE.Vector3(-20, 0, -20), new THREE.Vector3(20, 0, 20));
  const centre = overviewBox.getCenter(new THREE.Vector3());
  const size = overviewBox.getSize(new THREE.Vector3());
  const dist = Math.max(size.x, size.z) * 0.9 + 10;
  controls.target.copy(centre);
  camera.position.set(centre.x, centre.y + dist * 0.75, centre.z + dist * 0.8);
  controls.update();
}

function follow(id) {
  following = id;
  const ch = characters.get(id);
  if (ch) {
    const offset = camera.position.clone().sub(controls.target);
    if (offset.length() > 30) offset.setLength(16);
    controls.target.copy(ch.group.position);
    camera.position.copy(ch.group.position).add(offset);
  }
  renderPeople();
  updateCutaway();
}

$("overview").onclick = () => { following = null; frameAll(); renderPeople(); updateCutaway(); };

/** Look at a point on the ground (a click on the map), keeping the camera's angle. */
function lookAt(x, z) {
  following = null;
  const offset = camera.position.clone().sub(controls.target);
  if (offset.length() > 40) offset.setLength(22);
  controls.target.set(x, 0, z);
  camera.position.copy(controls.target).add(offset);
  renderPeople();
  updateCutaway();
}

const minimap = createMinimap({
  host: $("minimap"),
  people: () => [...characters.values()].filter((ch) => ch.group.visible).map((ch) => {
    const e = state.entities.get(ch.id);
    return {
      id: ch.id, name: e?.name || ch.id, offline: !e?.online,
      x: ch.group.position.x, z: ch.group.position.z, color: `#${colorFor(ch.id).getHexString()}`,
    };
  }),
  view: () => ({ target: controls.target, camera: camera.position }),
  following: () => following,
  onFollow: (id) => follow(id),
  onLook: lookAt,
});

function updateCutaway() {
  const inside = new Set();
  const e = following && state.entities.get(following);
  if (e) {
    inside.add(e.room);
    inside.add(state.rooms.get(e.room)?.area);
  }
  for (const [key, nodes] of model.hideInside) for (const n of nodes) n.visible = !inside.has(key);
}

const wallRay = new THREE.Raycaster();
let fadedWalls = new Set();
function fadeWalls() {
  const next = new Set();
  const ch = following && characters.get(following);
  if (ch && ch.group.visible) {
    const head = ch.group.position.clone().add(new THREE.Vector3(0, 1.2, 0));
    const dir = head.clone().sub(camera.position);
    const dist = dir.length();
    wallRay.set(camera.position, dir.normalize());
    wallRay.far = dist;
    for (const hit of wallRay.intersectObjects(model.walls, false)) next.add(hit.object);
  }
  for (const m of fadedWalls) if (!next.has(m)) { m.material.transparent = false; m.material.opacity = 1; m.material.depthWrite = true; }
  for (const m of next) { m.material.transparent = true; m.material.opacity = 0.18; m.material.depthWrite = false; }
  fadedWalls = next;
}

// --------------------------------------------------------------------- HUD

function renderPeople() {
  const list = $("people");
  list.replaceChildren();
  const people = [...state.entities.values()].sort((a, b) => a.name.localeCompare(b.name));
  for (const e of people) {
    const li = el("li");
    const button = el("button");
    button.type = "button";
    button.setAttribute("aria-pressed", String(following === e.id));
    const dot = el("span", "dot");
    dot.style.background = `#${colorFor(e.id).getHexString()}`;
    const name = el("span", e.online ? "" : "offline", e.name);
    const where = el("span", "where", state.rooms.get(e.room)?.name || "");
    button.append(dot, name, where);
    button.onclick = () => (following === e.id ? $("overview").onclick() : follow(e.id));
    li.append(button);
    list.append(li);
  }
}

function addChat(ev) {
  if (ev.type !== "speech" || ev.muffled) return;
  const li = el("li");
  li.append(el("span", "who", ev.speaker_name));
  const text = ev.mode === "whisper"
    ? ` whispers to ${ev.to_name}: ${ev.text == null ? "…" : `“${ev.text}”`}`
    : ev.mode === "shout" ? ` shouts: “${ev.text}”` : `: ${ev.text}`;
  li.append(el("span", ev.mode === "say" ? "" : ev.mode, text));
  const chat = $("chat");
  chat.append(li);
  while (chat.children.length > 40) chat.firstChild.remove();
  chat.scrollTop = chat.scrollHeight;
}

// -------------------------------------------------------------- streaming

function loadState(data) {
  const major = parseInt(String(data.protocol).split(".")[0], 10);
  if (major !== PROTOCOL_MAJOR) notice(`This server speaks protocol ${data.protocol}; this viewer expects ${PROTOCOL_MAJOR}.x.`);
  $("world-name").textContent = data.name;
  document.title = `${data.name} · Commonroom 3D`;
  state.rooms = new Map(data.rooms.map((r) => [r.id, r]));
  state.entities = new Map(data.entities.map((e) => [e.id, e]));
  state.objects = new Map(data.objects.map((o) => [o.id, o]));
  state.areas = new Map((data.areas || [{ id: "town", kind: "town", name: data.name }]).map((a) => [a.id, a]));
  for (const r of state.rooms.values()) if (!r.area) r.area = "town"; // protocol 1.0/1.1 servers
  state.clock = data.clock;
  for (const ev of data.recent_events) addChat(ev);
  renderPeople();
  if (!model.loaded && !buildWorld.started) {
    buildWorld.started = true;
    buildWorld();
  } else if (model.loaded) {
    applyTime(state.clock);
    for (const o of state.objects.values()) applyObject(o);
    applyWhen();
    for (const e of state.entities.values()) syncCharacter(e);
  }
}

function applyUpdate(data) {
  for (const o of data.objects) { state.objects.set(o.id, o); applyObject(o); }
  for (const e of data.entities) state.entities.set(e.id, e);
  if (data.objects.length) applyWhen();
  if (data.clock) {
    const phaseChanged = !state.clock || state.clock.phase !== data.clock.phase;
    state.clock = data.clock;
    if (model.loaded && (phaseChanged || data.events.some((ev) => ev.type === "time_tick"))) applyTime(state.clock);
    else $("clock").textContent = data.clock.label;
  }
  // Re-place everyone in the rooms that changed (an arrival can shift the others' spots).
  const rooms = new Set(data.entities.map((e) => e.room));
  for (const ev of data.events) if (ev.room) rooms.add(ev.room);
  for (const e of state.entities.values()) {
    if (rooms.has(e.room) || data.entities.some((x) => x.id === e.id)) syncCharacter(e);
  }
  for (const ev of data.events) {
    addChat(ev);
    const ch = characters.get(ev.speaker || ev.entity);
    if (ev.type === "speech" && ch && !ev.muffled) {
      ch.say(ev.mode === "whisper" ? (ev.text == null ? "(whispers)" : `(whispers) ${ev.text}`) : ev.text, ev.mode);
    } else if (ev.type === "typing" && ch) {
      ch.nameTag.classList.toggle("typing", ev.active);
    }
  }
  if (data.entities.length) { renderPeople(); updateCutaway(); }
}

// ------------------------------------------------------------------- loop

const timer = new THREE.Timer();
let lastFade = 0;
renderer.setAnimationLoop((now) => {
  timer.update(now);
  const dt = Math.min(timer.getDelta(), 0.5); // keep walks on time even at a low frame rate
  for (const ch of characters.values()) ch.update(dt);
  if (following && characters.has(following)) {
    const target = characters.get(following).group.position;
    const delta = target.clone().sub(controls.target).multiplyScalar(Math.min(1, dt * 4));
    controls.target.add(delta);
    camera.position.add(delta);
  }
  controls.update();
  if (now - lastFade > 150) { lastFade = now; fadeWalls(); } // time-based, so slow devices don't lag behind
  minimap.draw(now, following && state.entities.get(following)?.room);
  renderer.render(scene, camera);
  labelRenderer.render(scene, camera);
});

if (params.has("token")) {
  password = params.get("token");
  store.set(password, false);
  params.delete("token");
  const rest = params.toString();
  history.replaceState(null, "", `${location.pathname}${rest ? `?${rest}` : ""}`);
}
connect();

// For automated checks (and curious people in the console).
window.commonroom3d = {
  state, model, characters, scene, camera, controls, follow, lookAt, minimap, applyTime, fadeWalls,
  get following() { return following; },
};
