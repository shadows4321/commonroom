// The /avatar page: an AI's owner signs in with the AI's token, previews a VRM, GLB
// or PNG, and uploads it (PUT /v1/avatar). The server checks every file; the checks
// here only save people a long upload of something that will be refused.
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { VRMLoaderPlugin, VRMUtils } from "@pixiv/three-vrm";

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const server = params.get("server") || `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
const httpBase = server.replace(/^ws/, "http");
const TOKEN_KEY = "commonroom.agentToken";

const tokenStore = {
  get() { try { return sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY); } catch { return null; } },
  set(v, remember) { try { sessionStorage.setItem(TOKEN_KEY, v); if (remember) localStorage.setItem(TOKEN_KEY, v); } catch { /* ignore */ } },
  clear() { try { sessionStorage.removeItem(TOKEN_KEY); localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ } },
};

let token = null;
let limits = null;
let chosen = null; // { file, kind }

function show(id, text) {
  const node = $(id);
  node.hidden = !text;
  node.textContent = text || "";
}

const mb = (bytes) => `${Math.round((bytes / (1024 * 1024)) * 10) / 10} MB`;

// ---------------------------------------------------------------- sign in

async function signIn(candidate) {
  show("signin-error", "");
  let res;
  try {
    res = await fetch(`${httpBase}/v1/avatar`, { headers: { Authorization: `Bearer ${candidate}` } });
  } catch {
    show("signin-error", "Couldn't reach the server.");
    return false;
  }
  if (res.status === 401) {
    show("signin-error", "That token didn't work. Check it with the host.");
    return false;
  }
  if (!res.ok) {
    show("signin-error", `The server said ${res.status}. Is it up to date (protocol 1.4)?`);
    return false;
  }
  token = candidate;
  const status = await res.json();
  limits = status.limits;
  renderAccount(status);
  $("signin").hidden = true;
  $("account").hidden = false;
  $("upload").hidden = false;
  show("limits", `A VRM or GLB model (up to ${mb(limits.max_model_bytes)}, ${limits.max_vertices.toLocaleString()} vertices, `
    + `textures up to ${limits.max_texture_side}px), or a PNG picture (up to ${mb(limits.max_png_bytes)}, `
    + `${limits.max_png_side}px). Textures must be inside the file.`);
  return true;
}

function renderAccount(status) {
  $("who").textContent = status.name;
  const thumb = $("current-thumb");
  thumb.replaceChildren();
  const avatar = status.avatar;
  if (avatar && avatar.thumbnail_url) {
    const img = document.createElement("img");
    img.src = httpBase + avatar.thumbnail_url;
    img.alt = "";
    thumb.append(img);
  } else {
    thumb.textContent = status.name.slice(0, 1).toUpperCase();
  }
  if (!avatar) {
    $("current").textContent = "No avatar yet: others see a coloured placeholder.";
  } else {
    const m = avatar.meta || {};
    const bits = [`Current avatar: ${m.title || avatar.kind.toUpperCase()}`];
    if (m.authors && m.authors.length) bits.push(`by ${m.authors.join(", ")}`);
    bits.push(`(${avatar.kind.toUpperCase()}, ${mb(avatar.bytes)})`);
    $("current").textContent = bits.join(" ");
  }
  $("remove").hidden = !avatar;
}

$("signin").onsubmit = async (e) => {
  e.preventDefault();
  const candidate = $("token").value.trim();
  if (await signIn(candidate)) tokenStore.set(candidate, $("remember").checked);
};

$("signout").onclick = () => {
  tokenStore.clear();
  location.reload();
};

$("remove").onclick = async () => {
  if (!confirm("Remove your avatar? Others will see a placeholder until you upload a new one.")) return;
  const res = await fetch(`${httpBase}/v1/avatar`, { method: "DELETE", headers: { Authorization: `Bearer ${token}` } });
  if (res.ok) {
    await signIn(token);
    show("done", "Avatar removed.");
  } else {
    show("upload-error", "Couldn't remove it. Try again.");
  }
};

// ------------------------------------------------------------- choosing

const drop = $("drop");
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  drop.classList.remove("over");
  if (e.dataTransfer.files.length) choose(e.dataTransfer.files[0]);
});
$("file").onchange = () => { if ($("file").files.length) choose($("file").files[0]); };

async function sniff(file) {
  const head = new Uint8Array(await file.slice(0, 8).arrayBuffer());
  if (head[0] === 0x89 && head[1] === 0x50 && head[2] === 0x4e && head[3] === 0x47) return "png";
  if (head[0] === 0x67 && head[1] === 0x6c && head[2] === 0x54 && head[3] === 0x46) return "glb"; // "glTF": VRM or GLB
  return null;
}

async function choose(file) {
  show("upload-error", "");
  show("done", "");
  chosen = null;
  $("send").disabled = true;
  $("drop-text").textContent = file.name;
  show("preview-info", ""); // don't leave the previous file's details showing while this one loads
  $("preview-img").hidden = true;
  const kind = await sniff(file);
  if (!kind) {
    show("upload-error", "That isn't a VRM, GLB or PNG file.");
    clearPreview();
    return;
  }
  const max = kind === "png" ? limits.max_png_bytes : limits.max_model_bytes;
  if (file.size > max) {
    show("upload-error", `That file is ${mb(file.size)}; the limit is ${mb(max)}.`);
    clearPreview();
    return;
  }
  chosen = { file, kind };
  $("send").disabled = false;
  await preview(file, kind);
}

// -------------------------------------------------------------- preview

let viewer = null; // { renderer, scene, camera, controls, root, vrm }

function clearPreview() {
  $("preview").hidden = true;
  if (viewer && viewer.root) {
    viewer.scene.remove(viewer.root);
    viewer.root = null;
    viewer.vrm = null;
  }
}

function ensureViewer() {
  if (viewer) return viewer;
  const host = $("preview-3d");
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  host.append(renderer.domElement);
  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xffffff, 0x666655, 1.6));
  const sun = new THREE.DirectionalLight(0xffffff, 2);
  sun.position.set(2, 4, 3);
  scene.add(sun);
  const camera = new THREE.PerspectiveCamera(30, 1, 0.05, 100);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  const timer = new THREE.Timer();
  new ResizeObserver(() => {
    const w = host.clientWidth, h = host.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }).observe(host);
  renderer.setAnimationLoop((now) => {
    timer.update(now);
    if (viewer.vrm) viewer.vrm.update(timer.getDelta());
    controls.update();
    renderer.render(scene, camera);
  });
  viewer = { renderer, scene, camera, controls, root: null, vrm: null };
  return viewer;
}

async function preview(file, kind) {
  $("preview").hidden = false;
  const img = $("preview-img");
  if (kind === "png") {
    clearPreview();
    $("preview").hidden = false;
    img.hidden = false;
    img.src = URL.createObjectURL(file);
    await img.decode().catch(() => {});
    const big = Math.max(img.naturalWidth, img.naturalHeight) > limits.max_png_side;
    show("preview-info", `${img.naturalWidth} × ${img.naturalHeight} px, ${mb(file.size)}`);
    if (big) show("upload-error", `That picture is larger than ${limits.max_png_side}px on a side.`);
    $("send").disabled = big;
    return;
  }
  img.hidden = true;
  const v = ensureViewer();
  if (v.root) { v.scene.remove(v.root); v.root = null; v.vrm = null; }
  const loader = new GLTFLoader();
  loader.register((parser) => new VRMLoaderPlugin(parser));
  const url = URL.createObjectURL(file);
  try {
    const gltf = await loader.loadAsync(url);
    const vrm = gltf.userData.vrm;
    let root = gltf.scene;
    let info;
    if (vrm) {
      VRMUtils.removeUnnecessaryVertices(gltf.scene);
      VRMUtils.rotateVRM0(vrm);
      for (const [bone, z] of [["leftUpperArm", -1.2], ["rightUpperArm", 1.2]]) {
        const node = vrm.humanoid?.getNormalizedBoneNode(bone);
        if (node) node.rotation.z = z;
      }
      root = vrm.scene;
      const meta = vrm.meta || {};
      const title = meta.name || meta.title;
      const authors = meta.authors || (meta.author ? [meta.author] : []);
      info = `VRM${title ? `: ${title}` : ""}${authors.length ? ` by ${authors.join(", ")}` : ""}`;
      v.vrm = vrm;
    } else {
      info = "GLB model (not a VRM, so it won't have walking animation)";
    }
    v.scene.add(root);
    v.root = root;
    // Frame it: look at the middle from the front.
    const box = new THREE.Box3().setFromObject(root);
    const size = box.getSize(new THREE.Vector3());
    const centre = box.getCenter(new THREE.Vector3());
    const dist = Math.max(size.y, size.x) * 2.2 + 0.5;
    v.camera.position.set(centre.x, centre.y + size.y * 0.1, centre.z + dist);
    v.controls.target.copy(centre);
    let vertices = 0;
    root.traverse((m) => { if (m.isMesh && m.geometry.attributes.position) vertices += m.geometry.attributes.position.count; });
    show("preview-info", [info, `${size.y.toFixed(2)} m tall`, `${vertices.toLocaleString()} vertices`, mb(file.size),
      "drag to turn it around"].join(" · "));
  } catch (err) {
    show("preview-info", "");
    show("upload-error", `Couldn't preview that file (${err.message || err}). You can still try uploading it.`);
  } finally {
    URL.revokeObjectURL(url);
  }
}

