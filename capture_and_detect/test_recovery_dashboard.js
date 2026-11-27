/* Recovery remains usable across background polling without relaxing interlocks. */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "web_static/app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "web_static/index.html"), "utf8");
function section(start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `Missing production section: ${start}`);
  return source.slice(a, b);
}
function snapshot(overrides = {}) {
  return { instance_id: "controller-1", csrf_token: "csrf-1", device_epoch: "device-1",
    ready: true, busy: false, mode: "live", recovery_required: true,
    stop_requested: false, stop_in_flight: false, stop: null,
    progress: { state: "interrupted", step: "right_home", completed: 0, total: 2 }, ...overrides };
}
function dashboard() {
  const dom = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, {
    textContent: "", value: "", hidden: false, disabled: false, checked: false,
    replaceChildren() {}, removeAttribute() {}, classList: { toggle() {} },
  }]));
  const requests = [];
  const context = vm.createContext({
    dom, state: snapshot(), connected: true, posting: false, stopPosting: false, quickPosting: false,
    localStopInhibit: false, selectedId: null, selectedCapture: null, releaseReview: null,
    pollNumber: 0, appliedPoll: 0, transientError: "", quickGeneration: 0,
    imageCapture: null, imageLoaded: false, cardsKey: "", placementsKey: "", savedCupsKey: "", saveSelectionKey: "",
    proposalDeadline: 0, controlDeadline: 0, speedDirty: false, deviceSpeedDirty: false, phaseSpeedDirty: false,
    document: { querySelectorAll() { return []; } },
    text(id, value) { dom[id].textContent = String(value ?? ""); },
    hide(id, hidden) { dom[id].hidden = hidden; },
    asMessage: (value) => String(value ?? ""), robotRequestSupported: () => true,
    cancelReleaseReview() {}, deviceSwitchAllowed: () => false,
    AbortController, setTimeout: () => 1, clearTimeout() {},
    performance: { now: () => 1000 }, crypto: { randomUUID: () => "request-1" },
    window: { dispatchEvent() {} }, CustomEvent: class {},
    fetch: async (url, options) => {
      if (options.method === "POST") {
        requests.push({ url, body: JSON.parse(options.body) });
        if (context.postError) throw context.postError;
        return context.postResponse || { ok: true, json: async () => ({ accepted: true }) };
      }
      assert.equal(url, "/api/state");
      if (context.incoming instanceof Error) throw context.incoming;
      return { ok: true, json: async () => context.incoming || context.state };
    },
  });
  for (const name of ["renderMode", "renderConnection", "renderDevices", "renderInference", "renderStop",
    "renderInventory", "renderSelection", "renderPlacements", "renderProposal", "renderActivity", "renderNavigation",
    "renderSelectedActions", "updateCountdown", "renderRobotControls", "renderPickHeight", "renderSavedCups"]) context[name] = () => {};
  vm.runInContext(section("  const cups =", "  const selectedIds =")
    + section("  function updateCountdown()", "  function renderActivity()")
    + section("  function renderControls()", "  async function showQuick(")
    + "\nglobalThis.refresh = readState; globalThis.send = post;", context);
  context.render();
  return { dom, context, requests,
    gateway(value) { context.receiveGatewayState(value); },
    acknowledge() { dom["recovery-ack"].checked = true; context.render(); },
    async update(incoming) { context.incoming = incoming; await context.refresh(); },
  };
}

test("background busy polling keeps inspection visible and preserves unchanged acknowledgement", async () => {
  const page = dashboard(); page.acknowledge();
  for (let revision = 1; revision <= 4; revision++) {
    const busy = revision % 2 === 1;
    const incoming = snapshot({ busy, revision, operation: busy ? "failover_resume" : null,
      message: `Background status ${revision}`, error: "Unrelated robot recovery requires inspection before failover." });
    await page.update(incoming);
    assert.equal(page.context.connected, true);
    assert.equal(page.dom["recovery-panel"].hidden, false);
    assert.equal(page.dom["recovery-ack"].checked, true);
    assert.equal(page.dom["recovery-ack"].disabled, busy);
    assert.equal(page.dom["recovery-button"].disabled, busy);
  }
  assert.deepEqual(page.requests, [], "Polling and rendering never acknowledge recovery automatically");
});

test("changed ownership, recovery, Stop, or motion evidence requires fresh acknowledgement", async () => {
  for (const changes of [
    { instance_id: "controller-2" }, { device_epoch: "device-2" }, { recovery_required: false },
    { stop_requested: true }, { stop_in_flight: true },
    { stop: { state: "confirmed", stopped: true } },
    { progress: { state: "interrupted", step: "left_home", completed: 1, total: 2 } },
  ]) {
    const page = dashboard(); page.acknowledge();
    await page.update(snapshot(changes));
    assert.equal(page.context.connected, true);
    assert.equal(page.dom["recovery-ack"].checked, false);
    assert.equal(page.dom["recovery-button"].disabled, true);
    assert.equal(page.dom["recovery-panel"].hidden, changes.recovery_required === false);
    assert.deepEqual(page.requests, []);
  }
});

