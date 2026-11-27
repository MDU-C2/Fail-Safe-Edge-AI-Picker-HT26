"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const { pathToFileURL } = require("node:url");

const viewerSource = fs.readFileSync(path.join(__dirname, "web_static/3d/viewer3d.js"), "utf8")
  .replace(/^import\s+[\s\S]*?\sfrom\s+"[^"\n]+";\s*/gm, "");

test("hiding and reopening a 3D tab leaves exactly one animation loop", () => {
  const pending = new Map();
  let next = 0, renders = 0;
  const context = vm.createContext({
    document: { getElementById: () => null },
    ClockSync: class {}, PoseSource: class {}, Vector3: class {},
    requestAnimationFrame: callback => { pending.set(++next, callback); return next; },
    cancelAnimationFrame: id => pending.delete(id),
    clearTimeout: () => {},
    drawn: () => renders++,
  });
  vm.runInContext(viewerSource, context);
  // Isolate the animation scheduler from graphics and HTTP; its real frame()
  // and setRunning() functions still own all RAF scheduling and cancellation.
  vm.runInContext(`started = true; resize = () => {}; pollPose = () => {}; pollScene = () => {};
    applyPose = () => {}; updateCups = () => {}; controls = { update() {} };
    renderer = { render: drawn };`, context);
  for (let cycle = 0; cycle < 10; cycle++) {
    vm.runInContext("setRunning(true)", context);
    assert.equal(pending.size, 1);
    vm.runInContext("setRunning(false)", context);
    assert.equal(pending.size, 0, "hidden tabs must not retain a queued frame");
    vm.runInContext("setRunning(true)", context);
    const [id, callback] = pending.entries().next().value;
    pending.delete(id);
    callback(1000 + cycle * 16);
    assert.equal(pending.size, 1, "one rendered frame schedules exactly one successor");
    vm.runInContext("setRunning(false)", context);
  }
  assert.equal(renders, 10);
});

function fullscreenViewer({ native = false, rejectNative = false, fullscreenEnabled = true } = {}) {
  function element() {
    const classes = new Set(), listeners = new Map();
    return {
      dataset: {}, attributes: {}, focused: false,
      classList: { add: value => classes.add(value), remove: value => classes.delete(value),
        contains: value => classes.has(value) },
      addEventListener: (name, listener) => listeners.set(name, listener),
      dispatch: (name, event = {}) => listeners.get(name)?.(event),
      setAttribute(name, value) { this.attributes[name] = value; },
      focus() { this.focused = true; },
    };
  }
  const ids = Object.fromEntries(["view-scene", "scene-canvas-host", "scene-status", "scene-badge",
    "scene-full-screen", "scene-exit-full", "scene-badge-overlay"].map(id => [id, element()]));
  const scenePanel = element();
  ids["view-scene"].hidden = true; // Exercise controls without starting WebGL or polling.
  ids["scene-canvas-host"].closest = () => scenePanel;
  const document = Object.assign(element(), {
    getElementById: id => ids[id] ?? null,
    fullscreenElement: null, fullscreenEnabled, visibilityState: "visible", body: element(),
  });
  let requested = 0, exited = 0;
  if (native) {
    ids["scene-canvas-host"].requestFullscreen = async () => {
      requested++;
      if (rejectNative) throw new Error("Fullscreen denied");
      document.fullscreenElement = ids["scene-canvas-host"];
      document.dispatch("fullscreenchange");
    };
    document.exitFullscreen = async () => {
      exited++;
      document.fullscreenElement = null;
      document.dispatch("fullscreenchange");
    };
  }
  const context = vm.createContext({ document,
    ClockSync: class {}, PoseSource: class {}, Vector3: class {},
    MutationObserver: class { observe() {} },
  });
  vm.runInContext(viewerSource, context);
  return { context, document, ids, scenePanel, counts: () => ({ requested, exited }) };
}

test("native full screen keeps the live badge and exits through its overlay", async () => {
  const page = fullscreenViewer({ native: true });
  await page.ids["scene-full-screen"].dispatch("click");
  assert.equal(page.document.fullscreenElement, page.ids["scene-canvas-host"]);
  assert.equal(page.ids["scene-full-screen"].attributes["aria-pressed"], "true");
  assert.equal(page.scenePanel.classList.contains("scene-expanded"), false);
  vm.runInContext('setStatus("NO LIVE DATA", "Readings are stale.", "warn")', page.context);
  for (const id of ["scene-badge", "scene-badge-overlay"]) {
    assert.equal(page.ids[id].textContent, "NO LIVE DATA");
    assert.equal(page.ids[id].dataset.tone, "warn");
  }
  page.ids["scene-exit-full"].dispatch("click");
  assert.equal(page.document.fullscreenElement, null);
  assert.equal(page.ids["scene-full-screen"].attributes["aria-pressed"], "false");
  assert.deepEqual(page.counts(), { requested: 1, exited: 1 });
});

