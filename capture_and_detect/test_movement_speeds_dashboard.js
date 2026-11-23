/* Offline movement-settings contract and guards; no server or robot is contacted. */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const script = fs.readFileSync(path.join(__dirname, "web_static/app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "web_static/index.html"), "utf8");
const keys = ["pick_lift", "handover", "flip", "placement"];
const defaults = { pick_lift: 100, handover: null, flip: 100, placement: null };
function section(start, end) {
  const a = script.indexOf(start), b = script.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `Missing production section: ${start}`);
  return script.slice(a, b);
}
function element() {
  const events = {};
  return { value: "", textContent: "", hidden: false, disabled: false, checked: false,
    classList: { toggle() {} },
    addEventListener(name, fn) { events[name] = fn; },
    fire(name) { return events[name]?.({ preventDefault() {} }); },
    set innerHTML(_) { throw new Error("Settings must be text, not HTML"); },
  };
}
function snapshot(overrides = {}) {
  return { ready: true, busy: false, recovery_required: false, stop_requested: false,
    csrf_token: "csrf-test", instance_id: "test-instance", mode: "live",
    motion: { speed: 250, home_speed: 200, home_after_cup: false, flip_speed: 100, phase_speeds_supported: true,
      phase_speed_defaults: { ...defaults }, phase_speed_settings: { ...defaults },
      phase_speeds: { pick_lift: 100, handover: 250, flip: 100, placement: 250 },
      handover: { adjusted: false, approach_speed: 250 } }, ...overrides };
}
function dashboard(initial = snapshot()) {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
  assert.equal(new Set(ids).size, ids.length);
  const dom = Object.fromEntries(ids.map((id) => [id, element()]));
  const requests = [];
  const context = vm.createContext({ dom, state: initial, connected: true, posting: false, stopPosting: false,
    deviceSpeedDirty: false, phaseSpeedDirty: false, localStopInhibit: false, releaseReview: null, quickGeneration: 0,
    transientError: "", document: { activeElement: null },
    text: (id, value) => { dom[id].textContent = String(value ?? ""); },
    hide: (id, value) => { dom[id].hidden = value; },
    speedText: (value) => Number.isFinite(value) ? `${value} mm/s` : "unavailable",
    accelerationText: (value) => `${value}% acceleration`, coordinatesText: () => "fixture coordinates",
    asMessage: (value) => String(value ?? ""), updateCountdown() {},
    readState: async () => {}, cancelReleaseReview() {},
    AbortController, setTimeout: () => 1, clearTimeout() {}, crypto: { randomUUID: () => "request-test" },
    fetch: async (url, options) => {
      requests.push({ url, body: JSON.parse(options.body), headers: options.headers });
      return context.response || { ok: true, json: async () => ({ accepted: true }) };
    },
  });
  const code = section("  const idle =", "  const isStop =")
    + section("  function robotRequestSupported(", "  function renderRobotControls()")
    + section("  async function post(", "  async function showQuick(")
    + section('  deviceSpeedKeys.forEach((key) => dom[`device-speed-${key}`].addEventListener', '  phaseSpeedKeys.forEach((key) => dom[`phase-speed-${key}`].addEventListener')
    + section('  phaseSpeedKeys.forEach((key) => dom[`phase-speed-${key}`].addEventListener', '  dom["robot-status-button"].addEventListener')
    + section("  function renderProposal()", "  function renderSavedCups()")
    + section("  function renderMode()", "  function renderConnection()");
  vm.runInContext(code + "\nglobalThis.render = () => { renderDeviceSpeedControls(); renderPhaseSpeedControls(); }; globalThis.send = post; globalThis.review = renderProposal; globalThis.mode = renderMode;", context);
  context.render();
  return { dom, context, requests, render: () => context.render(),
    edit(key, value) { dom[`phase-speed-${key}`].value = value; dom[`phase-speed-${key}`].fire("input"); },
    editDevice(key, value) { dom[`device-speed-${key}`].value = value; dom[`device-speed-${key}`].fire("input"); },
    async applyDevices() { dom["robot-device-speed-form"].fire("submit"); await new Promise((resolve) => setImmediate(resolve)); },
    async apply() { dom["robot-phase-speed-form"].fire("submit"); await new Promise((resolve) => setImmediate(resolve)); },
  };
}

test("stage fields use explicit capability and server-applied values with independent defaults", () => {
  const page = dashboard();
  assert.equal(page.dom["robot-phase-speeds"].hidden, false);
  assert.equal(page.dom["phase-speed-pick_lift"].value, "100");
  assert.equal(page.dom["phase-speed-handover"].value, "");
  assert.match(page.dom["phase-speed-handover-current"].textContent, /Applied: 250 mm\/s/);
  assert.match(page.dom["phase-speed-handover-default"].textContent, /follow overall/);
  assert.equal(page.dom["robot-phase-speed-apply"].disabled, true);
  for (const capability of [undefined, false, "true", 1]) {
    page.context.state.motion.phase_speeds_supported = capability; page.render();
    assert.equal(page.dom["robot-phase-speeds"].hidden, true);
    assert.ok(keys.every((key) => page.dom[`phase-speed-${key}`].disabled));
    assert.match(page.dom["robot-speed-policy"].textContent, /Loaded pick and cup flip stay capped at 100/);
  }
  assert.equal(page.requests.length, 0);
});

function deviceSnapshot() {
  const state = snapshot({ device_epoch: "owner-7", devices: { active_id: "mac", switching: false } });
  state.motion.device_speeds_supported = true;
  state.motion.device_speeds = { primary: 250, secondary: 180, tertiary: 120 };
  return state;
}

test("device speed fields show server-applied values and replace overall only with explicit support", () => {
  const page = dashboard(deviceSnapshot());
  assert.equal(page.dom["robot-device-speeds"].hidden, false);
  assert.equal(page.dom["robot-speed-form"].hidden, true);
  assert.equal(page.dom["robot-speed-current"].hidden, true);
  assert.equal(page.dom["device-speed-primary"].value, "250");
  assert.equal(page.dom["device-speed-secondary"].value, "180");
  assert.equal(page.dom["device-speed-tertiary"].value, "120");
  assert.match(page.dom["device-speed-tertiary-current"].textContent, /Applied: 120 mm\/s/);
  assert.match(page.dom["robot-device-speed-status"].textContent, /applied for the next batch/);
  assert.equal(page.dom["robot-device-speed-apply"].disabled, true);
  assert.match(html, /for="device-speed-primary">MacBook/);
  assert.match(html, /for="device-speed-secondary">Raspberry Pi/);
  assert.match(html, /for="device-speed-tertiary">Lynx/);
  for (const capability of [undefined, false, "true", 1]) {
    page.context.state.motion.device_speeds_supported = capability; page.render();
    assert.equal(page.dom["robot-device-speeds"].hidden, true);
    assert.equal(page.dom["robot-speed-form"].hidden, false);
    assert.equal(page.dom["robot-speed-current"].hidden, false);
    assert.equal(page.dom["robot-device-speed-apply"].disabled, true);
    for (const key of ["primary", "secondary", "tertiary"]) assert.equal(page.dom[`device-speed-${key}`].disabled, true);
  }
  assert.equal(page.requests.length, 0);
});

test("device drafts survive polling and explicit Apply sends all three values with ownership", async () => {
  const page = dashboard(deviceSnapshot());
  page.editDevice("primary", "350");
  page.editDevice("secondary", "220.5");
  page.editDevice("tertiary", "160");
  page.context.state.motion.device_speeds = { primary: 260, secondary: 190, tertiary: 130 };
  page.render();
  assert.equal(page.dom["device-speed-primary"].value, "350");
  assert.equal(page.dom["device-speed-secondary"].value, "220.5");
  assert.match(page.dom["device-speed-primary-current"].textContent, /260 mm\/s/);
  assert.match(page.dom["robot-device-speed-status"].textContent, /Changes have not been applied/);
  assert.equal(page.requests.length, 0);
  await page.applyDevices();
  assert.deepEqual(JSON.parse(JSON.stringify(page.requests)), [{ url: "/api/device_speeds",
    body: { primary: 350, secondary: 220.5, tertiary: 160, _device_id: "mac", _device_epoch: "owner-7", request_id: "request-test" },
    headers: { "Content-Type": "application/json", "X-CSRF-Token": "csrf-test" } }]);
  assert.equal(page.context.deviceSpeedDirty, false);
});

test("device speed validation rejects every invalid field and accepts inclusive limits", async () => {
  for (const key of ["primary", "secondary", "tertiary"]) {
    for (const value of ["", " ", "0", "-1", "1500.1", "NaN", "Infinity", "100x"]) {
      const page = dashboard(deviceSnapshot()); page.editDevice(key, value);
      assert.equal(page.dom["robot-device-speed-apply"].disabled, true, `${key}: ${value}`);
      await page.applyDevices();
      assert.equal(page.requests.length, 0);
    }
  }
  const page = dashboard(deviceSnapshot());
  page.editDevice("primary", "1"); page.editDevice("secondary", "1500");
  await page.applyDevices();
  assert.equal(page.requests[0].body.primary, 1);
  assert.equal(page.requests[0].body.secondary, 1500);
});

test("device settings cannot dispatch after busy, Stop, recovery, connection or capability changes", async () => {
  for (const change of [{ busy: true }, { ready: false }, { stop_requested: true }, { stop_in_flight: true },
    { recovery_required: true }, { devices: { switching: true } }]) {
    const page = dashboard(deviceSnapshot()); page.editDevice("tertiary", "160");
    Object.assign(page.context.state, change);
    await page.applyDevices();
    assert.equal(page.requests.length, 0, JSON.stringify(change));
    page.render();
    assert.equal(page.dom["device-speed-tertiary"].disabled, true);
    assert.equal(page.dom["robot-device-speed-apply"].disabled, true);
  }
  for (const flag of ["posting", "stopPosting", "localStopInhibit"]) {
    const page = dashboard(deviceSnapshot()); page.editDevice("tertiary", "160");
    page.context[flag] = true;
    await page.applyDevices(); assert.equal(page.requests.length, 0, flag);
  }
  const page = dashboard(deviceSnapshot()); page.editDevice("tertiary", "160");
  page.context.connected = false;
  await page.applyDevices(); assert.equal(page.requests.length, 0);
  page.context.connected = true;
  vm.runInContext('gatewayRecovery.status = "idle"; gatewayRecovery.queue = { state: "failed" };', page.context);
  await page.applyDevices(); assert.equal(page.requests.length, 0);
  vm.runInContext('gatewayRecovery.queue = null;', page.context);
  page.context.state.motion.device_speeds_supported = false;
  await page.applyDevices(); assert.equal(page.requests.length, 0);
});

test("missing device values disable editing and malformed direct payloads cannot dispatch", async () => {
  const page = dashboard(deviceSnapshot());
  delete page.context.state.motion.device_speeds.tertiary; page.render();
  assert.equal(page.dom["robot-device-speeds"].hidden, false);
  assert.equal(page.dom["device-speed-primary"].disabled, true);
  assert.match(page.dom["robot-device-speed-status"].textContent, /unavailable/);
  page.context.state = deviceSnapshot(); page.render();
  for (const body of [{ primary: 100 }, { primary: 100, secondary: 100, tertiary: "100" },
    { primary: 100, secondary: 100, tertiary: null }, { primary: 100, secondary: 0, tertiary: 100 }]) {
    await page.context.send("/api/device_speeds", body);
  }
  assert.equal(page.requests.length, 0);
});

test("shared stage limits are explained separately from each device overall speed", () => {
  const page = dashboard(deviceSnapshot());
  assert.equal(page.dom["robot-phase-speeds"].hidden, false);
  assert.match(page.dom["robot-speed-policy"].textContent, /Stage limits below are shared by all three/);
  assert.match(page.dom["robot-phase-speed-cap"].textContent, /each device’s overall speed.*lower value/);
  assert.match(page.dom["phase-speed-pick_lift-current"].textContent, /MacBook applied: 100 mm\/s/);
  page.context.state.motion.handover.adjusted = true; page.render();
  assert.match(page.dom["robot-phase-speed-cap"].textContent, /25 mm\/s/);
});

test("a failed device update keeps the draft and never retries automatically", async () => {
  const page = dashboard(deviceSnapshot()); page.editDevice("secondary", "210");
  page.context.response = { ok: false, json: async () => ({ detail: "Finish the current batch first." }) };
  await page.applyDevices(); page.render();
  assert.equal(page.requests.length, 1);
  assert.equal(page.context.deviceSpeedDirty, true);
  assert.equal(page.dom["device-speed-secondary"].value, "210");
  assert.match(page.dom["device-speed-secondary-current"].textContent, /180 mm\/s/);
  assert.match(page.context.transientError, /Finish the current batch/);
});

test("drafts preserve follow-overall values and only Apply sends the guarded request", async () => {
  const page = dashboard(snapshot({ device_epoch: "owner-7", devices: { active_id: "mac", switching: false } }));
  page.edit("pick_lift", "180"); page.edit("flip", "140"); page.edit("placement", "210");
  page.render();
  assert.equal(page.dom["phase-speed-pick_lift"].value, "180");
  assert.match(page.dom["phase-speed-pick_lift-current"].textContent, /100 mm\/s/);
  assert.equal(page.requests.length, 0);
  assert.equal(page.dom["robot-phase-speed-apply"].disabled, false);
  await page.apply();
  assert.equal(page.requests.length, 1);
  assert.equal(page.requests[0].url, "/api/phase_speeds");
  assert.deepEqual(page.requests[0].body, { pick_lift: 180, handover: null, flip: 140, placement: 210,
    _device_id: "mac", _device_epoch: "owner-7", request_id: "request-test" });
  assert.equal(page.requests[0].headers["X-CSRF-Token"], "csrf-test");
  assert.equal(page.context.phaseSpeedDirty, false);
});

test("loading defaults only creates an unapplied draft", async () => {
  const state = snapshot(); state.motion.phase_speed_settings = { pick_lift: 180, handover: 180, flip: 140, placement: 180 };
  const page = dashboard(state);
  page.dom["robot-phase-speed-defaults"].fire("click");
  assert.equal(page.dom["phase-speed-pick_lift"].value, "100");
  assert.equal(page.dom["phase-speed-handover"].value, "");
  assert.equal(page.dom["phase-speed-flip"].value, "100");
  assert.equal(page.dom["phase-speed-placement"].value, "");
  assert.match(page.dom["robot-phase-speed-status"].textContent, /not been applied/);
  assert.equal(page.requests.length, 0);
  await page.apply();
  for (const key of keys) assert.equal(page.requests[0].body[key], defaults[key]);
});

test("a cleared draft adopts the new owner settings even when its field retained focus", () => {
  const page = dashboard();
  page.context.document.activeElement = page.dom["phase-speed-flip"];
  page.edit("flip", "220");
  page.context.phaseSpeedDirty = false; // readState clears drafts on instance or owner changes.
  page.context.state = snapshot();
  page.render();
  assert.equal(page.dom["phase-speed-flip"].value, "100");
  assert.equal(page.dom["robot-phase-speed-apply"].disabled, true);
  assert.equal(page.requests.length, 0);
});

test("invalid numbers cannot apply while optional blanks stay valid", async () => {
  for (const value of ["", "0", "-1", "1501", "NaN", "Infinity"]) {
    const page = dashboard(); page.edit("pick_lift", value);
    assert.equal(page.dom["robot-phase-speed-apply"].disabled, true, value);
    await page.apply(); assert.equal(page.requests.length, 0);
  }
  const page = dashboard(); page.edit("handover", "60.5"); page.edit("placement", "");
  assert.equal(page.dom["robot-phase-speed-apply"].disabled, false);
  await page.apply(); assert.equal(page.requests[0].body.handover, 60.5); assert.equal(page.requests[0].body.placement, null);
});

test("idle, stop, recovery, switching and explicit capability guards reject late submissions", async () => {
  for (const change of [{ busy: true }, { ready: false }, { stop_requested: true }, { recovery_required: true }, { devices: { switching: true } }]) {
    const page = dashboard(); page.edit("flip", "140");
    Object.assign(page.context.state, change);
    await page.apply(); assert.equal(page.requests.length, 0, JSON.stringify(change));
    page.render(); assert.equal(page.dom["phase-speed-flip"].disabled, true);
    assert.equal(page.dom["robot-phase-speed-apply"].disabled, true);
  }
  for (const field of ["posting", "stopPosting", "localStopInhibit"]) {
    const page = dashboard(); page.edit("flip", "140"); page.context[field] = true;
    await page.apply(); assert.equal(page.requests.length, 0, field);
  }
  const page = dashboard(); page.edit("flip", "140"); page.context.connected = false;
  await page.apply(); assert.equal(page.requests.length, 0);
  page.context.connected = true; page.context.state.motion.phase_speeds_supported = false;
  await page.apply(); assert.equal(page.requests.length, 0);
});

test("caps are read from controller and adjusted alignment explains receiving speed", () => {
  const state = snapshot(); state.motion.speed = 80;
  state.motion.phase_speed_settings = { pick_lift: 180, handover: null, flip: 140, placement: 180 };
  state.motion.phase_speeds = { pick_lift: 80, handover: 80, flip: 80, placement: 80 };
  state.motion.handover = { adjusted: true, approach_speed: 25 };
  const page = dashboard(state);
  assert.equal(page.dom["phase-speed-pick_lift"].value, "180");
  assert.match(page.dom["phase-speed-pick_lift-current"].textContent, /80 mm\/s/);
  assert.match(page.dom["phase-speed-handover-current"].textContent, /Applied: 80 mm\/s · Receiving: 25 mm\/s/);
  assert.match(page.dom["robot-phase-speed-cap"].textContent, /alignment.*25 mm\/s/);
  assert.equal(page.dom["robot-phase-speed-apply"].disabled, true);
});

test("failed submission keeps draft and is never retried automatically", async () => {
  const page = dashboard(); page.edit("flip", "140");
  page.context.response = { ok: false, json: async () => ({ detail: "Controller became busy." }) };
  await page.apply();
  assert.equal(page.requests.length, 1);
  assert.equal(page.context.phaseSpeedDirty, true);
  assert.equal(page.dom["phase-speed-flip"].value, "140");
  assert.match(page.context.transientError, /became busy/);
});

test("picking review shows captured phase speeds and old agents retain existing fallback", () => {
  const page = dashboard();
  page.context.state.proposal = { cup_id: 2, action: "goto", capture_id: "scan-1", speed: 220, home_speed: 200,
    phase_speeds: { pick_lift: 150, handover: 170, flip: 140, placement: 160 } };
  page.context.review();
  assert.match(page.dom["proposal-motion"].textContent, /Pick & lift: 150 mm\/s.*Right handover: 170 mm\/s.*Cup flip: 140 mm\/s.*Place & clear: 160 mm\/s/);
  assert.doesNotMatch(page.dom["proposal-motion"].textContent, /250 mm\/s/);
  delete page.context.state.proposal.phase_speeds; page.context.review();
  assert.match(page.dom["proposal-motion"].textContent, /Cup flip: 100 mm\/s/);
  assert.doesNotMatch(page.dom["proposal-motion"].textContent, /Pick & lift/);
  page.context.state.proposal.action = "hover"; page.context.review();
  assert.doesNotMatch(page.dom["proposal-motion"].textContent, /Cup flip|Place & clear/);
});

test("picking review describes the proposed Home mode instead of promising parking", () => {
  const page = dashboard();
  page.context.state.motion.home_after_cup = true;
  page.context.state.proposal = { cup_id: 2, action: "goto", home_after_cup: false, home_speed: 175 };
  page.context.review();
  assert.match(page.dom["proposal-action"].textContent, /lift clear/);
  assert.match(page.dom["proposal-motion"].textContent, /Automatic home off; arms remain at clearance poses/);
  assert.doesNotMatch(page.dom["proposal-motion"].textContent, /Sequence home:/);
  page.context.state.motion.home_after_cup = false;
  page.context.state.proposal.home_after_cup = true;
  page.context.review();
  assert.match(page.dom["proposal-action"].textContent, /return home/);
  assert.match(page.dom["proposal-motion"].textContent, /Sequence home: 175 mm\/s/);
  assert.doesNotMatch(page.dom["proposal-motion"].textContent, /Automatic home off/);
});

test("review uses action-specific acceleration and falls back for older controllers", () => {
  const page = dashboard();
  page.context.state.motion.acceleration = 60;
  page.context.state.proposal = { cup_id: 2, action: "hover", acceleration: 100 };
  page.context.review();
  assert.match(page.dom["proposal-motion"].textContent, /100% acceleration/);
  assert.doesNotMatch(page.dom["proposal-motion"].textContent, /60% acceleration/);
  page.context.state.proposal = { cup_id: 2, action: "goto", acceleration: 60 };
  page.context.state.motion.acceleration = 100;
  page.context.review();
  assert.match(page.dom["proposal-motion"].textContent, /60% acceleration/);
  delete page.context.state.proposal.acceleration;
  page.context.review();
  assert.match(page.dom["proposal-motion"].textContent, /100% acceleration/);
  assert.equal(page.requests.length, 0);
});

test("motion summary distinguishes the smooth dual-arm plan from older controllers", () => {
  const page = dashboard();
  page.context.state.motion.handover_motion = "blended_path";
  page.context.state.motion.handover_plan = "smooth_dual_arm_v1";
  page.context.mode();
  assert.match(page.dom["motion-summary"].textContent, /Smooth dual-arm handover/);
  assert.doesNotMatch(page.dom["motion-summary"].textContent, /smooth_dual_arm_v1/);
  delete page.context.state.motion.handover_plan;
  page.context.mode();
  assert.match(page.dom["motion-summary"].textContent, /Smooth handover/);
  assert.doesNotMatch(page.dom["motion-summary"].textContent, /dual-arm/);
  page.context.state.motion.handover_motion = "single_arm";
  page.context.mode();
  assert.doesNotMatch(page.dom["motion-summary"].textContent, /Smooth/);
});
