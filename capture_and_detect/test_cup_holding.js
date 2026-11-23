/* 3D view: which gripper holds a cup, from the finished pick steps. Run with node --test. */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const load = () => import(pathToFileURL(path.join(__dirname, "web_static/3d/cup_holding.js")).href);

const HANDOVER = ["right_open_start", "left_open", "approach_cup", "grip_cup", "right_via", "left_handover",
  "right_beside", "right_open", "right_take", "right_grip", "left_release", "left_clear", "right_lower",
  "right_retreat", "left_park", "right_place", "right_release", "right_clear"];

test("handover pick: table, left, right at the handover grip, then released at the placement", async () => {
  const { cupHolder } = await load();
  const seen = [];
  for (let i = 0; i <= HANDOVER.length; i++) {
    const { holder, step } = cupHolder({ completed_steps: HANDOVER.slice(0, i) });
    if (seen.at(-1)?.holder !== holder) seen.push({ holder, step });
  }
  assert.deepEqual(seen, [{ holder: "table", step: null }, { holder: "left", step: "grip_cup" },
    { holder: "right", step: "right_grip" }, { holder: "released", step: "right_release" }]);
});

test("steps finishing out of order (parallel arms) still give the right holder", async () => {
  const { cupHolder } = await load();
  assert.equal(cupHolder({ completed_steps: ["right_open_start", "left_open", "right_via", "approach_cup"] }).holder, "table");
  assert.equal(cupHolder({ completed_steps: ["grip_cup", "right_beside", "left_handover", "right_open", "right_take"] }).holder, "left");
  assert.equal(cupHolder({ completed_steps: ["grip_cup", "right_grip", "left_release", "right_lower", "left_clear"] }).holder, "right");
});

test("left-only lift and replace releases the cup on the table", async () => {
  const { cupHolder } = await load();
  assert.equal(cupHolder({ completed_steps: ["approach_cup", "grip_cup", "lift_cup"] }).holder, "left");
  assert.deepEqual(cupHolder({ completed_steps: ["grip_cup", "lift_cup", "replace_cup", "left_release"] }),
    { holder: "released", step: "left_release" });
});

test("hover-only moves, missing and malformed motion leave the cup on the table", async () => {
  const { cupHolder } = await load();
  for (const motion of [null, undefined, {}, { completed_steps: "grip_cup" }, { completed_steps: ["approach_cup"] }]) {
    assert.equal(cupHolder(motion).holder, "table");
  }
});

test("a stop after the grip keeps the cup in that gripper", async () => {
  const { cupHolder } = await load();
  assert.equal(cupHolder({ state: "interrupted", completed_steps: HANDOVER.slice(0, 5) }).holder, "left");
  assert.equal(cupHolder({ state: "interrupted", completed_steps: HANDOVER.slice(0, 11) }).holder, "right");
});
