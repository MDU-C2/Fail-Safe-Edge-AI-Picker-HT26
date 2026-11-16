/* Offline dashboard ownership and native-image rendering checks. Run with node --test. */
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
function element(tag = "div") {
  const listeners = {};
  return { tag, textContent: "", value: "", hidden: false, disabled: false, checked: false,
    title: "", dataset: {}, attributes: {}, children: [], naturalWidth: 1920, naturalHeight: 1080,
    classList: { add() {}, toggle() {} },
    replaceChildren() { this.children = []; }, append(...children) { this.children.push(...children); },
    setAttribute(key, value) { this.attributes[key] = String(value); },
    removeAttribute(key) { delete this.attributes[key]; },
    addEventListener(event, listener) { listeners[event] = listener; },
    change() { listeners.change?.(); }, click() { return listeners.click?.(); },
    set innerHTML(_) { throw new Error("Device and camera data must never become HTML"); },
  };
}
function snapshot(overrides = {}) {
  return { instance_id: "router-1", csrf_token: "csrf-1", ready: true, busy: false,
    recovery_required: false, stop_requested: false, device_epoch: "epoch-1",
    devices: { selected_id: "mac", active_id: "mac", switching: false, can_switch: true, message: "Ready",
      entries: [
        { id: "mac", label: "PC (this Mac)", available: true, status: "active", model: "mac-model" },
        { id: "pi", label: "Raspberry Pi", available: true, status: "ready", model: "pi-model" },
        { id: "lynx", label: "Lynx", available: false, status: "offline", model: "int8-model", reason: "Service is offline" },
      ] }, ...overrides };
}
function dashboard(initial = snapshot()) {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(new Set(ids).size, ids.length);
  const dom = Object.fromEntries(ids.map((id) => [id, element()]));
  const requests = [], events = [];
  const context = vm.createContext({
    state: initial, connected: true, posting: false, stopPosting: false, localStopInhibit: false,
    deviceChoice: "", deviceSelectionKey: "", deviceOptionsKey: "", dom,
    selectedId: null, selectedCapture: null, imageCapture: null, imageLoaded: false,
    cardsKey: "", placementsKey: "", savedCupsKey: "", saveSelectionKey: "",
    speedDirty: false, phaseSpeedDirty: false, handoverDirty: false, controlDeadline: 0, proposalDeadline: 0,
    releaseReview: null, quickGeneration: 0, transientError: "", pollNumber: 0, appliedPoll: 0,
    document: { createElement: element, createElementNS: (_, tag) => element(tag) },
    text(id, value) { assert.ok(dom[id]); dom[id].textContent = String(value ?? ""); },
    hide(id, value) { assert.ok(dom[id]); dom[id].hidden = value; },
    asMessage: (value) => String(value ?? ""), robotRequestSupported: () => true,
    releaseActionable: () => true, releaseContext: (state) => JSON.stringify([state.instance_id, state.device_epoch]),
    cancelReleaseReview() { context.releaseReview = null; },
    cups: () => context.state?.inventory?.cups || [], selectedIds: () => [context.selectedId],
    performance: { now: () => 1000 }, crypto: { randomUUID: () => "request-123" },
    AbortController, setTimeout: () => 1, clearTimeout() {},
    window: { dispatchEvent(event) { events.push(event); } },
    CustomEvent: class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } },
    fetch: async (url, options) => {
      if (options.method === "POST") {
        requests.push({ url, headers: options.headers, body: JSON.parse(options.body) });
        if (context.postResponse) return context.postResponse();
        return { ok: true, json: async () => ({ accepted: true }) };
      }
      assert.equal(url, "/api/state");
      return { ok: true, json: async () => context.incoming || context.state };
    },
  });
  const code = section("  const idle =", "  const isStop =")
    + section("  // Device ownership is server-confirmed", "  // Provider evidence comes only")
    + section("  function renderOverlay()", "  function placementCupSummary(")
    + section("  async function readState()", "  async function post(")
    + section("  async function post(", "  async function showQuick(")
    + section('  dom["device-select"].addEventListener', '  dom["start-button"].addEventListener');
  vm.runInContext(code + "\nglobalThis.render = () => { renderDevices(); globalThis.renderHook?.(); }; globalThis.draw = renderOverlay; globalThis.send = post; globalThis.refresh = readState;", context);
  context.render();
  return { context, dom, requests, events, render() { context.render(); },
    choose(id) { dom["device-select"].value = id; dom["device-select"].change(); },
    option(id) { return dom["device-select"].children.find((child) => child.value === id); } };
}

