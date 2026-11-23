/* The Mac batch review follows the workspace; selected-cup review stays singular. */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const script = fs.readFileSync(path.join(__dirname, "web_static/app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "web_static/index.html"), "utf8");

function section(start, end) {
  const a = script.indexOf(start), b = script.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `Missing production section: ${start}`);
  return script.slice(a, b);
}

function page() {
  const dom = {};
  for (const [, id] of html.matchAll(/\bid="([^"]+)"/g)) {
    dom[id] = { textContent: "", value: "", style: {}, hidden: false, disabled: false,
      classList: { toggle() {} }, setAttribute() {} };
  }
  const context = vm.createContext({ dom, connected: true, posting: false, stopPosting: false,
    quickPosting: false, localStopInhibit: false, eventsKey: "[]",
    state: { mode: "live", ready: true, busy: false, events: [], motion: { dynamic_batch: true },
      proposal: { action: "goto_all", dynamic_batch: true, cup_ids: [1, 2], cup_id: null,
        capture_id: "scan-1", placements: [{ id: 8 }, { id: 9 }], description: "Follow the current workspace." } },
    text(id, value) { dom[id].textContent = String(value ?? ""); },
    hide(id, hidden) { dom[id].hidden = hidden; }, asMessage(value) { return value || ""; },
    speedText() { return "100 mm/s"; }, accelerationText() { return "50%"; },
    coordinatesText() { return "coordinates"; }, humanize(value) { return value; }, updateCountdown() {},
    actionable() { return true; }, canStop() { return true; }, safeScan() { return true; },
    gridReady() { return true; }, idle() { return true; }, isStop() { return false; },
    pickableCups() { return [{ id: 1 }, { id: 2 }]; }, availableLocations() { return [{ id: 8 }, { id: 9 }]; },
    renderSelectedActions() {}, renderRobotControls() {}, renderSavedCups() {},
    document: { querySelectorAll() { return []; } },
  });
  vm.runInContext(section("  const gatewayRecovery =", "  const actionable =")
    + section("  function renderProposal()", "  function renderSavedCups()")
    + section("  function renderActivity()", "  function renderControls()")
    + section("  function renderControls()", "  function render()")
    + "\nglobalThis.proposal = renderProposal; globalThis.activity = renderActivity; globalThis.controls = renderControls;", context);
  return { dom, context };
}

test("dynamic Pick all review identifies an initial list and authorizes workspace changes", () => {
  const p = page(); p.context.proposal(); p.context.controls();
  assert.equal(p.dom["proposal-cup"].textContent, "Pick all workspace cups");
  assert.equal(p.dom["confirm-button"].textContent, "Confirm Pick all");
  assert.match(p.dom["proposal-context"].textContent, /Initially 2 cups/);
  assert.match(p.dom["proposal-context"].textContent, /moved and newly added cups/);
  assert.match(p.dom["proposal-destinations"].textContent, /^Initial destinations:/);
  assert.match(p.dom["batch-hint"].textContent, /Single-cup actions still pick only one cup/);
});

test("a single selected cup remains a singular review when dynamic batches are enabled", () => {
  const p = page();
  Object.assign(p.context.state.proposal, { action: "goto", dynamic_batch: false, cup_id: 2, cup_ids: [2], placements: [{ id: 8 }] });
  p.context.proposal();
  assert.equal(p.dom["proposal-cup"].textContent, "Cup 2");
  assert.equal(p.dom["confirm-button"].textContent, "Confirm cup 2");
  assert.doesNotMatch(p.dom["proposal-context"].textContent, /newly added|Initially/);
});

test("grid-refresh review explains cell reuse and waiting while source-only review keeps scan evidence", () => {
  const p = page();
  p.context.state.motion.between_cup_check = true;
  p.context.state.motion.between_cup_grid_check = true;
  p.context.proposal(); p.context.controls();
  assert.match(p.dom["proposal-context"].textContent, /camera-cleared cells can be reused/);
  assert.match(p.dom["proposal-context"].textContent, /waits and refreshes automatically/);
  assert.match(p.dom["batch-hint"].textContent, /Grid occupancy also refreshes/);
  p.context.state.proposal.between_cup_grid_check = false;
  p.context.proposal();
  assert.match(p.dom["proposal-context"].textContent, /placement evidence stays from the manual scan/);
  assert.doesNotMatch(p.dom["proposal-context"].textContent, /waits and refreshes automatically|camera-cleared cells/);
});

test("continuous Pick all reviews Home only on an empty workspace while selected picks remain unchanged", () => {
  const p = page();
  Object.assign(p.context.state.motion, { between_cup_check: true, between_cup_grid_check: true, continuous_batch: true });
  p.context.state.proposal.continuous_batch = true;
  p.context.proposal();
  assert.match(p.dom["proposal-context"].textContent, /until Stop/);
  assert.match(p.dom["proposal-context"].textContent, /both arms go Home once and the camera keeps watching/);
  assert.match(p.dom["proposal-context"].textContent, /full grid with cups still waiting does not send the arms Home/);
  assert.match(p.dom["proposal-motion"].textContent, /Home only when the source workspace becomes empty/);
  assert.doesNotMatch(p.dom["proposal-motion"].textContent, /Automatic home off/);
  Object.assign(p.context.state.proposal, { action: "goto", dynamic_batch: false, continuous_batch: false, cup_id: 2, cup_ids: [2], placements: [{ id: 8 }] });
  p.context.proposal();
  assert.equal(p.dom["confirm-button"].textContent, "Confirm cup 2");
  assert.match(p.dom["proposal-motion"].textContent, /Automatic home off/);
  assert.doesNotMatch(p.dom["proposal-context"].textContent, /go Home once|until Stop/);
});

