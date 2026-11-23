/* 3D view of the YuMi cell: live joint angles from /api/robot-pose, cups and
 * placement locations from /api/state. Read-only: this module never POSTs.
 *
 * Everything is in the YuMi base frame (wobj0), metres, z up. The model and
 * its joint frames come from tools/build_yumi_model.py. Nothing loads until the
 * 3D tab is first opened, and polling runs only while the tab is visible.
 */
import {
  AmbientLight, BufferAttribute, BufferGeometry, CanvasTexture, CircleGeometry, Color, CylinderGeometry,
  DirectionalLight, DoubleSide, GridHelper, Group, HemisphereLight, Mesh, MeshStandardMaterial, Object3D,
  PCFShadowMap, PerspectiveCamera, PlaneGeometry, SRGBColorSpace, Scene, Sprite,
  SpriteMaterial, Vector3, WebGLRenderer, OrbitControls,
} from "./three-bundle.min.js";
import { ClockSync, JointTrack } from "./pose_interpolation.js";
import { cupHolder } from "./cup_holding.js";

// /api/robot-pose only returns the dashboard's latest readings; it never waits
// for the gateway. One shared, rate-limited reader talks to the gateway
// (robot_telemetry.py), so polling this often costs the robot nothing.
const POSE_INTERVAL_MS = 100;
const SCENE_INTERVAL_MS = 2000;   // cups + placement grid
const STALE_AFTER_S = 1.5;        // older readings are flagged on screen
const MAX_GAP_S = 1.0;            // never interpolate across a longer gap
const DELAY_LIMITS_S = [0.2, 1.2];// playback delay behind the newest reading
const FINGER_SMOOTHING_PER_S = 10;
// Gateway gripper position is the travel of EACH finger in mm (0 closed .. 25),
// as handover_plan uses it: finger gap = closed gap + 2 * position.
const FINGER_M_PER_MM = 0.001;
const CUP_HEIGHT_M = 0.095;
const CUP_DEFAULT_BASE_M = 0.07;    // base diameter when the scan has none
const CUP_RIM_SCALE = 1.25;        // rim radius / base radius
// A held cup sits centred between the fingers, its end 20 mm into them:
// cup centre this far out along the gripper axis (fingertips at 0.136 m).
const CUP_GRIP_CENTRE_M = 0.136 - 0.020 + CUP_HEIGHT_M / 2;
const CELL_SIZE_M = 0.074;         // 80 mm grid pitch, drawn with a small gap
const CAMERA_HOME = { position: [1.55, -0.95, 0.85], target: [0.32, 0, 0.1] };
const BASE_FOV = 40;          // vertical, degrees, on panels at least WIDE_ASPECT wide
const WIDE_ASPECT = 1.1;

const DEG = Math.PI / 180;
const panel = document.getElementById("view-scene");
const host = document.getElementById("scene-canvas-host");
const statusText = document.getElementById("scene-status");
const statusBadge = document.getElementById("scene-badge");
const resetButton = document.getElementById("scene-reset-view");
const cupToggle = document.getElementById("scene-show-cups");
const gridToggle = document.getElementById("scene-show-grid");

let started = false;
let renderer, scene, camera, controls, model, cupLayer, labelLayer, gridLayer, tableZ = 0;
let running = false, poseTimer = null, sceneTimer = null, lastFrame = 0;
let poseSource = "connecting", poseError = null;
let playbackDelay = DELAY_LIMITS_S[0] + 0.15;
const clock = new ClockSync();
const arms = {};   // arm -> { joints: [Object3D x7], track: JointTrack, shown, fingers: [...] }

function setStatus(badge, text, tone) {
  statusBadge.textContent = badge;
  statusBadge.dataset.tone = tone;
  statusText.textContent = text;
}

// ---------------------------------------------------------------- model ----

