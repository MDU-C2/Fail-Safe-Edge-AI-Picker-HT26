"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

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
  const source = fs.readFileSync(path.join(__dirname, "web_static/3d/viewer3d.js"), "utf8")
    .replace(/^import\s+[\s\S]*?\sfrom\s+"[^"\n]+";\s*/gm, "");
  vm.runInContext(source, context);
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
