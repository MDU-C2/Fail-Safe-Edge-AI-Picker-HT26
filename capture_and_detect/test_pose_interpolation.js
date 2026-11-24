/* 3D view playback: smooth, never overshooting, never inventing motion. Run with node --test. */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const load = () => import(pathToFileURL(path.join(__dirname, "web_static/3d/pose_interpolation.js")).href);

test("device changes and controller restarts cannot mix clocks", async () => {
  const { PoseSource } = await load();
  const source = new PoseSource();
  const scene = { devices: { active_id: "mac" }, device_epoch: "epoch1", pose_clock_id: "worker1", instance_id: "dashboard" };
  const pose = { device_id: "mac", device_epoch: "epoch1", clock_id: "worker1" };
  assert.equal(source.accepts(pose), false);
  assert.equal(source.scene(scene), true);
  assert.equal(source.accepts(pose), true);
  assert.equal(source.scene(scene), false);
  assert.equal(source.scene({ ...scene, devices: { active_id: "pi" }, device_epoch: "epoch2", pose_clock_id: "worker2" }), true);
  assert.equal(source.accepts(pose), false);
  assert.equal(source.accepts({ device_id: "pi", device_epoch: "epoch2", clock_id: "worker2" }), true);
  assert.equal(source.scene({ ...scene, pose_clock_id: "restarted" }), true);
  assert.equal(source.accepts(pose), false);
});

test("direct dashboard uses its own instance clock", async () => {
  const { PoseSource } = await load();
  const source = new PoseSource();
  source.scene({ instance_id: "direct" });
  assert.equal(source.accepts({ clock_id: "direct" }), true);
  assert.equal(source.accepts({ clock_id: "other" }), false);
});

test("malformed joint samples cannot corrupt interpolation", async () => {
  const { JointTrack } = await load();
  const track = new JointTrack();
  for (const q of [null, [], [Infinity], [1, NaN], "bad"]) assert.equal(track.push(1, q), false);
  assert.equal(track.push(1, [2, 3]), true);
  assert.equal(track.push(2, [4]), false);
  assert.deepEqual(track.sample(2), [2, 3]);
});

test("passes through every reading and holds before the first and after the last", async () => {
  const { JointTrack } = await load();
  const track = new JointTrack();
  assert.equal(track.sample(1), null);
  [[0, 0], [0.2, 10], [0.4, 30], [0.6, 30]].forEach(([t, q]) => track.push(t, [q, -q]));
  for (const [t, q] of [[0, 0], [0.2, 10], [0.4, 30], [0.6, 30]]) assert.deepEqual(track.sample(t), [q, -q]);
  assert.deepEqual(track.sample(-5), [0, -0]);
  assert.deepEqual(track.sample(9), [30, -30]);   // no extrapolation
});

test("moves continuously with no overshoot when the arm stops", async () => {
  const { JointTrack } = await load();
  const track = new JointTrack();
  const readings = [[0, 0], [0.2, 10], [0.4, 20], [0.6, 30], [0.8, 30], [1.0, 30]];
  readings.forEach(([t, q]) => track.push(t, [q]));
  let previous = track.sample(0)[0], biggestStep = 0;
  for (let t = 0; t <= 1.0; t += 0.005) {
    const [q] = track.sample(t);
    assert.ok(q >= -1e-9 && q <= 30 + 1e-9, `overshoot ${q} at ${t}`);
    assert.ok(q >= previous - 1e-9, `moved backwards at ${t}`);
    biggestStep = Math.max(biggestStep, q - previous);
    previous = q;
  }
  assert.ok(biggestStep < 0.5, `jumpy playback, step ${biggestStep}`);   // 60 fps-ish steps, no stair-stepping
  for (let t = 0.6; t <= 1.0; t += 0.01) assert.equal(Math.round(track.sample(t)[0] * 1e9) / 1e9, 30);
});

test("constant speed plays back at constant speed", async () => {
  const { JointTrack } = await load();
  const track = new JointTrack();
  for (let i = 0; i <= 10; i++) track.push(i * 0.25, [i * 5]);
  for (let t = 0.25; t <= 2.25; t += 0.01) assert.ok(Math.abs(track.sample(t)[0] - t * 20) < 1e-9);
});

test("holds then jumps across missed readings instead of inventing motion", async () => {
  const { JointTrack } = await load();
  const track = new JointTrack({ maxGap: 1.0 });
  track.push(0, [0]); track.push(0.2, [5]); track.push(5.2, [90]); track.push(5.4, [95]);
  for (let t = 0.2; t < 5.2; t += 0.1) assert.equal(track.sample(t)[0], 5);
  assert.equal(track.sample(5.2)[0], 90);
});

test("ignores repeated and out-of-order readings and reports the reading interval", async () => {
  const { JointTrack } = await load();
  const track = new JointTrack();
  assert.equal(track.push(1, [1]), true);
  assert.equal(track.push(1, [2]), false);
  assert.equal(track.push(0.5, [2]), false);
  assert.equal(track.push(NaN, [2]), false);
  assert.equal(track.interval(), null);
  for (let i = 1; i <= 6; i++) track.push(1 + i * 0.3, [i]);
  assert.ok(Math.abs(track.interval() - 0.3) < 1e-9);
});

test("clock sync keeps the lowest-latency offset", async () => {
  const { ClockSync } = await load();
  const clock = new ClockSync();
  assert.equal(clock.ready, false);
  clock.observe(100.30, 10.0);   // 0.30 s delay
  clock.observe(100.55, 10.5);   // 0.05 s delay: best estimate
  clock.observe(101.20, 11.0);
  assert.ok(Math.abs(clock.serverTime(102) - 11.95) < 1e-9);
});