function geometryFrom(buffer, part) {
  const geometry = new BufferGeometry();
  const n = part.vertices;
  geometry.setAttribute("position", new BufferAttribute(new Float32Array(buffer, part.positions, n * 3), 3));
  geometry.setAttribute("normal", new BufferAttribute(new Int8Array(buffer, part.normals, n * 3), 3, true));
  const Index = part.index_type === "uint16" ? Uint16Array : Uint32Array;
  geometry.setIndex(new BufferAttribute(new Index(buffer, part.indices, part.index_count), 1));
  geometry.computeBoundingSphere();
  return geometry;
}

function meshFrom(buffer, manifest, name) {
  const part = manifest.parts[name];
  const material = new MeshStandardMaterial({ color: new Color(part.color), roughness: 0.55, metalness: 0.05 });
  const mesh = new Mesh(geometryFrom(buffer, part), material);
  mesh.name = name;
  mesh.castShadow = mesh.receiveShadow = true;
  return mesh;
}

function fixedFrame(columnMajor) {
  const frame = new Object3D();
  frame.matrixAutoUpdate = false;
  frame.matrix.fromArray(columnMajor);
  return frame;
}

async function loadModel() {
  const [manifest, buffer] = await Promise.all([
    fetch("/static/3d/yumi_model.json", { credentials: "same-origin" }).then((r) => {
      if (!r.ok) throw new Error(`model manifest: HTTP ${r.status}`); return r.json(); }),
    fetch("/static/3d/yumi_model.bin", { credentials: "same-origin" }).then((r) => {
      if (!r.ok) throw new Error(`model data: HTTP ${r.status}`); return r.arrayBuffer(); }),
  ]);
  const robot = new Group();
  robot.name = "yumi";
  robot.add(meshFrom(buffer, manifest, "body"));
  for (const arm of ["left", "right"]) {
    const spec = manifest.arms[arm];
    const joints = [];
    let parent = robot;
    spec.joints.forEach((origin, index) => {
      const frame = fixedFrame(origin);
      const joint = new Object3D();          // rotates about its local z
      frame.add(joint);
      joint.add(meshFrom(buffer, manifest, spec.links[index]));
      parent.add(frame);
      joints.push(joint);
      parent = joint;
    });
    const mount = fixedFrame(manifest.gripper_mount);
    mount.add(meshFrom(buffer, manifest, "gripper"));
    const fingers = Object.entries(manifest.fingers).map(([name, axis]) => {
      const finger = meshFrom(buffer, manifest, name);
      mount.add(finger);
      return { mesh: finger, axis: new Vector3(...axis), current: 0, target: 0 };
    });
    parent.add(mount);
    arms[arm] = { mount, joints, fingers, track: new JointTrack({ maxGap: MAX_GAP_S }), shown: false };
  }
  robot.visible = false;   // until the first reading: never show a made-up pose
  return { robot, manifest };
}

// Gateway order is rax_1..rax_6 + arm_angle (eax_a); on the YuMi the arm
// angle is physically the third joint, so the kinematic chain is
// rax_1, rax_2, eax_a, rax_3, rax_4, rax_5, rax_6.
function chainAngles(reading) {
  const [a1, a2, a3, a4, a5, a6] = reading.joints;
  return [a1, a2, reading.arm_angle, a3, a4, a5, a6].map((value) => value * DEG);
}

function applyPose(dt) {
  if (!clock.ready) return;
  // Play back MAX(2 readings) behind the newest one so there is always a
  // reading ahead to move towards; adapt slowly to the actual reading rate.
  const intervals = Object.values(arms).map((arm) => arm.track.interval()).filter((v) => v !== null);
  const wanted = intervals.length ? Math.max(...intervals) * 1.8 + 0.04 : DELAY_LIMITS_S[1];
  const target = Math.min(DELAY_LIMITS_S[1], Math.max(DELAY_LIMITS_S[0], wanted));
  playbackDelay += (target - playbackDelay) * Math.min(1, dt * 0.5);
  const renderTime = clock.serverTime(performance.now() / 1000) - playbackDelay;
  const blend = Math.min(1, dt * FINGER_SMOOTHING_PER_S);
  for (const arm of Object.values(arms)) {
    const angles = arm.track.sample(renderTime);
    if (angles) {
      arm.joints.forEach((joint, i) => { joint.rotation.z = angles[i]; });
      arm.shown = true;
    }
    for (const finger of arm.fingers) {
      finger.current += (finger.target - finger.current) * (arm.shown && dt > 0 ? blend : 1);
      finger.mesh.position.copy(finger.axis).multiplyScalar(finger.current);
    }
  }
  model.robot.visible = Object.values(arms).some((arm) => arm.shown);
}

