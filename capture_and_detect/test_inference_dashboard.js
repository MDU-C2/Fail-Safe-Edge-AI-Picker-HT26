/* Run with node --test capture_and_detect/test_inference_dashboard.js. No browser or hardware. */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "web_static/app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "web_static/index.html"), "utf8");
const start = script.indexOf("  // Provider evidence comes only from the controller");
const end = script.indexOf("\n  function renderStop()", start);
assert.ok(start > 0 && end > start, "Inference renderer must exist in the production dashboard");

function element() {
  return {
    textContent: "", hidden: false, dataset: {}, children: [], replacements: 0,
    replaceChildren() { this.children = []; this.replacements += 1; },
    append(...items) { this.children.push(...items); },
    set innerHTML(_) { throw new Error("Inference data must never become HTML"); },
  };
}

function dashboard() {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(new Set(ids).size, ids.length, "Dashboard IDs must remain unique");
  const dom = Object.fromEntries(ids.map((id) => [id, element()]));
  const context = vm.createContext({
    state: null, connected: false, dom, inferenceEventsKey: "",
    text(id, value) { assert.ok(dom[id], `Missing markup for ${id}`); dom[id].textContent = String(value ?? ""); },
    hide(id, hidden) { assert.ok(dom[id], `Missing markup for ${id}`); dom[id].hidden = hidden; },
    clockText(value) { const date = new Date(value); return Number.isNaN(date.getTime()) ? "" : date.toISOString().slice(11, 19); },
    document: { createElement: element },
  });
  vm.runInContext(script.slice(start, end) + "\nglobalThis.renderInference = renderInference;", context);
  return {
    dom,
    render(inference, { connected = true, mode = "dry_run", initial = false } = {}) {
      context.state = initial ? null : { mode, inference };
      context.connected = connected;
      context.renderInference();
    },
  };
}

function snapshot(overrides = {}) {
  return {
    enabled: true, local_provider: "pi", state: "mac_active", selected_provider: "mac", active_provider: "mac", in_flight_provider: null,
    reason: "A result was accepted.", session_id: "session-1",
    last_result: { provider: "mac", request_id: "request-1", observation_id: "observation-9", duration_ms: 83.25, accepted_at: "2026-11-06T11:42:01Z" },
    counters: { mac: 4, pi: 2 }, recovery: { successes: 0, required: 3, ready: false }, events: [],
    ...overrides,
  };
}

test("accepted provider remains distinct from routing and a result in flight", () => {
  const { render, dom } = dashboard();
  render(snapshot({ state: "switching_to_pi", selected_provider: "pi", in_flight_provider: "pi" }));
  assert.equal(dom["inference-provider"].textContent, "MacBook");
  assert.equal(dom["inference-selected"].textContent, "Raspberry Pi");
  assert.equal(dom["inference-requested"].textContent, "Raspberry Pi");
  assert.equal(dom["inference-badge"].textContent, "SWITCHING TO PI");
  assert.match(dom["inference-result-time"].textContent, /Accepted at 11:42:01 · 83 ms/);
  assert.equal(dom["inference-observation"].textContent, "observation-9");
});

test("recovery probes do not replace accepted result or increment counters", () => {
  const { render, dom } = dashboard();
  const data = snapshot({ state: "mac_recovering", selected_provider: "pi", in_flight_provider: "mac",
    last_result: { ...snapshot().last_result, provider: "pi", duration_ms: 1354 },
    recovery: { successes: 1, required: 3, next_probe_at: 12345.75, ready: false } });
  render(data);
  assert.equal(dom["inference-provider"].textContent, "Raspberry Pi");
  assert.match(dom["inference-result-time"].textContent, /1.35 s/);
  assert.match(dom["inference-recovery"].textContent, /1\/3/);
  assert.doesNotMatch(dom["inference-recovery"].textContent, /next check at/);
  assert.equal(dom["inference-recovery"].hidden, false);
  assert.equal(dom["inference-counters"].textContent, "Mac 4 · Pi 2");
  render({ ...data, recovery: { successes: 3, required: 3, ready: true }, state: "failback_pending" });
  assert.equal(dom["inference-provider"].textContent, "Raspberry Pi");
  assert.equal(dom["inference-counters"].textContent, "Mac 4 · Pi 2");
  assert.match(dom["inference-recovery"].textContent, /ready for the next request boundary/);
});

test("disconnect marks every displayed source snapshot stale while retaining evidence", () => {
  const { render, dom } = dashboard();
  const data = snapshot();
  render(data);
  render(data, { connected: false });
  assert.equal(dom["inference-badge"].textContent, "STALE · DISCONNECTED");
  assert.equal(dom["inference-panel"].dataset.tone, "stale");
  assert.equal(dom["inference-stale"].hidden, false);
  assert.match(dom["inference-result-time"].textContent, /^Last known:/);
  assert.match(dom["inference-selected"].textContent, /^Last known:/);
  assert.equal(dom["inference-provider"].textContent, "MacBook");
  render(data);
  assert.equal(dom["inference-stale"].hidden, true);
  assert.equal(dom["inference-badge"].textContent, "MAC ACTIVE");
});

test("demo and local-only sessions cannot claim active fallback", () => {
  const { render, dom } = dashboard();
  render(snapshot(), { mode: "demo" });
  assert.equal(dom["inference-badge"].textContent, "DEMO · NO FALLBACK");
  assert.equal(dom["inference-provider"].textContent, "Sample scene");
  assert.equal(dom["inference-counters"].textContent, "Not applicable");
  render(snapshot({ enabled: false, local_provider: "local", selected_provider: "local", state: "local_only" }));
  assert.equal(dom["inference-badge"].textContent, "LOCAL ONLY · NO FALLBACK");
  assert.equal(dom["inference-provider"].textContent, "No accepted result");
  assert.equal(dom["inference-selected"].textContent, "Local model");
  render(snapshot({ state: "local_only" }));
  assert.equal(dom["inference-badge"].textContent, "LOCAL ONLY · NO FALLBACK");
});