test("device choices describe active ownership separately from an unsubmitted choice", () => {
  const page = dashboard();
  assert.equal(page.dom["device-panel"].hidden, false);
  assert.equal(page.dom["inference-panel"].hidden, true);
  assert.equal(page.option("lynx").disabled, true);
  assert.match(page.option("lynx").textContent, /unavailable/);
  assert.equal(page.dom["device-use-button"].disabled, true);
  page.choose("pi");
  assert.equal(page.dom["device-use-button"].disabled, false);
  assert.equal(page.dom["device-active"].textContent, "PC (this Mac)");
  assert.equal(page.dom["device-model"].textContent, "mac-model");
  assert.match(page.dom["device-selection-note"].textContent, /Use device to activate/);
  assert.equal(page.requests.length, 0);
  page.render();
  assert.equal(page.dom["device-select"].value, "pi", "Polling retains an unsubmitted choice");
});

test("the combined Mac assistant keeps the full device selector visible", () => {
  const page = dashboard(snapshot({assistant_available:true}));
  assert.equal(page.dom["device-panel"].hidden,false);
  assert.equal(page.option("mac").disabled,false);
  assert.equal(page.option("pi").disabled,false);
  assert.match(page.dom["device-selection-note"].textContent,/Assistant can take a fresh photo/);
  page.choose("pi"); assert.equal(page.dom["device-use-button"].disabled,false);
  assert.match(page.dom["device-selection-note"].textContent,/Use device to activate/);
});

test("managed device display uses the server's local platform label with the same routing ID", () => {
  for (const label of ["MacBook", "Windows PC", "This PC"]) {
    const state = snapshot({ assistant_available: true });
    state.devices.entries[0].label = label;
    const page = dashboard(state);
    assert.equal(page.dom["device-panel"].hidden, false);
    assert.equal(page.option("mac").textContent, label);
    assert.equal(page.dom["device-active"].textContent, label);
    assert.equal(page.dom["device-select"].value, "mac");
    assert.match(page.dom["device-selection-note"].textContent, new RegExp(`^${label} runs detection and picking`));
    assert.equal(page.requests.length, 0);
  }
});

test("blocked target inspection reasons remain visible without selecting a disabled option", () => {
  const state = snapshot();
  Object.assign(state.devices.entries[1], { available: false, status: "needs_inspection", reason: "Inspect P2 and P5; unfinished reservations remain." });
  const page = dashboard(state);
  assert.equal(page.option("pi").disabled, true);
  assert.match(page.option("pi").textContent, /needs inspection/);
  assert.equal(page.dom["device-blocked-reasons"].hidden, false);
  assert.match(page.dom["device-blocked-reasons"].children[0].textContent, /Raspberry Pi · Needs inspection: Inspect P2 and P5/);
  assert.equal(page.requests.length, 0);
  page.context.state.devices.entries.forEach((entry) => { entry.available = true; entry.status = "Ready"; });
  page.render();
  assert.equal(page.dom["device-blocked-reasons"].hidden, true);
  assert.equal(page.dom["device-blocked-reasons"].children.length, 0);
});

test("reachable targets that need inspection still expose the reason before a switch request", () => {
  const state = snapshot();
  Object.assign(state.devices.entries[1], { available: true, status: "Needs inspection", reason: "P2 is reserved. Inspect the target records." });
  const page = dashboard(state);
  assert.match(page.option("pi").textContent, /needs inspection/);
  assert.match(page.dom["device-blocked-reasons"].children[0].textContent, /P2 is reserved/);
  page.choose("pi");
  assert.match(page.dom["device-selection-note"].textContent, /P2 is reserved/);
  assert.equal(page.requests.length, 0);
});