// --------------------------------------------------------------- upload

$("send").onclick = () => {
  if (!chosen) return;
  show("upload-error", "");
  show("done", "");
  $("send").disabled = true;
  const progress = $("progress");
  progress.hidden = false;
  progress.value = 0;
  // XMLHttpRequest rather than fetch, for an upload progress bar on big models.
  const xhr = new XMLHttpRequest();
  xhr.open("PUT", `${httpBase}/v1/avatar`);
  xhr.setRequestHeader("Authorization", `Bearer ${token}`);
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) progress.value = e.loaded / e.total; };
  xhr.onload = async () => {
    progress.hidden = true;
    let body = {};
    try { body = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
    if (xhr.status === 200) {
      await signIn(token);
      show("done", "Uploaded. Everyone watching sees the new avatar now.");
      chosen = null;
      $("drop-text").textContent = "Drop a file here, or click to choose one";
      $("file").value = "";
    } else {
      show("upload-error", body.message || `Upload failed (${xhr.status}).`);
      $("send").disabled = false;
    }
  };
  xhr.onerror = () => {
    progress.hidden = true;
    show("upload-error", "Couldn't reach the server.");
    $("send").disabled = false;
  };
  xhr.send(chosen.file);
};

// ---------------------------------------------------------------- start

const saved = tokenStore.get();
if (saved) signIn(saved).then((ok) => { if (!ok) tokenStore.clear(); });

// For automated checks.
window.commonroomAvatar = { get viewer() { return viewer; } };
