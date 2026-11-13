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
  return Object.fromEntries(["home", "open", "grip", "collision_ack", "rapid_reset", "rapid_start", "robot_status", "stop"]
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
  assert.equal(buttons.length, 12);
  const requests = [];
  const context = vm.createContext({
    dom, state: snapshot(), connected: true, posting: false, stopPosting: false, localStopInhibit: false,
    speedDirty: false, controlDeadline: 60000, releaseReview: null, quickGeneration: 0, transientError: "",
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
    + section("  // Only explicit server capabilities", "\n  function cancelReleaseReview(")
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

test("only explicitly supported actions appear, and local settings remain", () => {
  const page = dashboard();
  const caps = capabilities({ grip: { supported: false }, collision_ack: { supported: false }, rapid_reset: { supported: false } });
  page.render(snapshot({ robot_controls: { capabilities: caps } }));
  for (const button of page.buttons) {
    const supported = caps[button.dataset.robotAction].supported;
    assert.equal(button.hidden, !supported);
    assert.equal(button.disabled, !supported);
  }
  assert.equal(page.dom["robot-home-tool"].hidden, false);
  assert.equal(page.dom["robot-gripper-tool"].hidden, false);
  assert.equal(page.dom["robot-speed-input"].disabled, false);
  assert.equal(page.dom["robot-local-recovery-button"].hidden, false);
  assert.match(page.dom["robot-capability-notice"].textContent, /Only controls supported/);
});

test("missing or malformed capabilities hide hardware actions even in demo", () => {
  const page = dashboard();
  for (const map of [undefined, null, [], {}, { home: { supported: "true" } }, { home: true }]) {
    page.render(snapshot({ mode: "demo", robot_controls: { capabilities: map } }));
    assert.ok(page.buttons.every((el) => el.hidden && el.disabled));
    assert.equal(page.dom["robot-home-tool"].hidden, true);
    assert.equal(page.dom["robot-gripper-tool"].hidden, true);
    assert.equal(page.dom["robot-speed-input"].disabled, false);
  }
  page.render(snapshot({ mode: "demo" }));
  assert.ok(page.buttons.every((el) => !el.hidden && !el.disabled));
});

test("hardware actions are hidden before the first state arrives", () => {
  const page = dashboard();
  assert.ok(page.buttons.every((el) => el.hidden && el.disabled));
});

test("unsupported clicks cannot send previews even after DOM tampering", async () => {
  const page = dashboard();
  page.render(snapshot({ robot_controls: { capabilities: {} } }));
  const button = page.button("home", "left");
  button.disabled = false; button.hidden = false;
  button.click();
  await page.context.send("/api/control_preview", { action: "home", arm: "left" });
  await page.settle();
  assert.equal(page.requests.length, 0);
});

test("a capability disappearing between render and click blocks dispatch", async () => {
  const page = dashboard(); page.render();
  page.context.state.robot_controls.capabilities.home.supported = false;
  page.button("home", "left").click();
  await page.settle();
  assert.equal(page.requests.length, 0);
});

test("supported action sends its reviewed action and arm exactly once", async () => {
  const page = dashboard(); page.render();
  page.button("home", "right").click();
  await page.settle();
  assert.equal(page.requests.length, 1);
  assert.equal(page.requests[0].url, "/api/control_preview");
  assert.equal(page.requests[0].body.action, "home");
  assert.equal(page.requests[0].body.arm, "right");
});

test("confirmation loses permission when support disappears, but cancel remains local", async () => {
  const page = dashboard(); const state = snapshot();
  state.robot_controls.proposal = { action: "rapid_reset", token: "reviewed", description: "Leave stopped." };
  page.dom["robot-control-ack"].checked = true; page.render(state);
  assert.equal(page.dom["robot-control-confirm"].disabled, false);
  state.robot_controls.capabilities.rapid_reset = { supported: false, reason: "This gateway cannot leave the program stopped." };
  page.render(state);
  assert.equal(page.dom["robot-control-confirm"].hidden, true);
  assert.equal(page.dom["robot-control-ack"].checked, false);
  assert.equal(page.dom["robot-control-cancel"].disabled, false);
  assert.match(page.dom["robot-control-description"].textContent, /cannot leave the program stopped/);
  page.dom["robot-control-confirm"].disabled = false; page.dom["robot-control-ack"].checked = true;
  page.dom["robot-control-confirm"].click();
  await page.context.send("/api/control_confirm", { token: "reviewed", acknowledged: true });
  assert.equal(page.requests.length, 0);
  page.dom["robot-control-cancel"].click(); await page.settle();
  assert.equal(page.requests[0].url, "/api/control_cancel");
});

test("backend reset labels and descriptions remain plain text, distinct from app recovery", () => {
  const page = dashboard(); const state = snapshot();
  state.robot_controls.capabilities.rapid_start = { supported: true, reason: "Available",
    label: "Reset and initialize RAPID", description: "Starts a fresh program; may initialize grippers." };
  state.robot_controls.capabilities.rapid_reset.supported = false;
  state.robot_controls.proposal = { action: "rapid_start", token: "reviewed", description: "<b>Fresh initialization, not resume.</b>" };
  page.render(state);
  assert.equal(page.button("rapid_start").textContent, "Reset and initialize RAPID");
  assert.equal(page.button("rapid_start").title, "Starts a fresh program; may initialize grippers.");
  assert.equal(page.dom["robot-control-proposal-heading"].textContent, "Reset and initialize RAPID");
  assert.equal(page.dom["robot-control-description"].textContent, "<b>Fresh initialization, not resume.</b>");
  assert.match(html, />Acknowledge app recovery<\/button>/);
  assert.doesNotMatch(html, /Reset and initialize RAPID separately/);
  assert.equal(page.button("home", "both").textContent, "Right, then left home");
  assert.match(html, /Both home moves the right arm, then the left\./);
});

test("disconnect, busy, recovery and Stop retain existing action interlocks", () => {
  const page = dashboard();
  for (const state of [snapshot({ busy: true }), snapshot({ recovery_required: true }), snapshot({ stop_requested: true })]) {
    page.render(state); assert.ok(page.buttons.every((el) => !el.hidden && el.disabled));
  }
  page.render(snapshot(), false); assert.ok(page.buttons.every((el) => el.disabled));
});

test("unknown support permits read-only discovery and best-effort Stop", async () => {
  const page = dashboard(); page.render(snapshot({ busy: true, robot_controls: {} }));
  assert.equal(page.context.stopAllowed(), true);
  await page.context.send("/api/stop");
  assert.equal(page.requests[0].url, "/api/stop");
  page.context.localStopInhibit = false;
  page.render(snapshot({ robot_controls: {} }));
  assert.equal(page.dom["robot-status-button"].hidden, false);
  page.dom["robot-status-button"].click(); await page.settle();
  assert.equal(page.requests[1].url, "/api/robot_status");
});

test("explicitly unsupported status is hidden and cannot be requested", async () => {
  const page = dashboard(); const state = snapshot();
  state.robot_controls.capabilities.robot_status = { supported: false, reason: "Status unavailable" };
  page.render(state);
  assert.equal(page.dom["robot-status-button"].hidden, true);
  await page.context.send("/api/robot_status");
  assert.equal(page.requests.length, 0);
});

test("local speed and alignment settings need no hardware capability", async () => {
  const page = dashboard(); page.render(snapshot({ robot_controls: {} }));
  await page.context.send("/api/speed", { speed: 250 });
  await page.context.send("/api/handover_offset", { x: 0, y: 0, z: 0 });
  assert.deepEqual(page.requests.map((request) => request.url), ["/api/speed", "/api/handover_offset"]);
});

test("an explicit unsupported Stop capability never hides the best-effort Stop request", async () => {
  const page = dashboard(); const state = snapshot({ busy: true });
  state.robot_controls.capabilities.stop.supported = false;
  page.render(state);
  assert.equal(page.context.stopAllowed(), true);
  await page.context.send("/api/stop");
  assert.equal(page.requests[0].url, "/api/stop");
});