test("owned startup states do not claim the model is ready and expose the restart instruction", () => {
  const state = snapshot({ ready: false }); state.devices.can_switch = false;
  Object.assign(state.devices.entries[0], { status: "Starting", reason: "MacBook is starting its picker and model." });
  const page = dashboard(state);
  assert.equal(page.dom["device-badge"].textContent, "STARTING");
  assert.match(page.dom["device-selection-note"].textContent, /starting its picker/);
  Object.assign(state.devices.entries[0], { status: "Startup failed", reason: "MacBook picker startup failed: <missing module>. Fix the cause, then restart this dashboard to reload the Mac picker." });
  page.render();
  assert.equal(page.dom["device-badge"].textContent, "STARTUP FAILED");
  assert.match(page.dom["device-selection-note"].textContent, /<missing module>.*restart this dashboard/);
  assert.match(page.dom["device-blocked-reasons"].children[0].textContent, /<missing module>/);
  assert.equal(page.dom["device-use-button"].disabled, true);
  state.ready = true; state.devices.can_switch = true;
  Object.assign(state.devices.entries[0], { status: "Ready", reason: null }); page.render();
  assert.equal(page.dom["device-badge"].textContent, "READY");
  assert.doesNotMatch(page.dom["device-selection-note"].textContent, /restart this dashboard/);
  assert.equal(page.requests.length, 0);
});

test("unavailable choices cannot dispatch even if the DOM is changed", async () => {
  const page = dashboard(); page.choose("lynx");
  assert.equal(page.dom["device-use-button"].disabled, true);
  assert.equal(page.dom["device-selection-note"].textContent, "Service is offline");
  page.dom["device-use-button"].disabled = false;
  await page.dom["device-use-button"].click();
  assert.equal(page.requests.length, 0);
});

test("switch admission follows connection, busy state and the server switch gate", async () => {
  for (const alter of [
    (p) => { p.context.connected = false; },
    (p) => { p.context.state.busy = true; },
    (p) => { p.context.state.devices.switching = true; },
    (p) => { p.context.state.devices.can_switch = false; },
    (p) => { p.context.posting = true; },
    (p) => { p.context.stopPosting = true; },
    (p) => { p.context.state.stop_in_flight = true; },
    (p) => { p.context.state.device_epoch = null; },
  ]) {
    const page = dashboard(); page.choose("pi"); alter(page); page.render();
    assert.equal(page.dom["device-select"].disabled, true);
    assert.equal(page.dom["device-use-button"].disabled, true);
    await page.context.send("/api/device_select", { device_id: "pi" });
    assert.equal(page.requests.length, 0);
  }
});

test("initial activation is possible without an active ready controller", async () => {
  const state = snapshot({ ready: false }); state.devices.active_id = null;
  const page = dashboard(state);
  assert.equal(page.dom["device-active"].textContent, "None confirmed");
  assert.equal(page.dom["device-use-button"].disabled, false);
  await page.dom["device-use-button"].click();
  assert.deepEqual(page.requests[0].body, { device_id: "mac", _device_id: null, _device_epoch: "epoch-1", request_id: "request-123" });
});

test("every mutation binds to the observed active device and epoch", async () => {
  const paths = ["scan", "start", "batch", "command", "preview", "confirm", "cancel", "recovery", "robot_status", "speed", "phase_speeds", "handover_offset", "control_preview", "control_confirm", "control_cancel", "save_cup", "pick_saved", "forget_cup", "release_locations", "stop", "device_select"];
  for (const path of paths) {
    const page = dashboard();
    await page.context.send(`/api/${path}`, { device_id: "pi", _device_id: "forged", _device_epoch: "forged" });
    assert.equal(page.requests.length, 1, path);
    assert.equal(page.requests[0].body._device_id, "mac", path);
    assert.equal(page.requests[0].body._device_epoch, "epoch-1", path);
    assert.equal(page.requests[0].body.request_id, "request-123", path);
  }
});

test("a request captures its owner and CSRF together before any render or response", async () => {
  const page = dashboard(); let first = true;
  page.context.renderHook = () => {
    if (!first) return; first = false;
    page.context.state = snapshot({ device_epoch: "epoch-2", csrf_token: "csrf-2" });
    page.context.state.devices.active_id = "pi";
  };
  await page.context.send("/api/scan");
  assert.equal(page.requests[0].body._device_id, "mac");
  assert.equal(page.requests[0].body._device_epoch, "epoch-1");
  assert.equal(page.requests[0].headers["X-CSRF-Token"], "csrf-1");
  assert.match(page.context.transientError, /ownership changed.*not retried/);
  assert.equal(page.requests.length, 1);
});

