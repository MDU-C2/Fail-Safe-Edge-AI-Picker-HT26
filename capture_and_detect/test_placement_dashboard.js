/* Run with node --test capture_and_detect/test_placement_dashboard.js. Offline only. */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "web_static/app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "web_static/index.html"), "utf8");
const helpers = script.slice(script.indexOf("  const placementLocations ="), script.indexOf("  const releaseActionable ="));
const start = script.indexOf("  function placementCupSummary(");
const end = script.indexOf("\n  function renderProposal()", start);
assert.ok(start > 0 && end > start);

function element() {
  return {
    textContent: "", className: "", title: "", children: [], attributes: {},
    classList: { toggle() {} },
    replaceChildren() { this.children = []; },
    append(...items) { this.children.push(...items); },
    setAttribute(key, value) { this.attributes[key] = value; },
    set innerHTML(_) { throw new Error("Placement evidence must remain plain text"); },
  };
}

function dashboard() {
  const dom = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], element()]));
  const context = vm.createContext({
    dom, state: null, connected: true, placementsKey: "",
    document: { createElement: element },
    text(id, value) { assert.ok(dom[id], `Missing dashboard element ${id}`); dom[id].textContent = String(value ?? ""); },
    asMessage(value) { return String(value ?? ""); },
    clockText() { return ""; },
    humanize(value) { return value.charAt(0).toUpperCase() + value.slice(1); },
  });
  vm.runInContext(helpers + script.slice(start, end) + "\nglobalThis.render = renderPlacements;", context);
  return {
    dom,
    render(observation, locations = [2, 3, 9].map((id) => ({ id, occupied: true }))) {
      context.state = { mode: "live", inventory: { capture: "saved-scan", placement_observation: observation },
        placement_grid: { fingerprint: "unchanged", locations } };
      context.render();
    },
    slot(id) { return dom["placement-grid"].children.find((item) => item.children[0].textContent === String(id)); },
  };
}

function observation(overrides = {}) {
  return {
    frame_count: 3, applied: true, message: "Placement grid checked.", released_ids: [],
    cup_summary: { assigned_count: 2, location_ids: [2, 3], min_assigned_count: 2,
      max_assigned_count: 2, stable_cell_counts: true },
    slots: [
      { id: 2, state: "occupied", cup_count: 1, evidence: ["cup"], reason: "Cup assigned to P2." },
      { id: 3, state: "occupied", cup_count: 1, evidence: ["cup", "raised_depth"],
        assigned_cups: [{ object_indices: [7, 12, 14] }], reason: "Cup assigned to P3." },
      { id: 9, state: "occupied", cup_count: 0, evidence: ["raised_depth"], reason: "Raised depth remains outside assigned cup masks." },
    ], ...overrides,
  };
}

test("two assigned cup groups are distinct from a third depth-only block", () => {
  const page = dashboard();
  page.render(observation());
  assert.match(page.dom["placement-observation"].textContent, /Last frame: 2 cups confidently located in 2 cells: P2, P3\./);
  assert.equal(page.slot(3).children[2].textContent, "Camera: 1 cup");
  assert.equal(page.slot(9).children[2].textContent, "Camera: depth check");
  assert.match(page.slot(9).title, /Raised depth remains outside assigned cup masks/);
  assert.match(page.slot(9).attributes["aria-label"], /Camera: depth check/);
  assert.equal(page.dom["placement-summary"].textContent, "0 available · 3 occupied · 0 reserved · 0 unknown / blocked");
});

test("changed per-cell assignments retain earlier evidence even with equal total counts", () => {
  const page = dashboard();
  const data = observation();
  data.cup_summary.stable_cell_counts = false;
  data.slots[2] = { id: 9, state: "occupied", cup_count: 0, evidence: ["cup", "raised_depth"], reason: "Cup seen in an earlier frame." };
  page.render(data);
  assert.match(page.dom["placement-observation"].textContent, /Cup assignments changed across the scan\. Blocks from all frames are retained\./);
  assert.equal(page.slot(9).children[2].textContent, "Camera: cup seen earlier");
  assert.doesNotMatch(page.dom["placement-observation"].textContent, /2–2/);
  data.cup_summary.min_assigned_count = 1;
  page.render(data);
  assert.match(page.dom["placement-observation"].textContent, /1–2 confidently assigned cups/);
});

