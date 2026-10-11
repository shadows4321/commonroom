// The 3D view's map: the town's floors seen from above, everyone as a dot, and a wedge for
// where the camera looks. Click a dot to follow that person, or anywhere else to look there.
// Plain 2D canvas; town3d.js hands it floor meshes and positions, so it knows nothing of three.js.

const DOT_RADIUS = 4.5;
const PICK_RADIUS = 9; // px: a click this close to a dot follows that person
const PAD = 8; // px around the town
const DRAW_EVERY_MS = 100;

/**
 * @param {object} opts
 * @param {HTMLElement} opts.host        the .minimap element (holds the canvas and the toggle)
 * @param {() => {x:number,z:number,color:string,id:string,name:string,offline:boolean}[]} opts.people
 * @param {() => {target:{x:number,z:number}, camera:{x:number,z:number}}} opts.view
 * @param {() => string|null} opts.following    id of who the camera follows
 * @param {(id:string) => void} opts.onFollow
 * @param {(x:number, z:number) => void} opts.onLook
 */
export function createMinimap({ host, people, view, following, onFollow, onLook }) {
  const canvas = host.querySelector("canvas");
  const toggle = host.querySelector("button");
  const ctx = canvas.getContext("2d");
  const KEY = "commonroom.minimapHidden";
  let rooms = []; // { id, name, area, house, tris: Float32Array [x,z,x,z,x,z,...] }
  let areaLabels = []; // { name, x, z }
  let bounds = null; // { minX, minZ, maxX, maxZ }
  let scale = 1, offX = 0, offZ = 0, width = 0, height = 0;
  let activeRoom = null;
  let lastDraw = -Infinity;

  const setHidden = (hidden) => {
    host.classList.toggle("collapsed", hidden);
    toggle.setAttribute("aria-expanded", String(!hidden));
    try { hidden ? localStorage.setItem(KEY, "1") : localStorage.removeItem(KEY); } catch { /* ignore */ }
    if (!hidden) layout();
  };
  toggle.onclick = () => setHidden(!host.classList.contains("collapsed"));
  try { if (localStorage.getItem(KEY)) setHidden(true); } catch { /* ignore */ }

  /** floors: [{ id, name, area, house, meshes }]. Each floor mesh is flattened to triangles once. */
  function build(floors) {
    rooms = [];
    const byArea = new Map();
    let minX = Infinity, minZ = Infinity, maxX = -Infinity, maxZ = -Infinity;
    for (const floor of floors) {
      const tris = [];
      for (const mesh of floor.meshes) {
        mesh.updateWorldMatrix(true, false);
        const geo = mesh.geometry;
        const pos = geo.attributes.position;
        const e = mesh.matrixWorld.elements;
        const at = (i) => {
          const x = pos.getX(i), y = pos.getY(i), z = pos.getZ(i);
          return [e[0] * x + e[4] * y + e[8] * z + e[12], e[1] * x + e[5] * y + e[9] * z + e[13],
            e[2] * x + e[6] * y + e[10] * z + e[14]];
        };
        const vertex = (i) => at(geo.index ? geo.index.getX(i) : i);
        const count = geo.index ? geo.index.count : pos.count;
        for (let i = 0; i + 2 < count; i += 3) {
          const a = vertex(i), b = vertex(i + 1), c = vertex(i + 2);
          // Keep only faces that point up: a floor is often a box, and its bottom and sides
          // would cancel out or clutter the top-down outline.
          const ux = b[0] - a[0], uy = b[1] - a[1], uz = b[2] - a[2];
          const vx = c[0] - a[0], vy = c[1] - a[1], vz = c[2] - a[2];
          const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
          if (ny <= 0.5 * Math.hypot(nx, ny, nz)) continue;
          for (const [x, , z] of [a, b, c]) {
            tris.push(x, z);
            minX = Math.min(minX, x); maxX = Math.max(maxX, x);
            minZ = Math.min(minZ, z); maxZ = Math.max(maxZ, z);
          }
        }
      }
      if (!tris.length) continue;
      const room = { ...floor, tris: Float32Array.from(tris) };
      delete room.meshes;
      rooms.push(room);
      if (floor.house) {
        const a = byArea.get(floor.area) || byArea.set(floor.area, { name: floor.house, xs: [], zs: [] }).get(floor.area);
        for (let i = 0; i < tris.length; i += 2) { a.xs.push(tris[i]); a.zs.push(tris[i + 1]); }
      }
    }
    // Draw town floors first, so houses sit on top of the street.
    rooms.sort((a, b) => Number(Boolean(a.house)) - Number(Boolean(b.house)));
    areaLabels = [...byArea.values()].map((a) => ({
      name: a.name,
      x: (Math.min(...a.xs) + Math.max(...a.xs)) / 2,
      z: Math.min(...a.zs),
    }));
    bounds = rooms.length ? { minX, minZ, maxX, maxZ } : null;
    host.hidden = !bounds;
    layout();
  }

  function layout() {
    if (!bounds || host.classList.contains("collapsed")) return;
    const spanX = Math.max(bounds.maxX - bounds.minX, 1);
    const spanZ = Math.max(bounds.maxZ - bounds.minZ, 1);
    width = canvas.parentElement.clientWidth || 220;
    height = Math.round(Math.min(Math.max(width * (spanZ / spanX), 110), 260));
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.height = `${height}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    scale = Math.min((width - PAD * 2) / spanX, (height - PAD * 2) / spanZ);
    offX = (width - spanX * scale) / 2 - bounds.minX * scale;
    offZ = (height - spanZ * scale) / 2 - bounds.minZ * scale;
    lastDraw = -Infinity;
  }
  new ResizeObserver(layout).observe(canvas.parentElement);

  const toScreen = (x, z) => [x * scale + offX, z * scale + offZ];
  const toWorld = (sx, sy) => [(sx - offX) / scale, (sy - offZ) / scale];

  function cssVar(name, fallback) {
    return getComputedStyle(host).getPropertyValue(name).trim() || fallback;
  }

  function draw(now, active) {
    if (!bounds || host.classList.contains("collapsed")) return;
    if (now - lastDraw < DRAW_EVERY_MS) return; // time-based: cheap on slow devices
    lastDraw = now;
    activeRoom = active;
    const ink = cssVar("--ink", "#222");
    const accent = cssVar("--accent", "#3f6fd8");
    ctx.clearRect(0, 0, width, height);

    for (const room of rooms) {
      ctx.beginPath();
      for (let i = 0; i < room.tris.length; i += 6) {
        const [ax, ay] = toScreen(room.tris[i], room.tris[i + 1]);
        const [bx, by] = toScreen(room.tris[i + 2], room.tris[i + 3]);
        const [cx, cy] = toScreen(room.tris[i + 4], room.tris[i + 5]);
        ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.lineTo(cx, cy); ctx.closePath();
      }
      ctx.fillStyle = room.id === active ? accent : room.house ? "#c9b79c" : "#9a9488";
      ctx.globalAlpha = room.id === active ? 0.55 : 0.8;
      ctx.fill();
    }
    ctx.globalAlpha = 1;

    ctx.font = "600 10px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.fillStyle = ink;
    for (const label of areaLabels) {
      const [x, y] = toScreen(label.x, label.z);
      ctx.fillText(label.name, x, Math.max(y - 2, 11));
    }

    // The camera: a wedge from where it stands towards what it looks at.
    const v = view();
    const [tx, ty] = toScreen(v.target.x, v.target.z);
    const [cx, cy] = toScreen(v.camera.x, v.camera.z);
    const angle = Math.atan2(ty - cy, tx - cx);
    const r = 22;
    ctx.beginPath();
    ctx.moveTo(tx, ty);
    ctx.arc(tx, ty, r, angle + Math.PI - 0.5, angle + Math.PI + 0.5);
    ctx.closePath();
    ctx.fillStyle = accent;
    ctx.globalAlpha = 0.22;
    ctx.fill();
    ctx.globalAlpha = 1;

    const followed = following();
    for (const p of people()) {
      const [x, y] = toScreen(p.x, p.z);
      ctx.beginPath();
      ctx.arc(x, y, DOT_RADIUS, 0, Math.PI * 2);
      ctx.fillStyle = p.color;
      ctx.globalAlpha = p.offline ? 0.5 : 1;
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.lineWidth = p.id === followed ? 2.5 : 1.2;
      ctx.strokeStyle = p.id === followed ? accent : "#fff";
      ctx.stroke();
    }
  }

  function pointerAt(event) {
    const rect = canvas.getBoundingClientRect();
    return [event.clientX - rect.left, event.clientY - rect.top];
  }

  function personAt(sx, sy) {
    let best = null, bestD = PICK_RADIUS;
    for (const p of people()) {
      const [x, y] = toScreen(p.x, p.z);
      const d = Math.hypot(x - sx, y - sy);
      if (d <= bestD) { best = p; bestD = d; }
    }
    return best;
  }

  function roomAt(x, z) {
    // Houses are drawn last, so check them first.
    for (let k = rooms.length - 1; k >= 0; k--) {
      const t = rooms[k].tris;
      for (let i = 0; i < t.length; i += 6) {
        if (inTriangle(x, z, t[i], t[i + 1], t[i + 2], t[i + 3], t[i + 4], t[i + 5])) return rooms[k];
      }
    }
    return null;
  }

  canvas.addEventListener("click", (event) => {
    const [sx, sy] = pointerAt(event);
    const person = personAt(sx, sy);
    if (person) onFollow(person.id);
    else onLook(...toWorld(sx, sy));
  });
  canvas.addEventListener("mousemove", (event) => {
    const [sx, sy] = pointerAt(event);
    const person = personAt(sx, sy);
    const room = person ? null : roomAt(...toWorld(sx, sy));
    canvas.title = person ? `Follow ${person.name}` : room ? room.name : "";
    canvas.style.cursor = person ? "pointer" : "crosshair";
  });

  return {
    build,
    draw,
    toScreen,
    roomAt,
    get rooms() { return rooms; },
    get activeRoom() { return activeRoom; },
  };
}

function inTriangle(px, pz, ax, az, bx, bz, cx, cz) {
  const d1 = (px - bx) * (az - bz) - (ax - bx) * (pz - bz);
  const d2 = (px - cx) * (bz - cz) - (bx - cx) * (pz - cz);
  const d3 = (px - ax) * (cz - az) - (cx - ax) * (pz - az);
  const neg = d1 < 0 || d2 < 0 || d3 < 0;
  const pos = d1 > 0 || d2 > 0 || d3 > 0;
  return !(neg && pos);
}