test("Stop has priority during another POST, busy work and a device transition", async () => {
  const page = dashboard();
  page.context.posting = true; page.context.state.busy = true; page.context.state.devices.switching = true; page.context.state.ready = false;
  await page.context.send("/api/scan");
  assert.equal(page.requests.length, 0);
  await page.context.send("/api/stop");
  assert.equal(page.requests.length, 1);
  assert.equal(page.requests[0].url, "/api/stop");
  assert.equal(page.requests[0].body._device_id, "mac");
  assert.equal(page.context.localStopInhibit, true);
  assert.equal(page.context.posting, true, "Stop completion must not unlock a pending normal request");
});

test("missing ownership epoch fails closed instead of sending an unfenced mutation", async () => {
  const page = dashboard(snapshot({ device_epoch: undefined }));
  await page.context.send("/api/scan");
  assert.equal(page.requests.length, 0);
  assert.match(page.context.transientError, /ownership is unavailable/);
});

test("direct Mac and demo dashboards hide device routing while retaining their command payloads", async () => {
  for (const mode of ["live", "dry_run", "demo"]) {
    const page = dashboard({ ...snapshot(), devices: undefined, device_epoch: undefined, mode });
    assert.equal(page.dom["device-panel"].hidden, true);
    assert.equal(page.dom["inference-panel"].hidden, true);
    await page.context.send("/api/scan", { capture_id: "local-capture" });
    assert.deepEqual(page.requests[0].body, { capture_id: "local-capture", request_id: "request-123" });
  }
});

test("state updates and local Stop notify the inline Assistant", async () => {
  const page = dashboard({ ...snapshot(), devices: undefined, device_epoch: undefined });
  await page.context.refresh();
  assert.equal(page.events.at(-1).type, "assistant-state");
  assert.equal(page.events.at(-1).detail.instance_id, "router-1");
  await page.context.send("/api/stop");
  assert.ok(page.events.some((event) => event.type === "assistant-local-stop"));
  page.context.fetch = async () => { throw new Error("offline"); };
  await page.context.refresh();
  assert.equal(page.events.at(-1).type, "assistant-disconnected");
});

test("plain HTTP phone commands have unique request IDs without randomUUID", async () => {
  const page = dashboard();
  const webcrypto = require("node:crypto").webcrypto;
  page.context.crypto = { getRandomValues: (bytes) => webcrypto.getRandomValues(bytes) };
  await page.context.send("/api/scan");
  await page.context.send("/api/scan");
  assert.equal(page.requests.length, 2);
  const ids = page.requests.map((request) => request.body.request_id);
  ids.forEach((id) => assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/));
  assert.notEqual(ids[0], ids[1]);
  assert.equal(page.requests[0].body._device_id, "mac");
  assert.equal(page.requests[0].headers["X-CSRF-Token"], "csrf-1");
});

test("epoch changes invalidate local selections, image bindings and reviewed acknowledgements", async () => {
  for (const change of [{ device_epoch: "epoch-2" }, { instance_id: "router-2" }]) {
    const page = dashboard();
    Object.assign(page.context, { selectedId: 2, selectedCapture: "old", imageCapture: "old", imageLoaded: true,
      proposalDeadline: 9000, controlDeadline: 9000, speedDirty: true, phaseSpeedDirty: true, handoverDirty: true,
      cardsKey: "old", placementsKey: "old", savedCupsKey: "old", saveSelectionKey: "old" });
    page.dom["robot-control-ack"].checked = true; page.dom["recovery-ack"].checked = true;
    page.dom["selection-overlay"].append(element("rect"));
    page.dom["camera-image"].setAttribute("src", "/api/image/old");
    page.context.incoming = snapshot(change);
    await page.context.refresh();
    for (const key of ["selectedId", "selectedCapture", "imageCapture"]) assert.equal(page.context[key], null);
    for (const key of ["imageLoaded", "speedDirty", "phaseSpeedDirty", "handoverDirty"]) assert.equal(page.context[key], false);
    assert.equal(page.context.proposalDeadline, 0); assert.equal(page.context.controlDeadline, 0);
    assert.equal(page.dom["robot-control-ack"].checked, false); assert.equal(page.dom["recovery-ack"].checked, false);
    assert.equal(page.dom["selection-overlay"].children.length, 0);
    assert.equal(page.dom["camera-image"].attributes.src, undefined);
    assert.equal(page.dom["image-stage"].hidden, true);
    assert.match(page.context.transientError, /scan again|Scan again/);
  }
});

