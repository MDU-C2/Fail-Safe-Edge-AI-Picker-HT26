/* Offline selected-cup affordance and preview admission checks. */
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
function element() {
  const listeners = {};
  return { textContent: "", hidden: false, disabled: true,
    addEventListener(event, fn) { listeners[event] = fn; }, click() { listeners.click?.(); },
    set innerHTML(_) { throw new Error("Selection guidance must remain plain text"); } };
}
function snapshot(overrides = {}) {
  return { ready: true, busy: false, scan_usable: true, stop_requested: false, recovery_required: false,
    inventory: { capture: "capture-1", cups: [{ id: 2, selectable: true, pick_available: true }] },
    placement_grid: { locations: [{ id: 5, occupied: false, reserved: false, vision_blocked: false }] }, ...overrides };
}
function dashboard(initial = snapshot()) {
  const dom = Object.fromEntries(["selection-tools", "selected-label", "selected-status", "selected-reason", "pick-button", "hover-button"].map((id) => [id, element()]));
  const requests = [];
  const context = vm.createContext({ dom, state: initial, connected: true, posting: false, stopPosting: false,
    localStopInhibit: false, selectedId: null, selectedCapture: null,
    text(id, value) { dom[id].textContent = String(value ?? ""); }, hide(id, hidden) { dom[id].hidden = hidden; },
    post(path, payload) { requests.push({ path, payload: JSON.parse(JSON.stringify(payload)) }); },
  });
  const code = section("  const cups =", "  const releaseActionable =")
    + section("  function selectedActionAllowed(", "  function renderOverlay()")
    + section('  ["hover", "pick"].forEach((action) => dom[', '  dom["save-cup-name"].addEventListener');
  vm.runInContext(code + "\nglobalThis.render = renderSelectedActions;", context);
  return { dom, requests, context, render() { context.render(); },
    select() { context.selectedId = 2; context.selectedCapture = "capture-1"; context.render(); } };
}

test("selected-only picking is visible and disabled before any selection", () => {
  assert.doesNotMatch(/<div id="selection-tools"[^>]*>/.exec(html)[0], /\bhidden\b/);
  assert.match(html, /id="pick-button"[^>]*\bdisabled>Pick selected cup<\/button>/);
  const page = dashboard(); page.render();
  assert.equal(page.dom["selection-tools"].hidden, false);
  assert.equal(page.dom["pick-button"].disabled, true);
  assert.equal(page.dom["hover-button"].disabled, true);
  assert.match(page.dom["selected-reason"].textContent, /Select one cup/);
  page.dom["pick-button"].disabled = false; page.dom["pick-button"].click();
  assert.equal(page.requests.length, 0);
});

test("absent and invalidated scans require a fresh scan, even with a prior selection", () => {
  for (const state of [snapshot({ inventory: null, scan_usable: false }), snapshot({ scan_usable: false })]) {
    const page = dashboard(state); page.select();
    assert.equal(page.dom["selection-tools"].hidden, false);
    assert.match(page.dom["selected-reason"].textContent, /Scan workspace, then select a cup/);
    for (const action of ["pick", "hover"]) {
      assert.equal(page.dom[`${action}-button`].disabled, true);
      page.dom[`${action}-button`].disabled = false; page.dom[`${action}-button`].click();
    }
    assert.equal(page.requests.length, 0);
  }
});

test("one fresh selected cup requests only its preview, never a batch or confirmation", () => {
  const page = dashboard(); page.select();
  assert.match(page.dom["selected-label"].textContent, /Cup 2 selected/);
  assert.match(page.dom["selected-reason"].textContent, /Only this cup/);
  assert.equal(page.dom["pick-button"].disabled, false);
  page.dom["pick-button"].click();
  assert.deepEqual(page.requests, [{ path: "/api/select", payload: { capture_id: "capture-1", cup_id: 2, action: "goto" } }]);
});

test("state changes between render and click cannot reuse stale permission", () => {
  const changes = [
    (page) => { page.context.state.scan_usable = false; },
    (page) => { page.context.state.inventory.capture = "capture-2"; },
    (page) => { page.context.state.inventory.cups[0].selectable = false; },
    (page) => { page.context.state.inventory.cups[0].pick_available = false; },
    (page) => { page.context.state.placement_grid.locations[0].reserved = true; },
    (page) => { page.context.state.proposal = { action: "goto", cup_id: 2 }; },
    (page) => { page.context.state.busy = true; },
    (page) => { page.context.state.recovery_required = true; },
    (page) => { page.context.state.stop_requested = true; },
    (page) => { page.context.state.devices = { switching: true }; },
    (page) => { page.context.connected = false; },
  ];
  for (const change of changes) {
    const page = dashboard(); page.select(); change(page);
    page.dom["pick-button"].click(); assert.equal(page.requests.length, 0);
    page.render(); assert.equal(page.dom["pick-button"].disabled, true);
    assert.ok(page.dom["selected-reason"].textContent.length > 0);
  }
});

test("placement shortage blocks picking but does not disable valid hover", () => {
  const page = dashboard(snapshot({ placement_grid: { locations: [{ id: 5, occupied: false, reserved: true }] } }));
  page.select();
  assert.equal(page.dom["pick-button"].disabled, true);
  assert.equal(page.dom["hover-button"].disabled, false);
  assert.match(page.dom["selected-reason"].textContent, /No placement location is available/);
  page.dom["hover-button"].click();
  assert.deepEqual(page.requests.map((request) => request.payload.action), ["hover"]);
});

test("a blocked detection retains its plain-text explanation", () => {
  const page = dashboard();
  page.context.state.inventory.cups[0] = { id: 2, selectable: false, pick_available: true, blocked_reason: "<b>Object position unavailable</b>" };
  page.select();
  assert.equal(page.dom["selected-reason"].textContent, "<b>Object position unavailable</b>");
  assert.equal(page.dom["pick-button"].disabled, true);
  assert.equal(page.dom["hover-button"].disabled, true);
});
