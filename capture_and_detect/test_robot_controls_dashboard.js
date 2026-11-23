/* Run with node --test capture_and_detect/test_robot_controls_dashboard.js. Offline only. */
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
  return {
    textContent: "", value: "", hidden: false, disabled: false, checked: false, title: "", dataset: {},
    classList: { toggle() {} }, addEventListener(name, listener) { listeners[name] = listener; },
    click() { return listeners.click?.({ preventDefault() {} }); },
    focus() {}, scrollIntoView() {},
    set innerHTML(_) { throw new Error("Capability text must never become HTML"); },
  };
}
function capabilities(overrides = {}) {
  return Object.fromEntries(["home", "open", "grip", "robot_status", "stop"]
    .map((action) => [action, { supported: true, reason: "Available", ...overrides[action] }]));
}
function snapshot(overrides = {}) {
  return { ready: true, busy: false, mode: "live", recovery_required: false, stop_requested: false,
    csrf_token: "local-test", instance_id: "instance-1", motion: { speed: 250, home_speed: 200, flip_speed: 100 },
    robot_controls: { capabilities: capabilities(), status: null, proposal: null, home_speed: 100 }, ...overrides };
}
function dashboard() {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
  assert.equal(new Set(ids).size, ids.length, "Markup IDs must remain unique");
  const dom = Object.fromEntries(ids.map((id) => [id, element()]));
  const buttons = [...html.matchAll(/<button\b([^>]*data-robot-action="([^"]+)"[^>]*)>([^<]*)<\/button>/g)]
    .map((m) => { const el = element(); el.dataset.robotAction = m[2];
      el.dataset.arm = /data-arm="([^"]+)"/.exec(m[1])?.[1]; el.textContent = m[3];
      el.hidden = /\bhidden\b/.test(m[1]); el.disabled = /\bdisabled\b/.test(m[1]); return el; });
  assert.equal(buttons.length, 9);
  const requests = [];
  const context = vm.createContext({
    dom, state: snapshot(), connected: true, posting: false, stopPosting: false, localStopInhibit: false,
    speedDirty: false, deviceSpeedDirty: false, phaseSpeedDirty: false, controlDeadline: 60000, releaseReview: null, quickGeneration: 0, transientError: "",
    document: { activeElement: null, querySelectorAll(selector) { assert.equal(selector, "[data-robot-action]"); return buttons; } },
    text(id, value) { assert.ok(dom[id], `Missing element ${id}`); dom[id].textContent = String(value ?? ""); },
    hide(id, value) { assert.ok(dom[id], `Missing element ${id}`); dom[id].hidden = value; },
    speedText(value) { return `${value} mm/s`; }, humanize(value) { return String(value); },
    clockText() { return "12:00:00"; }, asMessage(value) { return String(value ?? ""); },
    renderHandoverControls() {}, renderReleaseLocations() {}, releaseActionable() { return true; },
    cancelReleaseReview() {}, readState: async () => {},
    performance: { now: () => 1000 }, crypto: { randomUUID: () => "test-request" },
    AbortController, setTimeout: () => 1, clearTimeout() {},
    window: { matchMedia: () => ({ matches: true }) },
    fetch: async (url, options) => { requests.push({ url, body: JSON.parse(options.body) });
      return { ok: true, json: async () => ({ accepted: true }) }; },
  });
  const code = section("  const idle =", "  const isStop =")
    + section("  // Discovery describes the gateway", "\n  function cancelReleaseReview(")
    + section("  function updateControlCountdown()", "\n  function updateCountdown()")
    + section("  async function post(", "\n  async function showQuick(")
    + section('  dom["robot-status-button"].addEventListener', '  dom["camera-image"].addEventListener');
  vm.runInContext(code + "\nglobalThis.render = renderRobotControls; globalThis.send = post; globalThis.stopAllowed = canStop;", context);
  return { dom, buttons, requests, context,
    render(state = snapshot(), connected = true) { context.state = state; context.connected = connected; context.render(); },
    button(action, arm) { return buttons.find((el) => el.dataset.robotAction === action && el.dataset.arm === arm); },
    async settle() { await new Promise((resolve) => setImmediate(resolve)); },
  };
}

test("missing capabilities hide unsupported motion controls", async () => {
  const page = dashboard();
  for (const map of [undefined, null, [], {}]) {
    page.render(snapshot({ robot_controls: { capabilities: map } }));
    assert.ok(page.buttons.every((el) => el.hidden && el.disabled));
    assert.equal(page.dom["robot-status-button"].hidden, false);
    assert.match(page.dom["robot-capability-notice"].textContent, /Only supported/);
    await page.context.send("/api/control_preview", { action: "home", arm: "left" });
  }
  assert.equal(page.requests.length, 0);
});