function acceptPose(payload, receivedSeconds) {
  poseSource = payload?.source || "unavailable";
  poseError = payload?.error || null;
  clock.observe(receivedSeconds, payload?.now);
  for (const [name, arm] of Object.entries(arms)) {
    const reading = payload?.arms?.[name];
    if (!reading) continue;
    arm.track.push(reading.t, chainAngles(reading));
    if (typeof reading.gripper === "number") {
      arm.fingers.forEach((finger) => { finger.target = reading.gripper * FINGER_M_PER_MM; });
    }
  }
}

function describePose() {
  if (poseSource === "simulated") {
    setStatus("SIMULATED", "Demo mode: both arms shown at the home pose. No robot is connected.", "info");
    return;
  }
  if (poseSource === "connecting") {
    setStatus("CONNECTING", "Reading joint angles…", "info");
    return;
  }
  const now = clock.ready ? clock.serverTime(performance.now() / 1000) : null;
  const ages = Object.fromEntries(Object.entries(arms).map(([name, arm]) =>
    [name, arm.track.latest && now !== null ? now - arm.track.latest.t : Infinity]));
  const stale = Object.keys(ages).filter((name) => ages[name] > STALE_AFTER_S);
  const busy = poseError ? " The gateway did not answer; the view waits before asking again so it never competes with robot control." : "";
  if (stale.length === 2) {
    const newest = Math.min(ages.left, ages.right);
    setStatus("NO LIVE DATA", Number.isFinite(newest)
      ? `No joint reading for ${newest.toFixed(1)} s. The pose shown is the last one received and may be wrong.${busy}`
      : `The gateway has not returned joint angles yet; the arms are hidden.${busy}`, "warn");
  } else if (stale.length === 1) {
    setStatus("PARTIAL", `No recent reading for the ${stale[0]} arm; it shows its last known pose.${busy}`, "warn");
  } else {
    const rates = Object.values(arms).map((arm) => arm.track.interval()).filter((v) => v);
    const rate = rates.length ? ` About ${(1 / Math.max(...rates)).toFixed(1)} readings/s per arm.` : "";
    setStatus("LIVE", `Joint angles from the robot controller via the gateway.${rate} `
      + `Played back ${playbackDelay.toFixed(2)} s behind for smooth motion.`, "ok");
  }
}

// ---------------------------------------------------------- cell items ----

