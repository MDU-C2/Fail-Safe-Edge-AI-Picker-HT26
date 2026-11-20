/* Explicit scans can reconcile grid state after failure without enabling motion. */
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

function dashboard() {
  const requests = [], dom = {};
  for (const [, id] of html.matchAll(/\bid="([^"]+)"/g)) {
    const listeners = {};
    dom[id] = { textContent: "", value: "goto 1", checked: false, disabled: true,
      addEventListener(event, fn) { listeners[event] = fn; },
      click() { listeners.click?.(); } };
  }
  const context = vm.createContext({
    dom, connected: true, posting: false, stopPosting: false, quickPosting: false,
    localStopInhibit: false, selectedId: null, selectedCapture: null,
    state: { ready: true, busy: false, mode: "live", recovery_required: true,
      stop_requested: false, scan_usable: true, proposal: null,
      inventory: { capture: "scan-1", cups: [{ id: 1, selectable: true, pick_available: true }] },
      placement_grid: { locations: [{ id: 1, occupied: false }] } },
    document: { querySelectorAll() { return []; } },
    text(id, value) { dom[id].textContent = String(value ?? ""); },
    hide(id, hidden) { dom[id].hidden = hidden; },
    renderSelectedActions() {}, updateCountdown() {}, renderRobotControls() {}, renderSavedCups() {},
    post(url) { requests.push(url); },
  });
  vm.runInContext(section("  const cups =", "  const releaseActionable =")
    + section("  function renderControls()", "  function render()")
    + section('  dom["scan-button"].addEventListener', '  dom["command-form"].addEventListener')
    + "\nglobalThis.render = renderControls;", context);
  return { dom, requests, context, render() { context.render(); } };
}

test("explicit scan remains available during recovery while motion stays blocked", () => {
  const page = dashboard();
  page.render();
  assert.equal(page.dom["scan-button"].disabled, false);
  assert.equal(page.dom["start-button"].disabled, true);
  assert.equal(page.dom["batch-button"].disabled, true);
  assert.equal(page.dom["command-button"].disabled, true);
  page.dom["scan-button"].click();
  assert.deepEqual(page.requests, ["/api/scan"]);
  assert.equal(page.context.state.recovery_required, true);
});

test("busy, Stop, switching and disconnected states keep the scan button disabled", () => {
  const changes = [
    (page) => { page.context.state.busy = true; },
    (page) => { page.context.state.stop_requested = true; },
    (page) => { page.context.localStopInhibit = true; },
    (page) => { page.context.stopPosting = true; },
    (page) => { page.context.posting = true; },
    (page) => { page.context.state.ready = false; },
    (page) => { page.context.state.devices = { switching: true }; },
    (page) => { page.context.connected = false; },
  ];
  for (const change of changes) {
    const page = dashboard();
    page.render();
    change(page);
    page.render();
    assert.equal(page.dom["scan-button"].disabled, true);
  }
});