test("a confirmed device selection clears old choices without a false restart warning", async () => {
  const page = dashboard();
  page.context.selectedId = 2; page.context.selectedCapture = "old";
  page.context.incoming = snapshot({ instance_id: "router-switch-2", device_epoch: "epoch-2" });
  page.context.incoming.devices.active_id = "pi";
  page.context.incoming.devices.selected_id = "pi";
  page.context.incoming.devices.message = "Raspberry Pi selected. Scan again.";
  await page.context.send("/api/device_select", { device_id: "pi" });
  assert.equal(page.requests.length, 1);
  assert.equal(page.context.selectedId, null);
  assert.equal(page.context.selectedCapture, null);
  assert.equal(page.context.transientError, "");
  assert.equal(page.dom["device-active"].textContent, "Raspberry Pi");
  assert.equal(page.dom["device-message"].textContent, "Raspberry Pi selected. Scan again.");
});

test("a switch observed by polling uses server handoff status without a restart alarm", async () => {
  const page = dashboard();
  page.context.incoming = snapshot({ instance_id: "router-switch-2", device_epoch: "epoch-2" });
  page.context.incoming.devices.switching = true;
  page.context.incoming.devices.active_id = null;
  page.context.incoming.devices.selected_id = "pi";
  await page.context.refresh();
  assert.equal(page.context.transientError, "");
  assert.equal(page.dom["device-badge"].textContent, "SWITCHING");
  assert.equal(page.dom["device-active"].textContent, "None confirmed");
});

test("device labels and unavailable reasons remain text", () => {
  const state = snapshot(); state.devices.entries[0].label = '<img src=x onerror="bad()">';
  state.devices.entries[0].model = "<script>bad()</script>";
  const page = dashboard(state);
  assert.equal(page.dom["device-active"].textContent, state.devices.entries[0].label);
  assert.equal(page.dom["device-model"].textContent, state.devices.entries[0].model);
  page.context.connected = false; page.render();
  assert.equal(page.dom["device-badge"].textContent, "LAST KNOWN STATE");
  assert.match(page.dom["device-message"].textContent, /last received snapshot/);
});

test("native images get grid status and cup IDs while selected-cup highlighting remains", () => {
  const page = dashboard();
  page.context.state.inventory = { image_overlay: "native", cups: [{ id: 2, bbox_xyxy: [100, 120, 160, 190] }],
    placement_observation: { slots: [
      { id: 4, state: "clear", polygon_px: [[200, 200], [300, 200], [300, 300], [200, 300]] },
      { id: 5, state: "occupied", polygon_px: [[310, 200], [410, 200], [410, 300], [310, 300]] },
      { id: 9, state: "unknown", polygon_px: [[420, 200], [520, 200], [520, 300], [420, 300]] },
    ] } };
  page.context.imageLoaded = true; page.context.selectedId = 2; page.context.draw();
  const children = page.dom["selection-overlay"].children;
  assert.equal(children.filter((el) => el.tag === "polygon").length, 3);
  assert.deepEqual(children.filter((el) => el.tag === "text").map((el) => el.textContent), ["P4", "P5", "P9", "Cup 2"]);
  assert.equal(children.filter((el) => el.tag === "rect").length, 2, "Cup outline plus existing selection rectangle");
  assert.equal(children[0].attributes.class, "native-grid native-grid-clear");
  delete page.context.state.inventory.image_overlay;
  page.context.draw();
  assert.equal(page.dom["selection-overlay"].children.length, 1, "Annotated legacy images only need selection highlighting");
});

test("native overlay rejects invalid geometry and waits for the image to load", () => {
  const page = dashboard();
  page.context.state.inventory = { image_overlay: "native", cups: [{ id: 2, pixel: { u: NaN, v: 50 } }],
    placement_observation: { slots: [{ id: 4, polygon_px: [[0, 0], [Infinity, 1], [2, 2]] }] } };
  page.context.imageLoaded = true; page.context.draw();
  assert.equal(page.dom["selection-overlay"].children.length, 0);
  page.context.state.inventory.cups = [{ id: 2, pixel: { u: 30, v: 50 } }];
  page.context.imageLoaded = false; page.context.draw();
  assert.equal(page.dom["selection-overlay"].children.length, 0);
});
