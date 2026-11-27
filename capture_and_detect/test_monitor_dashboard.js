"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const script = fs.readFileSync(path.join(__dirname, "web_static/app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "web_static/index.html"), "utf8");
function section(start, end) { return script.slice(script.indexOf(start), script.indexOf(end, script.indexOf(start))); }

function harness() {
  const dom = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, {
    textContent: "", hidden: false, disabled: false, classList: { toggle() {} }, querySelector() { return {}; },
  }]));
  const state = { read_only: true, ready: true, busy: false, mode: "monitor", devices: { active_id: "mac" },
    robot_controls: {}, monitor: { gateway_online: true, gateway_status: "live", camera: null } };
  const requests = [];
  const context = vm.createContext({ state, dom, connected: true, posting: false, stopPosting: false,
    text: (id, value) => { assert.ok(dom[id], `Unknown element: ${id}`); dom[id].textContent = value; },
    hide: (id, value) => { dom[id].hidden = value; }, clockText: (value) => value,
    fetch: (...args) => { requests.push(args); throw Error("Monitor must not POST"); },
  });
  vm.runInContext(section("  const idle =", "  const isStop =") +
    section("  function renderMonitoring()", "  function render()") +
    section("  async function post(", "  async function showQuick(") +
    "\nglobalThis.renderMonitor = renderMonitoring; globalThis.isIdle = idle; globalThis.mayStop = canStop; globalThis.send = post;", context);
  return { context, state, dom, requests };
}

test("monitor blocks commands even if old controller readiness is present", async () => {
  const page = harness();
  assert.equal(page.context.isIdle(), false);
  assert.equal(page.context.mayStop(), false);
  for (const path of ["/api/scan", "/api/stop", "/api/recover", "/api/confirm"]) await page.context.send(path);
  assert.deepEqual(page.requests, []);
  page.context.renderMonitor();
  assert.equal(page.dom["stop-button"].disabled, true);
  assert.equal(page.dom["cup-count"].textContent, "—", "Unknown detections are not zero cups");
});

test("monitor labels gateway failure, rejected credentials and stale camera frames", () => {
  const page = harness();
  page.state.monitor.camera = { stale: true, received_at: "receipt-time", age_seconds: 12 };
  page.context.renderMonitor();
  assert.equal(page.dom["scan-state"].textContent, "Last known frame");
  page.state.monitor.camera.stale = false;
  page.context.renderMonitor();
  assert.equal(page.dom["scan-state"].textContent, "Recent camera frame");
  page.state.monitor.gateway_online = false;
  page.context.renderMonitor();
  assert.equal(page.dom["connection-text"].textContent, "Gateway unavailable");
  page.state.monitor.gateway_status = "auth_error";
  page.context.renderMonitor();
  assert.equal(page.dom["operation-title"].textContent, "Diagnostics access required");
  page.context.connected = false;
  page.context.renderMonitor();
  assert.equal(page.dom["scan-state"].textContent, "Last known frame");
});
