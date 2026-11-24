/* Offline tab accessibility, proposal attention and global Stop checks. */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const script = fs.readFileSync(path.join(__dirname, "web_static/app.js"), "utf8");
function section(start, end) {
  const a = script.indexOf(start), b = script.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `Missing production section: ${start}`);
  return script.slice(a, b);
}
function snapshot(overrides = {}) {
  return { instance_id: "router-1", device_epoch: "owner-1", ready: true, busy: false, mode: "live",
    stop_requested: false, recovery_required: false, proposal: null, robot_controls: { proposal: null }, ...overrides };
}
function dashboard(initial = snapshot(), reducedMotion = false) {
  const requests = [], focus = [], scrolls = [], dom = {};
  const ids = ["nav-pick", "nav-assistant", "nav-robot", "nav-settings", "nav-scene", "view-scene", "view-pick", "view-assistant", "view-robot", "view-settings",
    "stop-button", "stop-panel", "recovery-panel", "connection-alert", "error-banner", "proposal-panel", "robot-control-proposal"];
  for (const id of ids) {
    const listeners = {};
    dom[id] = { hidden: false, disabled: false, tabIndex: 0, attributes: {},
      setAttribute(key, value) { this.attributes[key] = String(value); },
      addEventListener(name, fn) { listeners[name] = fn; },
      focus() { focus.push(id); },
      scrollIntoView(options) { scrolls.push({ id, ...options }); },
      click() { listeners.click?.(); },
      key(key, modifiers = {}) { const event = { key, prevented: false, preventDefault() { this.prevented = true; }, ...modifiers };
        listeners.keydown?.(event); return event; },
    };
  }
  const context = vm.createContext({ dom, state: initial, connected: true, localStopInhibit: false,
    posting: false, stopPosting: false, fetch: (...args) => { requests.push({ kind: "fetch", args }); throw new Error("Navigation must not request HTTP"); },
    post: (url) => requests.push({ kind: "post", url }),
    window: { matchMedia: (query) => { assert.equal(query, "(prefers-reduced-motion: reduce)"); return { matches: reducedMotion }; } },
  });
  vm.runInContext(section("  // Workspace navigation changes", "  // End workspace navigation.")
    + section("  const idle =", "  const isStop =")
    + section('  dom["stop-button"].addEventListener', '  dom["status-button"].addEventListener')
    + "\nglobalThis.render = renderNavigation; globalThis.activate = activateWorkspaceTab; globalThis.stopAllowed = canStop; initializeNavigation();", context);
  return { dom, context, requests, focus, scrolls, render() { context.render(); },
    selected() { return ["pick", "assistant", "robot", "scene", "settings"].filter((tab) => dom[`nav-${tab}`].attributes["aria-selected"] === "true"); },
    assertActive(tab) {
      assert.deepEqual(this.selected(), [tab]);
      for (const id of ["pick", "assistant", "robot", "scene", "settings"]) {
        assert.equal(dom[`view-${id}`].hidden, id !== tab);
        assert.equal(dom[`nav-${id}`].tabIndex, id === tab ? 0 : -1);
      }
    } };
}

test("default Pick and manual tabs use one roving focus target without HTTP", () => {
  const page = dashboard(); page.assertActive("pick");
  assert.deepEqual(page.focus, [], "Initial rendering must not steal focus");
  for (const tab of ["robot", "settings", "pick"]) {
    page.dom[`nav-${tab}`].click(); page.assertActive(tab);
    assert.equal(page.focus.at(-1), `nav-${tab}`);
  }
  page.context.activate("not-a-tab", true); page.assertActive("pick");
  assert.deepEqual(page.requests, []);
});

test("arrow keys wrap, Home and End jump, and unrelated keys retain browser behavior", () => {
  const page = dashboard();
  for (const [from, key, to] of [["pick", "ArrowRight", "assistant"], ["assistant", "ArrowRight", "robot"], ["robot", "ArrowDown", "scene"], ["scene", "ArrowDown", "settings"],
    ["settings", "ArrowRight", "pick"], ["pick", "ArrowLeft", "settings"], ["settings", "ArrowUp", "scene"],
    ["robot", "Home", "pick"], ["pick", "End", "settings"]]) {
    const event = page.dom[`nav-${from}`].key(key);
    assert.equal(event.prevented, true); page.assertActive(to);
    assert.equal(page.focus.at(-1), `nav-${to}`);
  }
  for (const key of ["Tab", "Enter", " ", "Escape", "PageDown"]) {
    assert.equal(page.dom["nav-settings"].key(key).prevented, false); page.assertActive("settings");
  }
  for (const modifier of ["altKey", "ctrlKey", "metaKey"]) {
    assert.equal(page.dom["nav-settings"].key("ArrowLeft", { [modifier]: true }).prevented, false);
    page.assertActive("settings");
  }
  assert.deepEqual(page.requests, []);
});

test("a new picking proposal opens Pick once, then respects manual tab choices", () => {
  const page = dashboard(); page.dom["nav-settings"].click();
  page.context.state.proposal = { token: "pick-one", action: "goto", cup_id: 2 };
  page.render(); page.assertActive("pick"); assert.equal(page.focus.at(-1), "nav-pick");
  page.dom["nav-robot"].click(); const focused = page.focus.length;
  for (const expires_in of [30, 29, 28]) {
    page.context.state.proposal.expires_in = expires_in;
    page.render(); page.assertActive("robot");
  }
  assert.equal(page.focus.length, focused);
  page.context.state.proposal = null; page.render(); page.assertActive("robot");
  page.context.state.proposal = { token: "pick-one" }; page.render(); page.assertActive("robot");
  page.context.state.proposal = { token: "pick-two" }; page.render(); page.assertActive("pick");
  assert.deepEqual(page.requests, []);
});