test("empty-workspace Home and cup-watching phases do not show completed cup-step percentages", () => {
  const p = page();
  p.context.state.proposal = null;
  p.context.state.busy = true;
  p.context.state.batch = { state: "homing_when_empty", continuous_batch: true, dynamic_batch: true,
    completed_cups: [1, 2], pending_cups: [], waiting_cup_ids: [] };
  p.context.state.progress = { completed: 14, total: 14, state: "homing_when_empty" };
  p.context.activity();
  assert.equal(p.dom["operation-title"].textContent, "Workspace empty · moving both arms Home");
  assert.equal(p.dom["progress-section"].hidden, true);
  assert.match(p.dom["batch-progress"].textContent, /2 completed · workspace empty · moving both arms Home once/);
  p.context.state.batch.state = "waiting_for_cups";
  p.context.activity();
  assert.equal(p.dom["operation-title"].textContent, "Watching for cups");
  assert.equal(p.dom["progress-section"].hidden, true);
  assert.match(p.dom["operation-description"].textContent, /resumes automatically/);
  assert.match(p.dom["batch-progress"].textContent, /Stop ends the run/);
  p.context.state.batch.completion_reason = "no_pickable_cups";
  p.context.state.batch.arms_home = false;
  p.context.activity();
  assert.equal(p.dom["operation-title"].textContent, "Waiting for a pickable cup");
  assert.match(p.dom["operation-description"].textContent, /Cups are still detected, so the arms stay clear/);
  assert.doesNotMatch(p.dom["operation-description"].textContent, /workspace empty|go Home/i);
  assert.equal(p.dom["progress-section"].hidden, true);
});

test("a full active grid shows queued cups and automatic refresh without a completed progress bar", () => {
  const p = page();
  p.context.state.proposal = null;
  p.context.state.busy = true;
  p.context.state.batch = { state: "waiting_for_grid", dynamic_batch: true, completed_cups: [1],
    cup_id: null, pending_cups: [], waiting_cup_ids: [2, 3], total: 3, completion_reason: "grid_full" };
  p.context.state.progress = { completed: 7, total: 7, state: "checking_workspace", step: "checking_workspace" };
  p.context.activity();
  assert.equal(p.dom["operation-title"].textContent, "Waiting for a clear grid cell");
  assert.match(p.dom["operation-description"].textContent, /picking resumes when a cell is confirmed clear/);
  assert.equal(p.dom["progress-section"].hidden, true);
  assert.equal(p.dom["batch-progress"].textContent, "Waiting for a clear grid cell · 1 completed · 2 cups queued · refreshes automatically");
  Object.assign(p.context.state.batch, { state: "running", current_cup_id: 2, pending_cups: [2], waiting_cup_ids: [] });
  p.context.state.progress = { completed: 7, total: 14, state: "running", step: "approach_cup" };
  p.context.activity();
  assert.equal(p.dom["progress-section"].hidden, false);
  assert.equal(p.dom["progress-fill"].style.width, "50%");
});

test("live progress follows changing pending cups and reports the final reason", () => {
  const p = page();
  p.context.state.proposal = null;
  p.context.state.batch = { dynamic_batch: true, state: "running", current: 2, total: 3,
    cup_id: 4, completed_cups: [1], pending_cups: [4, 5], revision: 2 };
  p.context.state.progress = { completed: 7, total: 21, step: "approach_cup" };
  p.context.activity();
  assert.match(p.dom["batch-progress"].textContent, /1 completed · 2 pending · picking cup 4/);
  assert.equal(p.dom["progress-count"].textContent, "7 / 21");
  Object.assign(p.context.state.batch, { completed_cups: [1, 5], pending_cups: [], total: 2,
    state: "completed", completion_reason: "workspace_empty" });
  p.context.state.progress = { completed: 14, total: 14, state: "completed" };
  p.context.activity();
  assert.equal(p.dom["batch-progress"].textContent, "2 completed · 0 pending · Source workspace empty");
  assert.equal(p.dom["progress-count"].textContent, "14 / 14");
  assert.equal(p.dom["progress-fill"].style.width, "100%");
  for (const [reason, label] of [["grid_full", "No available placement locations remain"], ["no_pickable_cups", "No pickable source cups remain"]]) {
    p.context.state.batch.completion_reason = reason;
    p.context.activity();
    assert.ok(p.dom["batch-progress"].textContent.endsWith(label));
  }
});

test("other runtimes retain their fixed-list batch review", () => {
  const p = page(); p.context.state.proposal.dynamic_batch = false; p.context.proposal();
  assert.equal(p.dom["proposal-cup"].textContent, "Pick 2 cups");
  assert.equal(p.dom["confirm-button"].textContent, "Confirm 2 cups");
  assert.match(p.dom["proposal-context"].textContent, /Cups 1, 2 run in this order/);
});
