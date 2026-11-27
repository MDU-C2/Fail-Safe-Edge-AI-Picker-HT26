"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { reading, queueProgress } = require("./web_static/observer.js");
const snapshot = () => ({status: "live", stale_after: 20, sections: {
  control: {state: "ok", stale: false, age_seconds: 2, data: {active: true, client_id: "secondary"}},
}});

test("fresh owner becomes unknown on server loss or client timeout without losing evidence", () => {
  const state = snapshot();
  assert.equal(reading(state, "control").fresh, true);
  for (const result of [reading(state, "control", 25), reading(state, "control", 0, false),
    reading({...state, status: "offline"}, "control")]) {
    assert.equal(result.fresh, false);
    assert.equal(result.data.client_id, "secondary");
    assert.match(result.stamp, /Last known/);
  }
});
test("missing, denied and unsupported readings never imply a healthy idle robot", () => {
  assert.equal(reading(snapshot(), "queue").fresh, false);
  for (const state of ["auth_error", "unavailable", "unsupported", "skipped"]) {
    const data = snapshot(); data.sections.control.state = state;
    assert.equal(reading(data, "control").fresh, false);
  }
});
test("queue progress counts completed jobs independently of lease ownership", () => {
  const progress = queueProgress({owner: "primary", length: 3, steps: [{state: "done"}, {state: "running"}, {state: "pending"}]});
  assert.equal(progress.completed, 1);
  assert.equal(progress.total, 3);
  assert.ok(Math.abs(progress.percent - 100 / 3) < 1e-9);
  assert.equal(queueProgress({length: 19, step: 8}), null);
  assert.equal(queueProgress({length: 2, steps: [{state: "done"}]}), null);
  assert.equal(queueProgress({length: -1, steps: []}), null);
});