function labelSprite(text) {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 64;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "rgba(20, 24, 22, 0.82)";
  ctx.beginPath(); ctx.arc(32, 32, 28, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = "#fff"; ctx.font = "bold 30px system-ui, sans-serif";
  ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(text, 32, 34);
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  const sprite = new Sprite(new SpriteMaterial({ map: texture, depthTest: false }));
  sprite.scale.set(0.04, 0.04, 1);
  sprite.renderOrder = 10;
  return sprite;
}

function clearLayer(layer) {
  for (const child of [...layer.children]) {
    child.traverse((node) => {
      node.geometry?.dispose();
      node.material?.map?.dispose();
      node.material?.dispose();
    });
    layer.remove(child);
  }
}

const CUP_COLORS = { available: "#2f8f83", blocked: "#9aa19d", done: "#c9a04a" };
let gridKey = "", cupCapture = null, cupsVisible = true;
// id -> { group, material, label, holder, wanted, pending: [{ at, holder }] }
const cups = new Map();
const scratch = new Vector3();

// A cup in its own frame: +z points from the base to the rim.
function makeMug(baseDiameter, color) {
  const rBase = baseDiameter / 2, rRim = rBase * CUP_RIM_SCALE, h = CUP_HEIGHT_M;
  const material = new MeshStandardMaterial({ color, roughness: 0.45, side: DoubleSide });
  const wall = new Mesh(new CylinderGeometry(rRim, rBase, h, 32, 1, true), material);
  wall.rotation.x = Math.PI / 2;                       // cylinder y axis -> z
  const base = new Mesh(new CircleGeometry(rBase, 32), material);
  base.position.z = -h / 2;
  const group = new Group();
  group.add(wall, base);
  group.traverse((node) => { node.castShadow = true; });
  return { group, material };
}

function settleOnTable(group) {
  // Released or never moved: stand it on the table, rim up or down as it was
  // held, keeping its position and heading.
  const up = scratch.set(0, 0, 1).applyQuaternion(group.quaternion).z >= 0;
  const x = scratch.set(1, 0, 0).applyQuaternion(group.quaternion);
  group.rotation.set(up ? 0 : Math.PI, 0, Math.atan2(x.y, x.x) * (up ? 1 : -1));
  group.position.z = tableZ + CUP_HEIGHT_M / 2;
}

function disposeCup(entry) {
  entry.group.removeFromParent();
  entry.group.traverse((node) => node.geometry?.dispose());
  entry.material.dispose();
  entry.label.removeFromParent();
  entry.label.material.map?.dispose();
  entry.label.material.dispose();
}

function setCupsVisible(visible) {
  cupsVisible = visible;
  for (const entry of cups.values()) entry.group.visible = entry.label.visible = visible;
}

function syncCups(inventory, tagOffset) {
  const [ox, oy] = Array.isArray(tagOffset) && tagOffset.every(Number.isFinite) ? tagOffset : [0, 0];
  const capture = inventory?.capture ?? null;
  if (capture !== cupCapture) {           // a new scan renumbers the cups
    for (const entry of cups.values()) disposeCup(entry);
    cups.clear();
    cupCapture = capture;
  }
  const seen = new Set();
  for (const cup of inventory?.cups || []) {
    if (!["x", "y"].every((k) => Number.isFinite(cup?.robot_mm?.[k]))) continue;
    seen.add(cup.id);
    const done = ["placed", "replaced"].includes(cup.status);
    const color = done ? CUP_COLORS.done : cup.pick_available ? CUP_COLORS.available : CUP_COLORS.blocked;
    const { holder, step } = cupHolder(cup.motion);
    const doneAt = Number(cup.motion?.done_at?.[step]);   // dashboard clock, same as joint readings
    let entry = cups.get(cup.id);
    if (!entry) {
      const diameter = Number.isFinite(cup.diameter) ? cup.diameter / 1000 : CUP_DEFAULT_BASE_M;
      const { group, material } = makeMug(diameter, color);
      // Where the plans grip it: scanned position plus the calibration offset.
      group.position.set((cup.robot_mm.x + ox) / 1000, (cup.robot_mm.y + oy) / 1000, 0);
      group.rotation.set(Math.PI, 0, 0);  // mugs stand upside-down in the tray (CUP BASE seen from above)
      settleOnTable(group);
      cupLayer.add(group);
      const label = labelSprite(String(cup.id));
      labelLayer.add(label);
      entry = { group, material, label, holder: "table", wanted: "table", pending: [] };
      group.visible = label.visible = cupsVisible;
      cups.set(cup.id, entry);
    }
    entry.material.color.set(color);
    if (holder !== entry.wanted) {
      entry.wanted = holder;
      // Applied when the (delayed) arm playback reaches this moment, so the
      // cup leaves the table when the drawn gripper is there, not before.
      entry.pending.push({ at: Number.isFinite(doneAt) ? doneAt : -Infinity, holder });
    }
  }
  for (const [id, entry] of cups) {
    if (!seen.has(id) && entry.holder === "table" && !entry.pending.length) {
      disposeCup(entry);
      cups.delete(id);
    }
  }
}

function moveCup(entry, holder) {
  if (holder === "left" || holder === "right") {
    // Snap into the fingers, centred on the gripper axis. The left arm grips
    // the upturned cup's base from above (rim away from the gripper); the right
    // arm takes it rim-first from below at the handover (rim towards the
    // gripper), so the turnover leaves it upright. Snapping drops any scan or
    // handover offset, so the cup lands on its placement cell.
    arms[holder].mount.add(entry.group);
    entry.group.position.set(0, 0, CUP_GRIP_CENTRE_M);
    entry.group.rotation.set(holder === "left" ? 0 : Math.PI, 0, Math.PI / 2);
  } else if (entry.group.parent !== cupLayer) {
    model.robot.updateMatrixWorld(true);
    cupLayer.attach(entry.group);       // keeps where it was let go
    settleOnTable(entry.group);
  }
  entry.holder = holder;
}

function updateCups() {
  if (!clock.ready) return;
  // The arms are drawn playbackDelay behind; move a cup when that playback
  // reaches the moment the dashboard saw the grip/release step finish.
  const playedBack = clock.serverTime(performance.now() / 1000) - playbackDelay;
  for (const entry of cups.values()) {
    while (entry.pending.length && entry.pending[0].at <= playedBack) moveCup(entry, entry.pending.shift().holder);
    entry.group.getWorldPosition(entry.label.position);
    entry.label.position.z += CUP_HEIGHT_M / 2 + 0.03;
  }
}

const CELL_COLORS = { available: "#7fbf8e", occupied: "#d08b5b", reserved: "#e3c35a", unknown: "#a7aca9" };

function renderGrid(grid, tableZ) {
  const locations = (grid?.locations || []).filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y));
  const key = JSON.stringify(locations.map((p) => [p.id, p.x, p.y, p.occupied, p.reserved, p.vision_blocked]));
  if (key === gridKey) return;
  gridKey = key;
  clearLayer(gridLayer);
  for (const place of locations) {
    const kind = place.vision_blocked ? "unknown" : place.reserved ? "reserved" : place.occupied ? "occupied" : "available";
    const cell = new Mesh(new PlaneGeometry(CELL_SIZE_M, CELL_SIZE_M),
      new MeshStandardMaterial({ color: CELL_COLORS[kind], transparent: true, opacity: 0.75, roughness: 0.9 }));
    cell.position.set(place.x / 1000, place.y / 1000, tableZ + 0.0015);
    cell.receiveShadow = true;
    const label = labelSprite(`P${place.id}`);
    label.scale.set(0.03, 0.03, 1);
    label.position.set(place.x / 1000, place.y / 1000, tableZ + 0.02);
    const group = new Group();
    group.add(cell, label);
    gridLayer.add(group);
  }
}