test("Assistant previews stay in Assistant and do not ask for a second Pick-panel confirmation", () => {
  const page = dashboard(); page.dom["nav-assistant"].click();
  page.context.state.proposal = { token: "assistant-pick", assistant_request_id: "request-1", action: "goto", cup_id: 2 };
  page.render(); page.assertActive("assistant");
  page.dom["nav-settings"].click(); page.render(); page.assertActive("settings");
  const hidden = {};
  const context = vm.createContext({ state: page.context.state, localStopInhibit: false,
    hide: (id, value) => { hidden[id] = value; } });
  vm.runInContext(section("  function renderProposal()", "  function renderSavedCups()") + "\nrenderProposal();", context);
  assert.equal(hidden["proposal-panel"], true);
  assert.deepEqual(page.requests, []);
});

test("new robot previews open Robot without reselecting it on ordinary polling", () => {
  const page = dashboard();
  page.context.state.robot_controls.proposal = { token: "home-one", action: "home" };
  page.render(); page.assertActive("robot"); assert.equal(page.focus.at(-1), "nav-robot");
  page.dom["nav-settings"].click(); const focused = page.focus.length;
  page.context.state.busy = true; page.context.state.operation = "robot_status"; page.render(); page.assertActive("settings");
  page.context.state.busy = false; page.context.state.mode = "dry_run"; page.render(); page.assertActive("settings");
  assert.equal(page.focus.length, focused);
  page.context.state.robot_controls.proposal = { token: "home-two", action: "home" }; page.render(); page.assertActive("robot");
  assert.deepEqual(page.requests, []);
});

test("navigation handles first loaded previews and device-bound token identities", () => {
  const page = dashboard(snapshot({ robot_controls: { proposal: { token: "same-token" } } }));
  page.assertActive("robot");
  page.dom["nav-settings"].click();
  page.context.state.device_epoch = "owner-2"; page.context.state.robot_controls.proposal = null;
  page.render(); page.assertActive("settings");
  page.context.state.robot_controls.proposal = { token: "same-token" }; page.render(); page.assertActive("robot");
  page.dom["nav-settings"].click();
  page.context.state.instance_id = "router-2"; page.render(); page.assertActive("robot");
  assert.deepEqual(page.requests, []);
});

test("Stop and disconnect do not draw attention to an inhibited preview", () => {
  for (const inhibit of [page => { page.context.localStopInhibit = true; },
    page => { page.context.state.stop_requested = true; }, page => { page.context.connected = false; }]) {
    const page = dashboard(); page.dom["nav-settings"].click(); inhibit(page);
    page.context.state.proposal = { token: "inhibited-preview" }; page.render(); page.assertActive("settings");
    page.context.localStopInhibit = false; page.context.state.stop_requested = false; page.context.connected = true;
    page.render(); page.assertActive("settings");
    assert.deepEqual(page.requests, []);
  }
});

test("tabs remain local and usable during work, while global Stop keeps its route", () => {
  const page = dashboard(snapshot({ busy: true, recovery_required: true, devices: { switching: true, active_id: "lynx" } }));
  page.context.posting = true;
  const globals = ["stop-panel", "recovery-panel", "connection-alert", "error-banner"];
  globals.forEach((id, index) => { page.dom[id].hidden = index % 2 === 0; });
  const original = globals.map((id) => page.dom[id].hidden);
  for (const tab of ["robot", "settings", "pick"]) {
    page.dom[`nav-${tab}`].click(); page.assertActive(tab);
    assert.equal(page.context.stopAllowed(), true);
    assert.equal(page.dom["stop-button"].hidden, false);
    assert.deepEqual(globals.map((id) => page.dom[id].hidden), original);
  }
  assert.deepEqual(page.requests, []);
  page.dom["stop-button"].click();
  assert.deepEqual(page.requests, [{ kind: "post", url: "/api/stop" }]);
});

test("malformed or absent preview tokens never force a tab", () => {
  const page = dashboard(); page.dom["nav-settings"].click();
  for (const token of [undefined, null, "", 123, true, {}]) {
    page.context.state.proposal = { token };
    page.context.state.robot_controls.proposal = { token };
    page.render(); page.assertActive("settings");
  }
  assert.deepEqual(page.requests, []);
});

test("new previews scroll into view once, respecting reduced motion without focusing confirmation", () => {
  for (const reducedMotion of [false, true]) {
    const page = dashboard(snapshot(), reducedMotion);
    assert.deepEqual(page.scrolls, []);
    page.context.state.proposal = { token: "new-pick" }; page.render();
    assert.deepEqual(page.scrolls, [{ id: "proposal-panel", behavior: reducedMotion ? "instant" : "smooth", block: "start" }]);
    page.render(); page.dom["nav-settings"].click(); page.render();
    assert.equal(page.scrolls.length, 1, "Repeated polls and manual tab changes never scroll back");
    page.context.state.robot_controls.proposal = { token: "new-home" }; page.render();
    assert.deepEqual(page.scrolls[1], { id: "robot-control-proposal", behavior: reducedMotion ? "instant" : "smooth", block: "start" });
    page.render(); page.context.state.proposal = null; page.context.state.robot_controls.proposal = null; page.render();
    assert.equal(page.scrolls.length, 2);
    assert.ok(page.focus.every((id) => id.startsWith("nav-")), "Only tab buttons may receive navigation focus");
    assert.deepEqual(page.requests, []);
  }
});