test("development fallback displays local evidence without claiming Raspberry Pi", () => {
  const { render, dom } = dashboard();
  render(snapshot({ local_provider: "local", selected_provider: "local", in_flight_provider: "local",
    state: "local_active", counters: { mac: 4, local: 2 },
    last_result: { ...snapshot().last_result, provider: "local" } }));
  assert.equal(dom["inference-provider"].textContent, "Local model");
  assert.equal(dom["inference-selected"].textContent, "Local model");
  assert.equal(dom["inference-requested"].textContent, "Local model");
  assert.equal(dom["inference-counters"].textContent, "Mac 4 · Local 2");
  assert.equal(dom["inference-recovery"].hidden, false);
  render(snapshot({ local_provider: "local", selected_provider: "local", state: "switching_to_pi" }));
  assert.equal(dom["inference-badge"].textContent, "SWITCHING TO LOCAL");
});

test("isolated Pi inference retains source evidence without claiming configured fallback", () => {
  const { render, dom } = dashboard();
  render(snapshot({ enabled: false, selected_provider: "pi", state: "pi_active",
    last_result: { ...snapshot().last_result, provider: "pi" } }));
  assert.equal(dom["inference-badge"].textContent, "LOCAL ONLY · NO FALLBACK");
  assert.equal(dom["inference-provider"].textContent, "Raspberry Pi");
  assert.equal(dom["inference-selected"].textContent, "Raspberry Pi");
  assert.equal(dom["inference-counters"].textContent, "Pi 2");
  assert.equal(dom["inference-recovery"].hidden, true);
  render(snapshot({ enabled: false, selected_provider: "pi", state: "audit_failed" }));
  assert.equal(dom["inference-badge"].textContent, "RECORDING FAILED");
});

test("first Mac selection does not claim recovery checks have already passed", () => {
  const { render, dom } = dashboard();
  render(snapshot({ state: "mac_ready", active_provider: null, last_result: null,
    reason: "Waiting for the first accepted inference result." }));
  assert.equal(dom["inference-badge"].textContent, "MAC PREFERRED");
  assert.equal(dom["inference-provider"].textContent, "No accepted result");
  assert.equal(dom["inference-recovery"].hidden, true);
  assert.doesNotMatch(dom["inference-reason"].textContent, /passed/);
});

test("selection alone never appears as an accepted result", () => {
  const { render, dom } = dashboard();
  render(snapshot({ state: "mac_preferred", last_result: null }));
  assert.equal(dom["inference-provider"].textContent, "No accepted result");
  assert.equal(dom["inference-observation"].textContent, "None accepted");
  assert.equal(dom["inference-selected"].textContent, "MacBook");
});

test("absent and malformed metadata render without a false provider claim", () => {
  const { render, dom } = dashboard();
  for (const value of [undefined, null, [], "bad"]) {
    render(value);
    assert.equal(dom["inference-badge"].textContent, "STATUS UNAVAILABLE");
    assert.equal(dom["inference-provider"].textContent, "No accepted result");
  }
  render(snapshot({ state: "__proto__", last_result: { provider: "unknown" }, counters: { mac: -1, pi: "3" }, events: [null, [], {}, 3] }));
  assert.equal(dom["inference-badge"].textContent, "STATUS UNKNOWN");
  assert.equal(dom["inference-counters"].textContent, "Mac — · Pi —");
  assert.equal(dom["inference-event-count"].textContent, "0");
});

test("timeline shows eight newest server events and is stable across repeated snapshots", () => {
  const { render, dom } = dashboard();
  const events = Array.from({ length: 11 }, (_, index) => ({ id: index, time: "2026-11-06T11:42:00Z", message: `Event ${index}`, level: "info" }));
  const data = snapshot({ events });
  render(data);
  assert.equal(dom["inference-events"].children.length, 8);
  assert.equal(dom["inference-events"].children[0].children[1].textContent, "Event 10");
  const replacements = dom["inference-events"].replacements;
  render(data);
  render(data, { connected: false });
  render(data);
  assert.equal(dom["inference-events"].replacements, replacements);
  render(snapshot({ session_id: "session-2", events: [] }));
  assert.equal(dom["inference-event-count"].textContent, "0");
  assert.equal(dom["inference-events"].children[0].textContent, "No source events reported.");
});

test("provider messages and evidence are rendered as plain text", () => {
  const { render, dom } = dashboard();
  const payload = "<img src=x onerror=alert(1)>";
  render(snapshot({ reason: payload, last_result: { ...snapshot().last_result, observation_id: payload }, events: [{ message: payload, level: payload, time: "invalid", observation_id: payload }] }));
  assert.equal(dom["inference-reason"].textContent, payload);
  assert.equal(dom["inference-observation"].textContent, payload);
  assert.equal(dom["inference-events"].children[0].children[1].textContent, payload);
  assert.equal(dom["inference-events"].children[0].dataset.level, "info");
  assert.equal(dom["inference-events"].children[0].children[0].textContent, "Time unknown");
});

test("initial connection and invalid timing do not invent timestamps", () => {
  const { render, dom } = dashboard();
  render(undefined, { initial: true, connected: false });
  assert.equal(dom["inference-badge"].textContent, "CONNECTING");
  assert.equal(dom["inference-stale"].hidden, true);
  render(snapshot({ last_result: { ...snapshot().last_result, accepted_at: null, duration_ms: -5 } }));
  assert.equal(dom["inference-result-time"].textContent, "Acceptance time unavailable · duration unavailable");
});