// ------------------------------------------------------------ polling ----

async function pollPose() {
  if (!running) return;
  try {
    const response = await fetch("/api/robot-pose", { cache: "no-store", credentials: "same-origin" });
    if (response.ok) {
      const payload = await response.json();
      acceptPose(payload, performance.now() / 1000);
    }
  } catch { /* shown as stale below */ }
  describePose();
  if (running) poseTimer = setTimeout(pollPose, POSE_INTERVAL_MS);
}

async function pollScene(tableZ) {
  if (!running) return;
  try {
    const response = await fetch("/api/state", { cache: "no-store", credentials: "same-origin" });
    if (response.ok) {
      const state = await response.json();
      syncCups(state?.inventory, state?.motion?.tag_offset_mm);
      renderGrid(state?.placement_grid, tableZ);
    }
  } catch { /* keep the last cell view */ }
  if (running) sceneTimer = setTimeout(() => pollScene(tableZ), SCENE_INTERVAL_MS);
}

// ------------------------------------------------------------- render ----

function resize() {
  const width = host.clientWidth, height = host.clientHeight;
  if (!width || !height) return;
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  // Keep the horizontal field of view of a wide panel on narrow (phone) screens,
  // so the whole cell stays in frame instead of cropping the robot.
  const wide = Math.tan((BASE_FOV / 2) * DEG) * WIDE_ASPECT;
  camera.fov = camera.aspect >= WIDE_ASPECT ? BASE_FOV
    : Math.min(80, 2 * Math.atan(wide / camera.aspect) / DEG);
  camera.updateProjectionMatrix();
}