test("only explicitly supported original gateway controls appear", () => {
  const page = dashboard();
  for (const support of [false, undefined, null, "true", 1]) {
    page.render(snapshot({ robot_controls: { capabilities: capabilities({ home: { supported: support }, grip: { supported: support } }) } }));
    assert.ok(page.buttons.filter((el) => ["home", "grip"].includes(el.dataset.robotAction)).every((el) => el.hidden && el.disabled));
    assert.ok(page.buttons.filter((el) => el.dataset.robotAction === "open").every((el) => !el.hidden && !el.disabled));
  }
});

test("failed discovery remains visible while reviewed Home is available", async () => {
  const page = dashboard();
  const reason = "Gateway capability read failed: connection unavailable.";
  page.render(snapshot({ robot_controls: { capabilities: capabilities({ robot_status: { supported: false, reason } }) } }));
  assert.ok(page.dom["robot-capability-notice"].textContent.includes(reason));
  page.button("home", "right").click(); await page.settle();
  assert.equal(page.requests.length, 1);
  assert.equal(page.requests[0].url, "/api/control_preview");
  assert.equal(page.requests[0].body.action, "home");
  assert.equal(page.requests[0].body.arm, "right");
});

test("removed and raw actions cannot request previews even if advertised", async () => {
  const page = dashboard(); const state = snapshot();
  for (const action of ["raw_move", "rapid_reset", "rapid_start", "collision_ack"]) {
    state.robot_controls.capabilities[action] = { supported: true };
    page.render(state);
    await page.context.send("/api/control_preview", { action, arm: "left" });
  }
  assert.equal(page.requests.length, 0);
  assert.doesNotMatch(html, /data-robot-action="(?:rapid_reset|rapid_start|collision_ack)"/);
  assert.doesNotMatch(html, /id="robot-collision-state"/);
});

test("support withdrawn after preview blocks confirmation", async () => {
  const page = dashboard(); const state = snapshot();
  state.robot_controls.proposal = { action: "home", arm: "left", token: "reviewed", description: "Home left." };
  state.robot_controls.capabilities.home.supported = false;
  page.dom["robot-control-ack"].checked = true; page.render(state);
  assert.equal(page.dom["robot-control-confirm"].disabled, true);
  assert.equal(page.dom["robot-control-confirm"].hidden, true);
  assert.equal(page.dom["robot-control-ack"].checked, false);
  page.dom["robot-control-confirm"].click(); await page.settle();
  assert.equal(page.requests.length, 0);
});

test("disconnect, busy, recovery and Stop retain action interlocks", () => {
  const page = dashboard();
  for (const state of [snapshot({ busy: true }), snapshot({ recovery_required: true }), snapshot({ stop_requested: true })]) {
    page.render(state); assert.ok(page.buttons.every((el) => !el.hidden && el.disabled));
  }
  page.render(snapshot(), false); assert.ok(page.buttons.every((el) => el.disabled));
});

test("confirmation still requires acknowledgement and an unexpired preview", () => {
  const page = dashboard(); const state = snapshot();
  state.robot_controls.proposal = { action: "home", arm: "left", token: "reviewed", description: "Home left." };
  page.render(state); assert.equal(page.dom["robot-control-confirm"].disabled, true);
  page.dom["robot-control-ack"].checked = true; page.render(state);
  assert.equal(page.dom["robot-control-confirm"].disabled, false);
  page.context.controlDeadline = 0; page.render(state);
  assert.equal(page.dom["robot-control-confirm"].disabled, true);
});

test("read-only status and best-effort Stop remain available without discovery", async () => {
  const page = dashboard(); page.render(snapshot({ busy: true, robot_controls: {} }));
  assert.equal(page.context.stopAllowed(), true);
  await page.context.send("/api/stop");
  page.context.localStopInhibit = false;
  page.render(snapshot({ robot_controls: {} }));
  page.dom["robot-status-button"].click(); await page.settle();
  assert.deepEqual(page.requests.map((request) => request.url), ["/api/stop", "/api/robot_status"]);
});

test("backend descriptions remain plain text", () => {
  const page = dashboard(); const state = snapshot();
  state.robot_controls.proposal = { action: "open", arm: "left", token: "reviewed", description: "<b>Open left gripper.</b>" };
  page.render(state);
  assert.equal(page.dom["robot-control-description"].textContent, "<b>Open left gripper.</b>");
});

test("receiving-alignment adjustment controls are removed", () => {
  assert.doesNotMatch(html, /id="robot-handover-(?:form|x|y|z|apply|reset)"/);
  assert.doesNotMatch(script, /\/api\/handover_offset/);
});
