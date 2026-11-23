/* Read-only device indicator: real ownership, transitions, and connection loss. */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "web_static/app.js"), "utf8");
const script = source.slice(source.indexOf("/* Read-only device readback."));
const html = fs.readFileSync(path.join(__dirname, "web_static/index.html"), "utf8");
const ui = () => ({ mode: "live", ready: true, busy: false,
  devices: { active_id: "mac", selected_id: "pi", switching: false,
    entries: [{ id: "mac", label: "MacBook", available: true }, { id: "pi", label: "Raspberry Pi", available: true }] } });
const owner = (role, label, device) => ({ status: "active", client_id: role, label, device_id: device, queue: { state: "idle" } });

async function page(initial = { status: "unconfigured" }, initialUi = ui()) {
  const dom = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => [m[1], {
    textContent: "", hidden: false, dataset: {}, attributes: {},
    setAttribute(key, value) { this.attributes[key] = value; },
    removeAttribute(key) { delete this.attributes[key]; },
    set innerHTML(_) { throw new Error("Never inject device names as HTML"); },
  }]));
  let reply = initial;
  const listeners = {}, timers = [], requests = [], events = [];
  const context = vm.createContext({ document: { getElementById: (id) => dom[id] },
    window: { addEventListener: (name, cb) => { listeners[name] = cb; }, dispatchEvent: (event) => events.push(event) },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } }, AbortController,
    setTimeout: (callback, delay) => { if (delay === 1500) timers.push(callback); return 1; }, clearTimeout() {},
    fetch: async (url, options) => {
      requests.push([url, options]);
      if (reply instanceof Error) throw reply;
      return { ok: !reply.httpStatus, status: reply.httpStatus || 200, json: async () => reply };
    },
  });
  vm.runInContext(script, context);
  const settle = async () => { for (let i = 0; i < 8; ++i) await Promise.resolve(); };
  await settle();
  const result = { dom, requests, events, show: (value = ui()) => listeners["assistant-state"]({ detail: value }),
    disconnect: () => listeners["assistant-disconnected"](),
    async update(value) { reply = value; assert.ok(timers.length); await timers.shift()(); await settle(); },
    name: () => dom["active-device-name"].textContent, status: () => dom["active-device-state-text"].textContent };
  if (initialUi) result.show(initialUi);
  return result;
}

test("an unsubmitted menu choice never replaces the confirmed UI device", async () => {
  const p = await page({ httpStatus: 404 });
  assert.equal(p.name(), "MacBook"); assert.equal(p.status(), "Ready");
  assert.equal(p.dom["active-device-eyebrow"].textContent, "UI WORKFLOW DEVICE");
  assert.equal(p.dom["active-device-route"].hidden, true);
  p.show({ ...ui(), busy: true }); assert.equal(p.status(), "Running");
  p.show({ ...ui(), devices: { ...ui().devices, switching: true } });
  assert.equal(p.name(), "Switching device…");
});

test("actual cloud to Pi to Lynx control takes priority over the UI device", async () => {
  const p = await page(owner("primary", "Cloud", "cloud"));
  for (const [role, label, id] of [["primary", "Cloud", "cloud"], ["secondary", "Raspberry Pi", "pi"], ["tertiary", "Lynx", "lynx"]]) {
    await p.update(owner(role, label, id));
    assert.equal(p.name(), label); assert.equal(p.status(), "In control");
    assert.equal(p.dom["active-device-" + id].attributes["aria-current"], "true");
    for (const other of ["cloud", "pi", "lynx"].filter((value) => value !== id)) assert.equal(p.dom["active-device-" + other].attributes["aria-current"], undefined);
  }
  assert.ok(p.requests.every(([url, options]) => url === "/api/active_device" && !options.method && !options.body));
});

test("configured local primary labels the idle route as well as active ownership", async () => {
  const names = { cloud: "MacBook", pi: "Raspberry Pi", lynx: "Lynx" };
  const p = await page({ status: "idle", names, queue: null });
  assert.equal(p.name(), "No active device");
  assert.equal(p.dom["active-device-cloud"].textContent, "MacBook");
  assert.equal(p.dom["active-device-route"].hidden, false);
  await p.update({ ...owner("primary", "MacBook", "cloud"), names });
  assert.equal(p.name(), "MacBook");
  assert.equal(p.dom["active-device-cloud"].attributes["aria-current"], "true");
  await p.update({ status: "idle", names: { ...names, cloud: "Windows PC" }, queue: null });
  assert.equal(p.dom["active-device-cloud"].textContent, "Windows PC");
  await p.update({ status: "idle", queue: null });
  assert.equal(p.dom["active-device-cloud"].textContent, "Cloud", "Unconfigured monitors retain their original role label");
});