test("multiple cups in one cell are not counted as multiple owning cells", () => {
  const page = dashboard();
  const data = observation();
  Object.assign(data.cup_summary, { assigned_count: 3, min_assigned_count: 3, max_assigned_count: 3 });
  data.slots[0].cup_count = 2;
  page.render(data);
  assert.match(page.dom["placement-observation"].textContent, /3 cups confidently located in 2 cells: P2, P3/);
  assert.equal(page.slot(2).children[2].textContent, "Camera: 2 cups");
});

test("zero confident assignments never claims an empty or available grid", () => {
  const page = dashboard();
  const data = observation({ cup_summary: { assigned_count: 0, location_ids: [], min_assigned_count: 0,
    max_assigned_count: 0, stable_cell_counts: true }, slots: [
      { id: 9, state: "occupied", cup_count: 0, evidence: ["detected_object", "raised_depth"] },
    ] });
  page.render(data, [{ id: 9, occupied: true }]);
  assert.match(page.dom["placement-observation"].textContent, /^No cups confidently assigned to grid cells in the last frame\./);
  assert.equal(page.slot(9).children[2].textContent, "Camera: detected object");
  assert.equal(page.slot(9).children[1].textContent, "Occupied");
  assert.match(page.dom["placement-summary"].textContent, /^0 available/);
});

test("camera summary cannot override reservations, blocks, or the saved availability", () => {
  const page = dashboard();
  const data = observation({ slots: [{ id: 2, state: "clear", cup_count: 0, evidence: [] },
    { id: 3, state: "clear", cup_count: 0, evidence: [] }, { id: 9, state: "occupied", cup_count: 0, evidence: ["raised_depth"] }] });
  const locations = [{ id: 2, occupied: true, reserved: true }, { id: 3, occupied: false, vision_blocked: true }, { id: 9, occupied: false }];
  const before = JSON.stringify(locations);
  page.render(data, locations);
  assert.equal(page.slot(2).children[1].textContent, "Reserved");
  assert.equal(page.slot(3).children[1].textContent, "Blocked");
  assert.equal(page.slot(9).children[1].textContent, "Available");
  assert.equal(page.dom["placement-summary"].textContent, "1 available · 0 occupied · 1 reserved · 1 unknown / blocked");
  assert.equal(JSON.stringify(locations), before);
});

test("legacy observations retain their camera state without inventing cup counts", () => {
  const page = dashboard();
  const legacy = { applied: false, message: "Earlier saved scan.", released_ids: [],
    slots: [{ id: 9, state: "occupied", reason: "Old evidence." }] };
  page.render(legacy, [{ id: 9, occupied: true }]);
  assert.equal(page.dom["placement-observation"].textContent, "Earlier saved scan. Saved location status was not updated.");
  assert.equal(page.slot(9).children[2].textContent, "Camera: occupied");
  for (const summary of [null, {}, { assigned_count: "2", location_ids: [2, 3] },
    { assigned_count: 2, location_ids: [2, 2] }, { assigned_count: 1, location_ids: [] }]) {
    page.render({ ...legacy, cup_summary: summary }, [{ id: 9, occupied: true }]);
    assert.doesNotMatch(page.dom["placement-observation"].textContent, /confidently/);
  }
});

test("camera reasons remain plain text and singular assignment wording is accurate", () => {
  const page = dashboard();
  const data = observation({ cup_summary: { assigned_count: 1, location_ids: [2], min_assigned_count: 1,
    max_assigned_count: 1, stable_cell_counts: true } });
  data.slots[0].reason = "<img src=x onerror=alert(1)>";
  page.render(data);
  assert.match(page.dom["placement-observation"].textContent, /1 cup confidently located in 1 cell: P2\./);
  assert.ok(page.slot(2).title.includes(data.slots[0].reason));
  assert.ok(page.slot(2).attributes["aria-label"].includes(data.slots[0].reason));
});