test("real work, switching, readiness loss and Stop in flight prevent recovery dispatch", async () => {
  for (const changes of [
    { busy: true, operation: "control_confirm" }, { ready: false },
    { devices: { switching: true } }, { stop_in_flight: true },
  ]) {
    const page = dashboard(); page.acknowledge();
    page.context.state = snapshot(changes); page.context.render();
    assert.equal(page.dom["recovery-panel"].hidden, false);
    assert.equal(page.dom["recovery-ack"].disabled, true);
    assert.equal(page.dom["recovery-button"].disabled, true);
    await page.context.send("/api/recovery", { acknowledged: true });
    assert.deepEqual(page.requests, []);
  }
});

test("connection loss clears acknowledgement and never hides the outstanding inspection", async () => {
  const page = dashboard(); page.acknowledge();
  await page.update(new Error("offline"));
  assert.equal(page.context.connected, false);
  assert.equal(page.dom["recovery-panel"].hidden, false);
  assert.equal(page.dom["recovery-ack"].checked, false);
  assert.equal(page.dom["recovery-button"].disabled, true);
  await page.context.send("/api/recovery", { acknowledged: true });
  assert.deepEqual(page.requests, []);
  await page.update(snapshot());
  assert.equal(page.context.connected, true);
  assert.equal(page.dom["recovery-ack"].checked, false);
  assert.equal(page.dom["recovery-button"].disabled, true);
});

test("an explicit idle acknowledgement sends one local recovery request", async () => {
  const page = dashboard(); page.acknowledge();
  assert.equal(page.dom["recovery-button"].disabled, false);
  await page.context.send("/api/recovery", { acknowledged: true });
  assert.deepEqual(page.requests, [{ url: "/api/recovery", body: { acknowledged: true, request_id: "request-1" } }]);
  assert.equal(page.dom["recovery-ack"].checked, false);
  assert.equal(page.dom["recovery-button"].disabled, true);
});

test("unblocking is named clearly and explains the new-batch boundary", () => {
  assert.match(html, /id="recovery-button"[^>]*>Unblock failed batch<\/button>/);
  assert.match(html, /Recovery sends no movement and does not resume the interrupted pick/);
  assert.match(html, /Scan again, then start and confirm a new batch/);
  assert.match(html, /id="robot-local-recovery-button"[^>]*>Review blocked workflow<\/button>/);
});

test("recovery failure is explained beside the inspection without reusing acknowledgement", async () => {
  const page = dashboard(); page.acknowledge();
  page.context.postResponse = { ok: false, status: 409,
    json: async () => ({ accepted: false, error: "The gateway queue changed during inspection." }) };
  await page.context.send("/api/recovery", { acknowledged: true });
  assert.equal(page.dom["recovery-error"].hidden, false);
  assert.match(page.dom["recovery-error"].textContent, /gateway queue changed/);
  assert.equal(page.dom["recovery-ack"].checked, false);
  assert.equal(page.dom["recovery-button"].disabled, true);
  await page.update(snapshot());
  assert.match(page.dom["recovery-error"].textContent, /gateway queue changed/);
  assert.equal(page.requests.length, 1);
});

test("recovery dispatch requires the current inspection checkbox and an outstanding block", async () => {
  const page = dashboard();
  await page.context.send("/api/recovery", { acknowledged: true });
  assert.deepEqual(page.requests, []);
  page.acknowledge();
  await page.context.send("/api/recovery", { acknowledged: false });
  assert.deepEqual(page.requests, []);
  page.context.state = snapshot({ recovery_required: false });
  await page.context.send("/api/recovery", { acknowledged: true });
  assert.deepEqual(page.requests, []);
});

test("double clicks and unchanged polling do not repeat recovery", async () => {
  const page = dashboard(); page.acknowledge();
  await Promise.all([
    page.context.send("/api/recovery", { acknowledged: true }),
    page.context.send("/api/recovery", { acknowledged: true }),
  ]);
  await page.update(snapshot());
  await page.context.send("/api/recovery", { acknowledged: true });
  assert.equal(page.requests.length, 1);
  assert.equal(page.dom["recovery-ack"].checked, false);
});

test("failed and uncertain recovery requests require a fresh inspection acknowledgement", async () => {
  for (const unknown of [false, true]) {
    const page = dashboard(); page.acknowledge();
    if (unknown) page.context.postError = new TypeError("Disconnected");
    else page.context.postResponse = { ok: false, json: async () => ({ accepted: false, error: "Queue changed; inspect again." }) };
    await page.context.send("/api/recovery", { acknowledged: true });
    await page.update(snapshot());
    await page.context.send("/api/recovery", { acknowledged: true });
    assert.equal(page.requests.length, 1);
    assert.equal(page.dom["recovery-ack"].checked, false);
    assert.equal(page.dom["recovery-button"].disabled, true);
    assert.equal(page.dom["recovery-panel"].hidden, false);
  }
});

test("a newly requested Stop invalidates acknowledgement before its state reply", async () => {
  const page = dashboard(); page.acknowledge();
  await page.context.send("/api/stop");
  assert.equal(page.dom["recovery-ack"].checked, false);
  assert.deepEqual(page.requests.map((request) => request.url), ["/api/stop"]);
});