test("a previous owner's queue is distinguished from the new control owner", async () => {
  const p = await page({ ...owner("tertiary", "Lynx", "lynx"), queue: { state: "running", owner: "secondary", label: "Raspberry Pi" } });
  assert.equal(p.name(), "Lynx"); assert.equal(p.status(), "Taking over");
  assert.match(p.dom["active-device-detail"].textContent, /queue from Raspberry Pi/);
  await p.update({ status: "idle", queue: { state: "running", owner: "secondary", label: "Raspberry Pi" } });
  assert.equal(p.name(), "Raspberry Pi"); assert.equal(p.status(), "Finishing queue");
  assert.match(p.dom["active-device-detail"].textContent, /no device currently holds control/);
  assert.equal(p.dom["active-device-pi"].attributes["aria-current"], undefined);
});

test("idle and gateway or dashboard connection loss clear active claims", async () => {
  const p = await page(owner("tertiary", "Lynx", "lynx"));
  await p.update({ status: "idle", queue: null });
  assert.equal(p.name(), "No active device"); assert.equal(p.status(), "Idle");
  await p.update(new Error("offline"));
  assert.equal(p.name(), "Device unconfirmed"); assert.equal(p.status(), "Unavailable");
  assert.notEqual(p.dom["active-device-panel"].dataset.tone, "active");
  await p.update(owner("primary", "Cloud", "cloud")); p.disconnect();
  assert.equal(p.name(), "Status unavailable"); assert.equal(p.status(), "Disconnected");
  await p.update(owner("secondary", "Raspberry Pi", "pi"));
  assert.equal(p.status(), "Disconnected", "Separate polling must not clear main dashboard disconnection");
  p.show(); assert.equal(p.name(), "Raspberry Pi");
});

test("failed queues and unavailable UI devices never show a ready badge", async () => {
  const p = await page({ status: "idle", queue: { state: "failed", owner: "primary", label: "Cloud" } });
  assert.equal(p.status(), "Queue failed");
  await p.update({ status: "unconfigured" });
  const snapshot = ui(); snapshot.devices.entries[0].available = false; p.show(snapshot);
  assert.equal(p.status(), "Unavailable");
  snapshot.devices.entries[0].available = true; snapshot.devices.entries[0].status = "Starting"; p.show(snapshot);
  assert.equal(p.status(), "Starting");
});

test("demo and dry run do not present live control as their execution device", async () => {
  const p = await page(owner("primary", "Cloud", "cloud"));
  p.show({ ...ui(), mode: "demo" }); assert.equal(p.name(), "Demo workspace"); assert.equal(p.status(), "Preview");
  p.show({ ...ui(), mode: "dry_run" }); assert.equal(p.name(), "MacBook"); assert.equal(p.status(), "Dry run");
  assert.equal(p.dom["active-device-route"].hidden, true);
  const failed = ui(); failed.mode = "dry_run"; failed.devices.entries[0].status = "Startup failed";
  p.show(failed); assert.equal(p.status(), "Unavailable");
  assert.equal(p.dom["active-device-panel"].dataset.tone, "attention");
  failed.devices.entries[0].status = "Starting"; p.show(failed); assert.equal(p.status(), "Starting");
  failed.devices.switching = true; p.show(failed); assert.equal(p.status(), "Switching");
});

test("connection failure before the first state response shows disconnected", async () => {
  const p = await page({ status: "unconfigured" }, null);
  assert.equal(p.name(), "Connecting…");
  p.disconnect(); assert.equal(p.name(), "Status unavailable"); assert.equal(p.status(), "Disconnected");
  p.show(); assert.equal(p.name(), "MacBook"); assert.equal(p.status(), "Ready");
});

test("unknown roles and labels stay plain text without guessing a device", async () => {
  const p = await page(owner("operator", "Operator", null));
  assert.equal(p.name(), "Operator"); assert.equal(p.dom["active-device-panel"].dataset.device, "unknown");
  await p.update(owner("unknown", "<img src=x>", null));
  assert.equal(p.name(), "<img src=x>");
  await p.update({ status: "not-real", device_id: "cloud" });
  assert.equal(p.name(), "Device unconfirmed");
});

test("read-only polling shares current gateway evidence with recovery controls", async () => {
  const terminal = { status: "idle", client_id: null, queue: { state: "cancelled", owner: "secondary" } };
  const p = await page(terminal);
  assert.equal(p.events.at(-1).type, "gateway-state");
  assert.deepEqual(p.events.at(-1).detail, terminal);
  await p.update(new Error("offline"));
  assert.equal(p.events.at(-1).detail.status, "unavailable");
  assert.ok(p.requests.every(([url, options]) => url === "/api/active_device" && !options.method && !options.body));
});