function frame(now) {
  if (!running) return;
  const dt = lastFrame ? Math.min(0.1, (now - lastFrame) / 1000) : 0;
  lastFrame = now;
  applyPose(dt);
  updateCups();
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(frame);
}

function resetView() {
  camera.position.set(...CAMERA_HOME.position);
  controls.target.set(...CAMERA_HOME.target);
  controls.update();
}

function setRunning(visible) {
  if (!started || visible === running) return;
  running = visible;
  clearTimeout(poseTimer); clearTimeout(sceneTimer);
  if (running) {
    resize();
    lastFrame = 0;
    requestAnimationFrame(frame);
    pollPose();
    pollScene(model.manifest.table_z);
  }
}

async function start() {
  started = true;
  setStatus("LOADING", "Loading the YuMi model…", "info");
  let canvas;
  try {
    renderer = new WebGLRenderer({ antialias: true });
    canvas = renderer.domElement;
  } catch {
    started = false;
    setStatus("UNAVAILABLE", "This browser cannot show 3D (WebGL is off or unsupported).", "warn");
    return;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = SRGBColorSpace;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = PCFShadowMap;
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", "3D view of the YuMi robot, cups and placement locations");
  host.appendChild(canvas);

  scene = new Scene();
  scene.background = new Color("#eef1ee");
  camera = new PerspectiveCamera(BASE_FOV, 1, 0.01, 20);
  camera.up.set(0, 0, 1);
  controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.maxPolarAngle = Math.PI * 0.495;   // stay above the table
  controls.minDistance = 0.3;
  controls.maxDistance = 4;
  resetView();

  scene.add(new HemisphereLight("#ffffff", "#9a9f9b", 1.1));
  scene.add(new AmbientLight("#ffffff", 0.25));
  const sun = new DirectionalLight("#ffffff", 1.7);
  sun.position.set(1.2, -0.8, 2.2);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  Object.assign(sun.shadow.camera, { left: -1, right: 1, top: 1, bottom: -1, near: 0.5, far: 5 });
  sun.shadow.bias = -0.0004;
  scene.add(sun);

  try {
    model = await loadModel();
  } catch (error) {
    setStatus("UNAVAILABLE", `The robot model could not be loaded (${error.message}). Reopen the tab to retry.`, "warn");
    controls.dispose();
    renderer.dispose();
    canvas.remove();
    started = false;
    return;
  }
  tableZ = model.manifest.table_z;
  const table = new Mesh(new PlaneGeometry(1.8, 1.6), new MeshStandardMaterial({ color: "#f7f7f4", roughness: 0.95 }));
  table.position.set(0.35, 0, tableZ);
  table.receiveShadow = true;
  const grid = new GridHelper(1.6, 16, "#c9cec9", "#dde1dd");
  grid.rotation.x = Math.PI / 2;
  grid.position.set(0.35, 0, tableZ + 0.0005);
  cupLayer = new Group(); gridLayer = new Group(); labelLayer = new Group();
  scene.add(table, grid, model.robot, gridLayer, cupLayer, labelLayer);

  cupToggle?.addEventListener("change", () => setCupsVisible(cupToggle.checked));
  gridToggle?.addEventListener("change", () => { gridLayer.visible = gridToggle.checked; });
  resetButton?.addEventListener("click", resetView);
  new ResizeObserver(resize).observe(host);
  setRunning(!panel.hidden);
}

function onVisibility() {
  const visible = !panel.hidden && document.visibilityState === "visible";
  if (visible && !started) start();
  else setRunning(visible);
}

if (panel && host) {
  new MutationObserver(onVisibility).observe(panel, { attributes: true, attributeFilter: ["hidden"] });
  document.addEventListener("visibilitychange", onVisibility);
  onVisibility();
}