test("an unowned terminal gateway queue exposes explicit recovery without a local journal", async () => {
  for (const state of ["failed", "cancelled"]) {
    const page = dashboard();
    await page.update(snapshot({ recovery_required: false }));
    assert.equal(page.dom["recovery-panel"].hidden, true);
    page.gateway({ status: "idle", client_id: null, queue: { state, owner: "secondary" } });
    assert.equal(page.dom["recovery-panel"].hidden, false);
    assert.equal(page.dom["start-button"].disabled, true);
    assert.equal(page.dom["recovery-button"].disabled, true);
    assert.equal(page.dom["scan-button"].disabled, false, "Camera-only scanning remains available");
    await page.context.send("/api/recovery", { acknowledged: true });
    assert.deepEqual(page.requests, [], "Detection cannot acknowledge inspection");
    page.acknowledge();
    assert.equal(page.dom["recovery-button"].disabled, false);
    await page.context.send("/api/recovery", { acknowledged: false });
    assert.deepEqual(page.requests, []);
    await page.context.send("/api/recovery", { acknowledged: true });
    assert.deepEqual(page.requests, [{ url: "/api/recovery", body: { acknowledged: true, request_id: "request-1" } }]);
    assert.equal(page.dom["recovery-ack"].checked, false);
  }
});

test("external queue recovery blocks preparation and existing motion confirmations", async () => {
  const page = dashboard();
  await page.update(snapshot({ recovery_required: false, scan_usable: true,
    inventory: { capture: "scan-1", cups: [] }, proposal: { capture_id: "scan-1" } }));
  page.context.proposalDeadline = 30000;
  page.context.render();
  assert.equal(page.dom["confirm-button"].disabled, false);
  page.gateway({ status: "idle", queue: { state: "cancelled", owner: "primary" } });
  assert.equal(page.dom["confirm-button"].disabled, true);
  assert.equal(vm.runInContext("controlActionable()", page.context), false);
  for (const path of ["/api/start", "/api/batch", "/api/select", "/api/confirm", "/api/command", "/api/control_confirm"]) {
    await page.context.send(path, {});
  }
  assert.deepEqual(page.requests, []);
});

test("unchanged gateway polls preserve inspection but changed ownership or queue state invalidates it", async () => {
  const original = { status: "idle", client_id: null, queue: { state: "cancelled", owner: "primary" } };
  for (const changed of [
    { status: "unavailable" }, { status: "active", client_id: "secondary", queue: original.queue },
    { ...original, client_id: "secondary" },
    { ...original, queue: { state: "running", owner: "primary" } },
    { ...original, queue: { state: "idle" } },
    { ...original, queue: { state: "failed", owner: "primary" } },
    { ...original, queue: { state: "cancelled", owner: "secondary" } },
  ]) {
    const page = dashboard();
    await page.update(snapshot({ recovery_required: false }));
    page.gateway(original); page.acknowledge();
    page.gateway({ ...original, read_at: 123, label: "Device", queue: { ...original.queue, label: "Cloud" } });
    assert.equal(page.dom["recovery-ack"].checked, true);
    page.gateway(changed);
    assert.equal(page.dom["recovery-ack"].checked, false);
    assert.equal(page.dom["recovery-button"].disabled, true);
    await page.context.send("/api/recovery", { acknowledged: true });
    assert.deepEqual(page.requests, []);
  }
});

test("external recovery respects busy, Stop, readiness, and preview-only modes", async () => {
  for (const overrides of [{ busy: true }, { stop_in_flight: true }, { ready: false }, { mode: "demo" }, { mode: "dry_run" }]) {
    const page = dashboard();
    await page.update(snapshot({ recovery_required: false, ...overrides }));
    page.gateway({ status: "idle", queue: { state: "failed", owner: "primary" } });
    page.acknowledge();
    assert.equal(page.dom["recovery-button"].disabled, true);
    if (overrides.mode) assert.equal(page.dom["recovery-panel"].hidden, true);
    await page.context.send("/api/recovery", { acknowledged: true });
    assert.deepEqual(page.requests, []);
  }
});

test("same-owner terminal queue replacement requires a new inspection acknowledgement", async () => {
  const page = dashboard();
  await page.update(snapshot({ recovery_required: false }));
  const observed = { status: "idle", client_id: null,
    queue: { state: "cancelled", owner: "primary", fingerprint: "a".repeat(64) } };
  page.gateway(observed); page.acknowledge();
  page.gateway({ ...observed, updated_at: "later poll" });
  assert.equal(page.dom["recovery-ack"].checked, true);
  page.gateway({ ...observed, queue: { ...observed.queue, fingerprint: "b".repeat(64) } });
  assert.equal(page.dom["recovery-panel"].hidden, false);
  assert.equal(page.dom["recovery-ack"].checked, false);
  assert.equal(page.dom["recovery-button"].disabled, true);
  await page.context.send("/api/recovery", { acknowledged: true });
  assert.deepEqual(page.requests, []);
});