test("unavailable or denied full screen fills the window and Escape restores it", async () => {
  for (const options of [{}, { native: true, rejectNative: true }, { native: true, fullscreenEnabled: false }]) {
    const page = fullscreenViewer(options);
    await page.ids["scene-full-screen"].dispatch("click");
    assert.equal(page.scenePanel.classList.contains("scene-expanded"), true);
    assert.equal(page.document.body.classList.contains("scene-expanded-open"), true);
    assert.equal(page.ids["scene-exit-full"].focused, true);
    assert.equal(page.ids["scene-full-screen"].attributes["aria-pressed"], "true");
    page.document.dispatch("keydown", { key: "Escape" });
    assert.equal(page.scenePanel.classList.contains("scene-expanded"), false);
    assert.equal(page.document.body.classList.contains("scene-expanded-open"), false);
    assert.equal(page.ids["scene-full-screen"].focused, true);
    assert.equal(page.ids["scene-full-screen"].attributes["aria-pressed"], "false");
  }
});

test("hiding the 3D tab leaves both native and window-filling full screen", async () => {
  for (const native of [false, true]) {
    const page = fullscreenViewer({ native });
    page.ids["view-scene"].hidden = false;
    await page.ids["scene-full-screen"].dispatch("click");
    page.ids["view-scene"].hidden = true;
    vm.runInContext("onVisibility()", page.context);
    assert.equal(page.document.fullscreenElement, null);
    assert.equal(page.scenePanel.classList.contains("scene-expanded"), false);
    assert.equal(page.document.body.classList.contains("scene-expanded-open"), false);
    assert.equal(page.ids["scene-full-screen"].attributes["aria-pressed"], "false");
  }
});

test("model loader mounts each arm's fingers and preserves hidden roots until telemetry", async () => {
  const assetPath = name => path.join(__dirname, "web_static/3d", name);
  const three = await import(pathToFileURL(assetPath("three-bundle.min.js")).href);
  const manifest = JSON.parse(fs.readFileSync(assetPath("yumi_model.json"), "utf8"));
  const data = fs.readFileSync(assetPath("yumi_model.bin"));
  const buffer = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
  const context = vm.createContext({ ...three,
    document: { getElementById: () => null },
    ClockSync: class {}, PoseSource: class {}, JointTrack: class {},
    fetch: async url => ({ ok: true, json: async () => manifest, arrayBuffer: async () => {
      assert.equal(url, "/static/3d/yumi_model.bin"); return buffer;
    } }),
  });
  vm.runInContext(viewerSource, context);
  const model = await vm.runInContext("loadModel()", context);
  assert.equal(model.robot.visible, false);
  for (const name of ["left", "right"]) {
    const arm = vm.runInContext(`arms.${name}`, context);
    assert.equal(arm.root.visible, false);
    assert.equal(arm.shown, false);
    assert.equal(arm.joints.length, 7);
    assert.equal(arm.fingers.length, 2);
    assert.equal(arm.gripCentre, manifest.grippers[name].cup_grip_centre_m);
    for (let index = 0; index < arm.fingers.length; index++) {
      const finger = arm.fingers[index], spec = manifest.grippers[name].fingers[index];
      assert.equal(finger.mesh.name, spec.part);
      assert.equal(finger.mesh.parent.parent, arm.mount);
      assert.deepEqual(finger.mesh.parent.matrix.toArray(), spec.matrix);
      const direction = finger.axis.clone().transformDirection(finger.mesh.parent.matrix);
      const expectedX = index === 0 ? -1 : 1;
      assert.ok(Math.abs(direction.x - expectedX) < 1e-9, "paired jaws open in opposite directions");
    }
  }
});


test("the dashboard exposes one scene and unambiguous fullscreen controls", () => {
  const html = fs.readFileSync(path.join(__dirname, "web_static/index.html"), "utf8");
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(new Set(ids).size, ids.length, "duplicate IDs can wire controls to a hidden scene");
  for (const id of ["view-scene", "scene-canvas-host", "scene-full-screen", "scene-exit-full", "scene-badge-overlay"])
    assert.equal(ids.filter(value => value === id).length, 1, id);
});
