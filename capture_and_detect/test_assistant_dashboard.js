/* Offline tests: no OpenAI, gateway, camera, or microphone is contacted. */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "web_static/assistant.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "web_static/index.html"), "utf8");
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

function element(tag = "div") {
  const handlers = new Map();
  return {
    tag, children: [], attributes: {}, dataset: {}, textContent: "", value: "", checked: false,
    hidden: false, disabled: false, className: "", focused: false, scrollTop: 0, scrollHeight: 0,
    append(...items) { this.children.push(...items); },
    setAttribute(key, value) { this.attributes[key] = String(value); },
    removeAttribute(key) { delete this.attributes[key]; },
    addEventListener(name, callback) { if (!handlers.has(name)) handlers.set(name, []); handlers.get(name).push(callback); },
    async fire(name, event = {}) { for (const callback of handlers.get(name) || []) await callback({ preventDefault() {}, ...event }); },
    click() { return this.fire("click"); },
    requestSubmit() { return this.fire("submit"); }, focus() { this.focused = true; }, scrollIntoView() {},
    set innerHTML(_) { throw new Error("Untrusted content must never be rendered as HTML"); },
  };
}
function allText(node) { return [node.textContent, ...node.children.map(allText)].join(" "); }
function buttons(node) { return node.children.flatMap((child) => [...(child.tag === "button" ? [child] : []), ...buttons(child)]); }
function nodes(node, className) { return node.children.flatMap((child) => [...(child.className === className ? [child] : []), ...nodes(child, className)]); }
const suggestion = (action = "select", extra = {}) => ({ message: "I can pick Cup 2.", action, cup_ids: action === "pick_saved" ? [] : [2], proposal_id: "draft-1", ...extra });
const realProposal = (extra = {}) => ({ assistant_request_id: "draft-1", token: "abcdef12", capture_id: "scan-1", cup_id: 2, action: "goto", cup_ids: [2], placements: [{ id: 10, x: 480, y: -360, z: 230 }], home_after_cup: false, description: "Pick, hand over, place and finish at clearance poses.", expires_in: 60, ...extra });
const actionButton = (p, label) => buttons(p.dom["assistant-messages"]).filter((button) => button.textContent === label).at(-1);
const managedMac = (extra = {}) => ({ assistant_available: true, device_epoch: "epoch-1", devices: { active_id: "mac", selected_id: "mac", switching: false }, ...extra });

async function page(options = {}) {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(ids.length, new Set(ids).size, "HTML IDs remain unique");
  const dom = Object.fromEntries(ids.map((id) => [id, element()]));
  const examples = [...html.matchAll(/data-assistant-example="([^"]+)"/g)].map((match) => {
    const node = element("button"); node.dataset.assistantExample = match[1]; return node;
  });
  const events = [], requests = [], timers = new Map();
  let csrf = "csrf-1", timerNumber = 0, now = Date.now();
  let controller = { instance_id: "mac-1", revision: 1, mode: "live", ready: true, busy: false, scan_usable: true, stop_requested: false, recovery_required: false, inventory: { capture: "scan-1", cups: [{id: 2}] }, proposal: null, ...options.state };
  let status = { configured: true, model: "gpt-6-astra", service_tier: "default", reasoning: "low", remember: false, demo: false, settings_editable: true, ...options.status };
  const window = element("window");
  Object.assign(window, {
    dispatchEvent(event) { events.push(event); return window.fire(event.type, event); },
  });
  const document = element("document");
  Object.assign(document, {
    hidden: false,
    getElementById: (id) => dom[id], createElement: element,
    querySelectorAll: () => examples,
  });
  const fetch = async (url, init = {}) => {
    requests.push({ url, ...init });
    if (options.fetch) { const override = await options.fetch(url, init); if (override) return override; }
    if (url === "/api/assistant/prepare" && options.answer?.action !== "save_cup") controller = { ...controller, revision: controller.revision + 1, ...(options.preparedState || { proposal: realProposal(options.proposal) }) };
    if (url === "/api/confirm") controller = { ...controller, revision: controller.revision + 1, proposal: null, busy: true, operation: "confirm", message: "Picking the cup…" };
    if (url === "/api/cancel") controller = { ...controller, revision: controller.revision + 1, proposal: null, busy: false, operation: null, message: "Preview cancelled. No movement requested." };
    const result = url === "/api/state" ? { ...controller, csrf_token: csrf }
      : url === "/api/assistant/status" ? status
      : url === "/api/assistant/ask" ? options.answer || { message: "Cup 2 is available.", action: "answer", cup_ids: [], context_label: "Scan 1" }
      : url === "/api/assistant/prepare" ? options.answer?.action === "save_cup" ? { accepted: true } : { accepted: true, assistant_request_id: "draft-1" }
      : ["/api/confirm", "/api/cancel"].includes(url) ? { accepted: true }
      : { message: "Connection works." };
    return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(result)) };
  };
  const context = vm.createContext({
    window, document,
    fetch, AbortController, URL, crypto: options.insecure ? {getRandomValues: require("node:crypto").webcrypto.getRandomValues.bind(require("node:crypto").webcrypto)} : require("node:crypto").webcrypto,
    Date: class extends Date { static now() { return now; } }, CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options?.detail; } },
    setTimeout(fn, ms) { const id = ++timerNumber; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  vm.runInContext(source, context);
  await tick();
  return { dom, examples, requests, events, timers, window, document,
    posts: () => requests.filter((request) => request.method === "POST"),
    setStatus(value) { status = { ...status, ...value }; }, setCsrf(value) { csrf = value; },
    setState(value) { controller = { ...controller, revision: controller.revision + 1, ...value }; },
    state: () => controller,
    async publish(value = {}) { controller = { ...controller, revision: controller.revision + 1, ...value }; await window.fire("assistant-state", { detail: JSON.parse(JSON.stringify(controller)) }); await tick(); },
    async advance(ms) { now += ms; const entry = [...timers.entries()].find(([,timer]) => timer.ms === 700); if (entry) { timers.delete(entry[0]); await entry[1].fn(); } await tick(); },
    async refresh() { await window.fire("assistant-refresh"); await tick(); },
    async ask(value) { dom["assistant-input"].value = value; await dom["assistant-input"].fire("input"); return dom["assistant-form"].fire("submit"); },
  };
}

test("assistant settings default to session-only, low reasoning, standard processing", async () => {
  const p = await page();
  assert.equal(p.dom["assistant-remember"].checked, false);
  assert.equal(p.dom["assistant-tier"].value, "default");
  assert.equal(p.dom["assistant-reasoning"].value, "low");
  assert.equal(p.posts().length, 0);
  assert.match(html, /id="assistant-api-key" type="password" autocomplete="off"/);
  assert.doesNotMatch(source, /localStorage|sessionStorage|innerHTML/);
});

test("offline demo works without an API key and is distinguished from OpenAI", async () => {
  const p = await page({ status: { configured: false, demo: true } });
  assert.equal(p.dom["assistant-input"].disabled, false);
  assert.match(p.dom["assistant-source"].textContent, /Local demo.*no OpenAI/);
  await p.ask("Which cups are available?");
  assert.equal(p.posts().length, 1);
  assert.equal(p.posts()[0].url, "/api/assistant/ask");
});

test("keyless live assistant permits local saved-cup commands without claiming GPT", async () => {
  const p = await page({ status: { configured: false }, answer: { message: "Ready to remember Cup 2.", action: "save_cup", cup_ids: [2], cup_name: "Kasun's cup", proposal_id: "local-save", mode: "saved_cup_command" } });
  assert.equal(p.dom["assistant-input"].disabled, false);
  assert.equal(p.dom["assistant-save"].disabled, false);
  assert.equal(p.dom["assistant-source"].textContent, "Saved cup commands · local");
  assert.match(p.dom["assistant-context-note"].textContent, /Save and list cup references without a key.*key in Settings to find a saved cup/);
  await p.ask("Remember cup 2 as Kasun's cup");
  assert.equal(p.posts().length, 1);
  assert.equal(buttons(p.dom["assistant-messages"])[0].textContent, "Save cup reference");
  assert.match(allText(p.dom["assistant-messages"]), /Saved cup command/);
  assert.doesNotMatch(allText(p.dom["assistant-messages"]) + p.dom["assistant-source"].textContent, /GPT|OpenAI/);
});

test("keyless flexible-request errors keep local commands available and prepare no action", async () => {
  const p = await page({ status: { configured: false }, fetch: async (url) => url === "/api/assistant/ask"
    ? { ok: false, status: 422, json: async () => ({ detail: "Add an API key in Settings for this request." }) } : null });
  await p.ask("Find the cup nearest the orange bowl");
  assert.match(p.dom["assistant-error"].textContent, /API key in Settings/);
  assert.equal(p.dom["assistant-input"].disabled, false);
  assert.equal(buttons(p.dom["assistant-messages"]).length, 0);
  assert.equal(p.posts().length, 1);
});

test("unavailable device dashboards block assistant commands while keeping Mac API settings editable", async () => {
  const reason = "Assistant features run on the MacBook controller only. Open the MacBook dashboard to use them.";
  const p = await page({ status: { available: false, unavailable_reason: reason, settings_editable: true } });
  assert.equal(p.dom["assistant-state"].textContent, "Local computer only");
  assert.equal(p.dom["assistant-context-note"].textContent, reason);
  assert.equal(p.dom["assistant-input"].disabled, true);
  assert.equal(p.dom["assistant-send"].disabled, true);
  assert.equal(p.dom["assistant-api-key"].disabled, false);
  await p.ask("Remember cup 2 as Kasun's cup");
  assert.equal(p.posts().length, 0);
  await p.dom["assistant-settings-form"].fire("submit");
  assert.deepEqual(p.posts().map((request) => request.url), ["/api/assistant/settings"]);
});

test("combined dashboard enables only an explicitly owned local Mac and reacts immediately to switching", async () => {
  const p = await page({ state: managedMac(), status: { available: true } });
  await p.publish();
  assert.equal(p.dom["assistant-input"].disabled, false);
  const statusReads = p.requests.filter((request) => request.url === "/api/assistant/status").length;
  await p.publish({ revision: 0, assistant_available: false, device_epoch: "epoch-2", devices: { active_id: "pi", switching: false } });
  assert.equal(p.dom["assistant-input"].disabled, true);
  assert.match(p.dom["assistant-context-note"].textContent, /Select the local computer.*enable the Assistant/);
  assert.equal(p.dom["assistant-save"].disabled, false, "Mac API settings remain editable while Pi is selected");
  assert.ok(p.requests.filter((request) => request.url === "/api/assistant/status").length > statusReads);
  await p.ask("Pick cup 2"); assert.equal(p.posts().length, 0);
  p.setStatus({ available: false });
  await p.publish(managedMac({ device_epoch: "epoch-3" }));
  assert.equal(p.dom["assistant-input"].disabled, false, "current server capability overrides a stale Pi status response");
  for (const change of [{assistant_available:false}, {assistant_available:undefined}, {ready:false}, {device_epoch:""}, {devices:{active_id:"mac",switching:true}}, {devices:{active_id:"lynx",switching:false}}]) {
    await p.publish({...managedMac(),ready:true,...change});
    assert.equal(p.dom["assistant-input"].disabled,true,JSON.stringify(change));
  }
});

test("managed Assistant names the configured local computer while remaining disabled on Pi and Lynx", async () => {
  for (const label of ["MacBook", "Windows PC", "This PC"]) {
    const entries = [{ id: "mac", label }, { id: "pi", label: "Raspberry Pi" }, { id: "lynx", label: "Lynx" }];
    const p = await page({ state: managedMac({ devices: { active_id: "mac", switching: false, entries } }) });
    await p.publish();
    assert.equal(p.dom["assistant-input"].disabled, false);
    for (const device of ["pi", "lynx"]) {
      await p.publish({ assistant_available: false, devices: { active_id: device, switching: false, entries } });
      assert.equal(p.dom["assistant-input"].disabled, true);
      assert.equal(p.dom["assistant-state"].textContent, "Local computer only");
      assert.ok(p.dom["assistant-context-note"].textContent.startsWith(`Select ${label} and use that device`));
    }
    assert.equal(p.posts().length, 0);
  }
});

test("managed Assistant binds ask, prepare, confirmation and cancellation to observed ownership over HTTP", async () => {
  const p = await page({ state: managedMac(), answer: suggestion(), insecure: true });
  await p.ask("Pick cup 2"); await actionButton(p,"Confirm pick").click();
  assert.deepEqual(p.posts().map((request) => request.url), ["/api/assistant/ask", "/api/assistant/prepare", "/api/confirm"]);
  for (const request of p.posts()) {
    const body = JSON.parse(request.body); assert.equal(body._device_id, "mac"); assert.equal(body._device_epoch, "epoch-1");
  }
  const c = await page({ state: managedMac(), answer: suggestion(), insecure: true });
  await c.ask("Pick cup 2"); await actionButton(c,"Cancel").click();
  const cancelled = JSON.parse(c.posts().find((request) => request.url === "/api/cancel").body);
  assert.equal(cancelled._device_id,"mac"); assert.equal(cancelled._device_epoch,"epoch-1"); assert.equal(cancelled.token,"abcdef12");
});

test("device switches invalidate an in-flight AI answer before preparation even after returning to Mac", async () => {
  for (const polled of [true, false]) {
    let release;
    const pending = new Promise((resolve) => { release = resolve; });
    const p = await page({ state: managedMac(), fetch: async (url) => url === "/api/assistant/ask" ? pending : null });
    await p.publish(); const asking = p.ask("Pick the white cup"); await tick();
    const switched = { device_epoch:"epoch-2",assistant_available:false,devices:{active_id:"pi",switching:false} };
    if (polled) { await p.publish(switched); await p.publish(managedMac({device_epoch:"epoch-3"})); }
    else p.setState(switched);
    release({ok:true,json:async()=>suggestion()}); await asking;
    assert.deepEqual(p.posts().map((request)=>request.url),["/api/assistant/ask"]);
    assert.match(allText(p.dom["assistant-messages"]),/workspace changed.*No new pick was prepared/);
  }
});

test("a device transition between answer and automatic preparation cannot silently retarget", async () => {
  let readsAfterAnswer = 0;
  const p = await page({ state: managedMac(), answer: suggestion(), fetch: async (url) => {
    if (url === "/api/assistant/ask") readsAfterAnswer = 1;
    if (url === "/api/state" && readsAfterAnswer > 0 && ++readsAfterAnswer === 3) return {ok:true,json:async()=>({...p.state(),device_epoch:"new-owner" ,csrf_token:"csrf"})};
    return null;
  } });
  await p.ask("Pick cup 2");
  assert.deepEqual(p.posts().map((request)=>request.url),["/api/assistant/ask"]);
  assert.match(allText(p.dom["assistant-messages"]),/controller or device changed/);
});

test("automatic camera refresh shows progress and refreshed numbers without issuing another scan or prepare", async () => {
  let release;
  const pending = new Promise((resolve)=>{release=resolve;});
  const p = await page({state:managedMac(),fetch:async(url)=>url==="/api/assistant/ask"?pending:null});
  await p.publish(); const asking=p.ask("Pick cup 2"); await tick();
  await p.publish({busy:true,operation:"scan",message:"Taking a fresh workspace photo automatically…"});
  assert.equal(p.dom["assistant-state"].textContent,"Taking photo…");
  assert.match(p.dom["assistant-context-note"].textContent,/fresh workspace photo automatically/);
  await p.publish({busy:false,operation:null,inventory:{capture:"new-photo",image_url:"/api/image/new-photo",cups:[{id:2}]}});
  release({ok:true,json:async()=>({message:"A new photo is ready. Check the cup numbers before asking again.",action:"answer",mode:"camera_command",camera_refreshed:true,capture_id:"new-photo"})});
  await asking;
  assert.match(allText(p.dom["assistant-messages"]),/Fresh photo · new-photo/);
  assert.match(allText(p.dom["assistant-messages"]),/Camera result · no AI request/);
  assert.doesNotMatch(allText(p.dom["assistant-messages"]),/GPT-6 Astra response/);
  const photo=nodes(p.dom["assistant-messages"],"assistant-camera-result assistant-pick-scan")[0];
  assert.equal(photo.open,true); assert.equal(photo.children[1].attributes.src,"/api/image/new-photo");
  assert.deepEqual(p.posts().map((request)=>request.url),["/api/assistant/ask"]);
});

test("camera replies display only the exact current capture through a local image route", async () => {
  for (const inventory of [{capture:"other-photo",image_url:"/api/image/new-photo"},{capture:"new-photo",image_url:"https://example.com/photo"},{capture:"new-photo",image_url:"javascript:alert(1)"}]) {
    const p=await page({state:{inventory},answer:{action:"answer",mode:"camera_command",camera_refreshed:true,capture_id:"new-photo"}});
    await p.ask("Take a fresh photo");
    assert.equal(nodes(p.dom["assistant-messages"],"assistant-camera-result assistant-pick-scan").length,0);
  }
});

test("a saved-reference submission cannot cross device ownership", async () => {
  const p = await page({state:managedMac(),answer:{action:"save_cup",proposal_id:"save-1",cup_name:"Cup",cup_ids:[2]}});
  await p.ask("Remember cup 2 as Cup"); p.setState({device_epoch:"epoch-2"});
  await actionButton(p,"Save cup reference").click();
  assert.deepEqual(p.posts().map((request)=>request.url),["/api/assistant/ask"]);
  assert.match(p.dom["assistant-error"].textContent,/controller or device changed/);
});

test("configured demo identifies GPT on a sample scene instead of claiming offline responses", async () => {
  const p = await page({ status: { configured: true, demo: true }, answer: { message: "Two sample cups.", mode: "gpt", context_label: "Sample scene" } });
  assert.match(p.dom["assistant-source"].textContent, /GPT-6 Astra.*OpenAI.*sample scene/);
  assert.doesNotMatch(p.dom["assistant-source"].textContent, /no OpenAI|Local demo/);
  assert.equal(p.dom["assistant-state"].textContent, "Sample scene");
  await p.ask("Which cups?");
  assert.match(allText(p.dom["assistant-messages"]), /GPT-6 Astra response/);
});

test("local named-cup commands are distinguished from GPT even with a key configured", async () => {
  const p = await page({ answer: { message: "Ready to remember this cup.", mode: "saved_cup_command", context_label: "Current scan" } });
  await p.ask("Remember cup 2 as Kasun's cup");
  assert.match(allText(p.dom["assistant-messages"]), /Current scan · Saved cup command/);
  assert.doesNotMatch(allText(p.dom["assistant-messages"]), /GPT-6 Astra response|Local demo response/);
});

test("example chips only fill an editable draft", async () => {
  const p = await page(); await p.examples[1].click();
  assert.equal(p.dom["assistant-input"].value, "Remember cup 2 as Kasun's cup");
  await p.examples[3].click();
  assert.equal(p.dom["assistant-input"].value, "Pick Kasun's cup");
  assert.equal(p.examples.length, 4, "named cup examples do not clutter the welcome screen");
  assert.equal(p.posts().length, 0);
});

test("saving a named cup requires an explicit no-motion action and does not claim completion", async () => {
  const cupName = '<img src=x onerror="alert(1)">';
  const p = await page({ answer: { message: "Ready to remember this cup.", action: "save_cup", cup_ids: [2], cup_name: cupName, proposal_id: "save-2" } });
  await p.ask("Remember cup 2");
  const save = buttons(p.dom["assistant-messages"])[0];
  assert.equal(save.textContent, "Save cup reference");
  assert.ok(allText(p.dom["assistant-messages"]).includes(`Saves Cup 2 as “${cupName}” from this scan. No robot movement.`));
  assert.equal(p.posts().length, 1, "an answer alone does not save a reference");
  await save.click();
  assert.deepEqual(p.posts().map((request) => request.url), ["/api/assistant/ask", "/api/assistant/prepare"]);
  assert.deepEqual(JSON.parse(p.posts()[1].body), { proposal_id: "save-2" });
  assert.equal(save.textContent, "Save requested", "202 acceptance is not a completed save");
  assert.equal(save.disabled, true);
  assert.equal(p.events[0].type, "assistant-preview-ready", "the existing hook refreshes Pick cups");
  await save.click(); assert.equal(p.posts().length, 2, "the same save cannot be submitted twice");
});

test("settings sends key once, honors explicit remember, then clears the textbox", async () => {
  const p = await page(); p.dom["assistant-api-key"].value = "secret-example-key";
  p.dom["assistant-remember"].checked = true; p.dom["assistant-tier"].value = "flex";
  await p.dom["assistant-settings-form"].fire("submit");
  assert.deepEqual(JSON.parse(p.posts()[0].body), { api_key: "secret-example-key", remember: true, service_tier: "flex", reasoning: "low" });
  assert.equal(p.dom["assistant-api-key"].value, "");
  assert.ok(!allText(p.dom["view-assistant"]).includes("secret-example-key"));
  await p.dom["assistant-settings-form"].fire("submit");
  assert.ok(!("api_key" in JSON.parse(p.posts()[1].body)), "blank key preserves existing credentials");
});

test("Windows session-only settings hide and clear Remember and never submit a persistence request", async () => {
  const p = await page({ status: { remember_supported: false, remember: true } });
  assert.equal(p.dom["assistant-remember-option"].hidden, true);
  assert.equal(p.dom["assistant-remember"].disabled, true);
  assert.equal(p.dom["assistant-remember"].checked, false);
  p.dom["assistant-api-key"].value = "secret-example-session-key";
  p.dom["assistant-remember"].checked = true;
  p.dom["assistant-remember"].disabled = false;
  await p.dom["assistant-settings-form"].fire("submit");
  assert.equal(p.posts().length, 1);
  assert.equal(JSON.parse(p.posts()[0].body).remember, false);
  assert.equal(p.dom["assistant-remember"].checked, false);
  assert.equal(p.dom["assistant-api-key"].value, "");
  assert.match(p.dom["assistant-settings-status"].textContent, /this service session/);
});

test("a failed key save never exposes a secret in an error and clears the input", async () => {
  const p = await page({ fetch: async (url) => url === "/api/assistant/settings" ? { ok: false, json: async () => ({ detail: "Invalid secret-example-key" }) } : null });
  p.dom["assistant-api-key"].value = "secret-example-key";
  await p.dom["assistant-settings-form"].fire("submit");
  assert.equal(p.dom["assistant-api-key"].value, "");
  assert.ok(!p.dom["assistant-error"].textContent.includes("secret-example-key"));
});

test("assistant requests do not disable the existing robot Stop control", async () => {
  let resolve;
  const pending = new Promise((done) => { resolve = done; });
  const p = await page({ fetch: async (url) => url === "/api/assistant/ask" ? pending : null });
  p.dom["stop-button"].disabled = false;
  const ask = p.ask("Explain the grid"); await tick();
  assert.equal(p.dom["assistant-send"].disabled, true);
  assert.equal(p.dom["stop-button"].disabled, false);
  resolve({ ok: true, json: async () => ({ message: "Here is the grid." }) }); await ask;
});

test("phone setup shows a validated address and loopback pairing code", async () => {
  const p = await page({ status: { phone: { origin: "http://192.168.1.24:8087", pairing_code: "123456" } } });
  assert.equal(p.dom["assistant-phone-setup"].hidden, false);
  assert.equal(p.dom["assistant-phone-link"].attributes.href, "http://192.168.1.24:8087");
  assert.equal(p.dom["assistant-phone-link"].textContent, "http://192.168.1.24:8087");
  assert.equal(p.dom["assistant-phone-code"].textContent, "123456");
  assert.equal(p.dom["assistant-phone-code-row"].hidden, false);
});

test("paired phone status does not display a missing pairing code", async () => {
  const p = await page({ status: { phone: { origin: "http://macbook.local:8087", pairing_code: null } } });
  assert.equal(p.dom["assistant-phone-setup"].hidden, false);
  assert.equal(p.dom["assistant-phone-code-row"].hidden, true);
  assert.equal(p.dom["assistant-phone-code"].textContent, "");
});

test("phone setup rejects script URLs and credential-bearing origins", async () => {
  for (const origin of ["javascript:alert(1)", "https://user:password@example.com", "https://example.com/path", "https://example.com/?key=secret"]) {
    const p = await page({ status: { phone: { origin, pairing_code: "123456" } } });
    assert.equal(p.dom["assistant-phone-setup"].hidden, true);
    assert.equal(p.dom["assistant-phone-link"].attributes.href, undefined);
    assert.equal(p.dom["assistant-phone-code"].textContent, "");
  }
});

test("API connection and phone access live in Settings, with no voice or certificate UI", () => {
  const assistantStart = html.indexOf('<section id="view-assistant"');
  const settingsStart = html.indexOf('<section id="view-settings"');
  const chat = html.slice(assistantStart, settingsStart);
  assert.ok(assistantStart >= 0 && settingsStart > assistantStart);
  assert.doesNotMatch(chat, /id="assistant-(?:api-key|settings-form|phone-setup)"/);
  for (const id of ["assistant-api-key", "assistant-settings-form", "assistant-phone-setup"]) assert.ok(html.indexOf(`id="${id}"`) > settingsStart);
  assert.doesNotMatch(html, /assistant-record|assistant-speak|assistant-phone-certificate|Certificate Trust|Record voice/);
  assert.doesNotMatch(source, /MediaRecorder|getUserMedia|speechSynthesis|transcribe|certificate_url/);
});

test("phone clients can ask but cannot save, test or forget API settings", async () => {
  const p = await page({ status: { settings_editable: false, configured: true } });
  assert.equal(p.dom["assistant-input"].disabled, false);
  assert.equal(p.dom["assistant-settings-access"].hidden, false);
  for (const id of ["api-key", "remember", "tier", "reasoning", "save", "test", "forget"]) assert.equal(p.dom[`assistant-${id}`].disabled, true);
  p.dom["assistant-api-key"].value = "secret-not-to-transmit";
  for (const id of ["save", "test", "forget"]) p.dom[`assistant-${id}`].disabled = false;
  await p.dom["assistant-settings-form"].fire("submit");
  await p.dom["assistant-test"].click(); await p.dom["assistant-forget"].click();
  assert.equal(p.posts().length, 0, "DOM tampering cannot bypass the client permission check");
  await p.ask("Which cups are available?"); assert.equal(p.posts()[0].url, "/api/assistant/ask");
});

test("API settings fail closed when editability has not been confirmed", async () => {
  const p = await page({ status: { settings_editable: undefined } });
  assert.equal(p.dom["assistant-api-key"].disabled, true);
  await p.dom["assistant-settings-form"].fire("submit"); assert.equal(p.posts().length, 0);
});

test("opening Settings refreshes shared API status and clears unauthorized key drafts", async () => {
  const p = await page(); p.dom["assistant-api-key"].value = "unsaved-secret";
  p.setStatus({ settings_editable: false, configured: false });
  await p.dom["nav-settings"].click(); await tick();
  assert.equal(p.dom["assistant-api-key"].value, "");
  assert.equal(p.dom["assistant-save"].disabled, true);
  assert.equal(p.dom["assistant-key-state"].textContent, "No key saved");
});

test("a pick automatically prepares the real review and still needs exactly one confirmation", async () => {
  const p = await page({ answer: suggestion() }); await p.ask("Pick cup 2");
  assert.deepEqual(p.posts().map((item) => item.url), ["/api/assistant/ask", "/api/assistant/prepare"]);
  assert.equal(actionButton(p, "Prepare preview"), undefined);
  assert.equal(actionButton(p, "Confirm pick").disabled, false);
  assert.match(allText(p.dom["assistant-messages"]), /Cup 2 → P10/);
  assert.match(allText(p.dom["assistant-messages"]), /LIVE · confirmation will move the robot/);
  const review = nodes(p.dom["assistant-messages"], "assistant-pick-review")[0];
  assert.match(review.children[1].textContent, /Cup 2 → P10\. LIVE · moves the robot\. Preview expires/);
  assert.equal(p.dom["assistant-input"].disabled, true, "resolve this review before starting another pick");
  p.setCsrf("fresh-confirm-token");
  const confirm = actionButton(p, "Confirm pick"); await Promise.all([confirm.click(), confirm.click()]);
  const posts = p.posts().filter((item) => item.url === "/api/confirm"); assert.equal(posts.length, 1);
  assert.equal(posts[0].headers["X-CSRF-Token"], "fresh-confirm-token");
  const payload = JSON.parse(posts[0].body);
  assert.deepEqual(Object.keys(payload).sort(), ["action", "capture_id", "cup_id", "request_id", "token"]);
  assert.equal(payload.token, "abcdef12"); assert.equal(payload.cup_id, 2); assert.equal(payload.capture_id, "scan-1"); assert.equal(payload.action, "goto");
  assert.equal(confirm.disabled, true);
});

test("batch review uses controller cups and destinations and has one batch confirmation", async () => {
  const p = await page({ answer: suggestion("batch", {cup_ids: [2, 3]}), proposal: { action: "goto_all", cup_ids: [2, 3], placements: [{id: 9}, {id: 10}] }, state: {mode: "demo"} });
  await p.ask("Pick all cups");
  assert.match(allText(p.dom["assistant-messages"]), /Cup 2 → P9 · Cup 3 → P10/);
  assert.match(allText(p.dom["assistant-messages"]), /DEMO · no robot movement/);
  await actionButton(p, "Confirm 2 cups").click();
  assert.equal(JSON.parse(p.posts().find((item) => item.url === "/api/confirm").body).action, "goto_all");
});

test("dynamic batch review explicitly confirms all workspace cups with initial destinations", async () => {
  const p = await page({ answer: suggestion("batch", { cup_ids: [2, 3] }), proposal: {
    action: "goto_all", dynamic_batch: true, cup_ids: [2, 3], placements: [{ id: 9 }, { id: 10 }],
    description: "Follow the current workspace after each placement, including new pickable cups.",
  } });
  await p.ask("Pick all cups");
  const review = allText(p.dom["assistant-messages"]);
  assert.match(review, /Pick all workspace cups/);
  assert.match(review, /Initial destinations: Cup 2 → P9 · Cup 3 → P10/);
  assert.match(review, /moved and newly added cups/);
  assert.equal(actionButton(p, "Confirm 2 cups"), undefined);
  await actionButton(p, "Confirm Pick all").click();
  const confirmations = p.posts().filter((item) => item.url === "/api/confirm");
  assert.equal(confirmations.length, 1);
  assert.equal(JSON.parse(confirmations[0].body).action, "goto_all");
});

test("assistant review distinguishes grid refresh and stationary waiting from source-only checks", async () => {
  for (const gridCheck of [false, true]) {
    const p = await page({ answer: suggestion("batch", { cup_ids: [2, 3] }), proposal: {
      action: "goto_all", dynamic_batch: true, cup_ids: [2, 3], placements: [{ id: 9 }, { id: 10 }],
      between_cup_check: true, between_cup_grid_check: gridCheck,
    } });
    await p.ask("Pick all cups");
    const review = allText(p.dom["assistant-messages"]);
    if (gridCheck) {
      assert.match(review, /Camera-cleared cells can be reused/);
      assert.match(review, /wait and refresh automatically, then resume when a cell is clear/);
      assert.doesNotMatch(review, /evidence stays from the manual scan/);
    } else {
      assert.match(review, /refreshes source cups only/);
      assert.match(review, /evidence stays from the manual scan/);
      assert.doesNotMatch(review, /Camera-cleared cells can be reused|wait and refresh automatically/);
    }
  }
});

test("continuous assistant review binds empty-workspace Home and watching only to Pick all", async () => {
  const motion = { continuous_batch: true, between_cup_grid_check: true, between_cup_check: true };
  const p = await page({ answer: suggestion("batch", { cup_ids: [2, 3] }), state: { motion }, proposal: {
    action: "goto_all", dynamic_batch: true, continuous_batch: true, cup_ids: [2, 3], placements: [{ id: 9 }, { id: 10 }],
  } });
  await p.ask("Pick all cups");
  let review = allText(p.dom["assistant-messages"]);
  assert.match(review, /both arms go Home once and the camera keeps watching/);
  assert.match(review, /full grid with cups still waiting does not send the arms Home/);
  assert.match(review, /Home only when the source workspace becomes empty/);
  assert.doesNotMatch(review, /Automatic home off/);
  await actionButton(p, "Confirm Pick all").click();
  await p.publish({ busy: true, batch: { state: "homing_when_empty", home_cycles: 0 },
    progress: { completed: 14, total: 14 } });
  assert.match(allText(p.dom["assistant-messages"]), /Workspace empty · moving both arms Home once/);
  await p.publish({ busy: true, batch: { state: "waiting_for_cups", home_cycles: 1, arms_home: true },
    progress: { completed: 14, total: 14 } });
  review = allText(p.dom["assistant-messages"]);
  assert.match(review, /Watching for cups. Pick all is still active/);
  assert.doesNotMatch(review, /14 \/ 14 steps/);
  await p.publish({ busy: true, batch: { state: "waiting_for_cups", home_cycles: 1,
    arms_home: false, completion_reason: "no_pickable_cups" }, progress: { completed: 14, total: 14 } });
  review = allText(p.dom["assistant-messages"]);
  assert.match(review, /Cups are still detected, so the arms stay clear while the camera looks for a pickable cup/);
  assert.doesNotMatch(review, /14 \/ 14 steps/);
  assert.equal(p.posts().filter((item) => item.url === "/api/confirm").length, 1);

  const single = await page({ answer: suggestion(), state: { motion }, proposal: { continuous_batch: false } });
  await single.ask("Pick cup 2");
  const singleReview = allText(single.dom["assistant-messages"]);
  assert.match(singleReview, /Automatic home off/);
  assert.doesNotMatch(singleReview, /Home only when|both arms go Home once/);
  assert.ok(actionButton(single, "Confirm pick"));
});

test("camera-clear waiting position is described only when the controller enables it", async () => {
  const p = await page({ answer: suggestion(), proposal: {
    right_park_pose: { x: 400, y: -200, z: 300, rx: 0, ry: 180, rz: 0 },
  } });
  await p.ask("Pick cup 2");
  const review = allText(p.dom["assistant-messages"]);
  assert.match(review, /right arm waits at its camera-clear waiting position/);
  assert.doesNotMatch(review, /taught pose|taught parking pose/);
  assert.doesNotMatch(review, /arms remain at clearance poses/);
});

test("saved-cup matching automatically prepares a text-safe reference review", async () => {
  const injected = '<img src=x onerror="alert(1)">';
  const p = await page({ answer: suggestion("pick_saved", { message: injected }), proposal: { saved_cup: { name: injected, match_reason: "Astra found the white handle.", thumbnail_url: "/api/saved-image/cup_reference_1" } } });
  await p.ask("Pick Kasun's cup");
  assert.deepEqual(p.posts().map((item) => item.url), ["/api/assistant/ask", "/api/assistant/prepare"]);
  assert.ok(allText(p.dom["assistant-messages"]).includes(`Saved reference: ${injected}`));
  assert.match(allText(p.dom["assistant-messages"]), /Astra found the white handle/);
  assert.equal(actionButton(p, "Find and prepare preview"), undefined);
  assert.equal(actionButton(p, "Confirm pick").disabled, false);
});

test("real review includes the current scan and controller movement settings", async () => {
  const p=await page({answer:suggestion("pick_saved"),state:{inventory:{capture:"scan-1",image_url:"/api/image/scan-1"},motion:{acceleration:50,flip_acceleration:20}},proposal:{speed:250,flip_speed:100,home_speed:200,handover:{receive_mm:{x:330,y:-115,z:316},offset_mm:{x:1,y:2,z:3},approach_speed:25},saved_cup:{name:"Kasun's cup"}}});
  await p.ask("Pick Kasun's cup"); const text=allText(p.dom["assistant-messages"]);
  assert.match(text,/Current scan · scan-1/); assert.match(text,/Normal: 250 mm\/s · Cup flip: 100 mm\/s · Automatic home off/);
  assert.match(text,/finish at clearance poses/); assert.doesNotMatch(text,/Return home:|return home\./);
  assert.match(text,/Acceleration: 50% · Cup flip: 20%/);
  assert.match(text,/Right receiving target: X 330, Y -115, Z 316 mm/); assert.match(text,/Offset: X 1, Y 2, Z 3 mm/);
  assert.equal(nodes(p.dom["assistant-messages"],"assistant-pick-scan")[0].open,true);
});

test("assistant review displays the refreshed photo while confirming the original selection capture", async () => {
  const p = await page({ answer: suggestion(), state: { inventory: {
    capture: "scan-1", image_capture: "between_cups-2", image_url: "/api/image/between_cups-2", cups: [{ id: 2 }],
  } } });
  await p.ask("Pick cup 2");
  const scan = nodes(p.dom["assistant-messages"], "assistant-pick-scan")[0];
  assert.equal(scan.children.find((child) => child.tag === "img").attributes.src, "/api/image/between_cups-2");
  await actionButton(p, "Confirm pick").click();
  const confirmation = p.posts().find((item) => item.url === "/api/confirm");
  assert.equal(JSON.parse(confirmation.body).capture_id, "scan-1");
});

test("assistant review prefers proposal acceleration over current controller settings", async () => {
  const p = await page({ answer: suggestion(), state: { motion: { acceleration: 100, flip_acceleration: 30 } },
    proposal: { acceleration: 60 } });
  await p.ask("Pick cup 2");
  const review = allText(p.dom["assistant-messages"]);
  assert.match(review, /Acceleration: 60% · Cup flip: 30%/);
  assert.doesNotMatch(review, /Acceleration: 100%/);
  assert.deepEqual(p.posts().map((item) => item.url), ["/api/assistant/ask", "/api/assistant/prepare"]);
});

test("explicit automatic Home remains visible in the captured assistant review", async () => {
  const p = await page({ answer: suggestion(), state: { motion: { home_after_cup: false } },
    proposal: { home_after_cup: true, home_speed: 175, description: "" } });
  await p.ask("Pick cup 2");
  const review = allText(p.dom["assistant-messages"]);
  assert.match(review, /Pick, hand over, place and return home/);
  assert.match(review, /Return home: 175 mm\/s/);
  assert.doesNotMatch(review, /Automatic home off|clearance poses/);
});

test("review uses the preview stage speeds, never fresh settings or another confirmation", async () => {
  const p = await page({ answer: suggestion(), state: { motion: { phase_speeds: { pick_lift: 999, handover: 999, flip: 999, placement: 999 } } },
    proposal: { phase_speeds: { pick_lift: 160, handover: 220, flip: 140, placement: 180 } } });
  await p.ask("Pick cup 2");
  const review = allText(p.dom["assistant-messages"]);
  assert.match(review, /Pick & lift: 160 mm\/s · Right handover: 220 mm\/s · Cup flip: 140 mm\/s · Place & clear: 180 mm\/s/);
  assert.doesNotMatch(review, /999 mm\/s/);
  assert.equal(buttons(p.dom["assistant-messages"]).filter((button) => button.textContent === "Confirm pick").length, 1);
  assert.deepEqual(p.posts().map((item) => item.url), ["/api/assistant/ask", "/api/assistant/prepare"]);
});

test("a busy prepare waits for its correlated proposal and ignores stale polling responses", async () => {
  const p = await page({ answer: suggestion(), preparedState: { busy: true, operation: "select", proposal: null, message: "Checking destinations…", revision: 5 } });
  await p.ask("Pick cup 2");
  assert.equal(actionButton(p, "Confirm pick"), undefined);
  await p.window.fire("assistant-state", { detail: { ...p.state(), busy: false, revision: 1, proposal: realProposal({assistant_request_id:"other"}) } });
  assert.equal(actionButton(p, "Confirm pick"), undefined);
  await p.publish({ busy: false, proposal: realProposal() });
  assert.equal(actionButton(p, "Confirm pick").disabled, false);
});

test("cached progress refresh does not prevent the watchdog from fetching new state", async () => {
  const p=await page({answer:suggestion(),preparedState:{busy:true,proposal:null,operation:"select"}}); await p.ask("Pick cup 2");
  const before=p.requests.filter((item)=>item.url==="/api/state").length;
  await p.advance(700); assert.equal(p.requests.filter((item)=>item.url==="/api/state").length,before);
  p.setState({busy:false,proposal:realProposal()}); await p.advance(700);
  assert.ok(p.requests.filter((item)=>item.url==="/api/state").length>before);
  assert.equal(actionButton(p,"Confirm pick").disabled,false);
});

test("a newer app poll unlocks review when the post-acceptance read returns an older revision", async () => {
  let p, accepted = false, resolveRead;
  p = await page({ answer: suggestion(), fetch: async (url) => {
    if (url === "/api/assistant/prepare") accepted = true;
    if (url === "/api/state" && accepted && !resolveRead) return new Promise((resolve) => { resolveRead = resolve; });
    return null;
  } });
  const asking = p.ask("Pick cup 2");
  for (let index = 0; index < 6 && !resolveRead; index++) await tick();
  assert.equal(typeof resolveRead, "function");
  const older = { ...p.state(), revision: 2, busy: true, proposal: null };
  await p.publish({ revision: 3, busy: false, proposal: realProposal() });
  resolveRead({ ok: true, json: async () => older }); await asking; await tick();
  assert.equal(actionButton(p, "Confirm pick").disabled, false);
  assert.match(allText(p.dom["assistant-messages"]), /Cup 2 → P10/);
});

test("an unrelated proposal cannot be mistaken for the assistant's requested preview", async () => {
  const p = await page({ answer: suggestion(), proposal: {assistant_request_id: "unrelated"} }); await p.ask("Pick cup 2");
  assert.equal(actionButton(p, "Confirm pick"), undefined);
  assert.match(allText(p.dom["assistant-messages"]), /No matching pick preview/);
  assert.equal(p.posts().filter((item) => item.url === "/api/confirm").length, 0);
});

test("a fresh page restores an existing assistant-owned real review without preparing again", async () => {
  const p = await page(); await p.publish({ proposal: realProposal(), mode: "demo" });
  assert.equal(p.posts().length, 0);
  assert.equal(actionButton(p, "Confirm pick").disabled, false);
  assert.match(allText(p.dom["assistant-messages"]), /waiting for your review/);
  await actionButton(p, "Cancel").click();
  assert.deepEqual(p.posts().map((item) => item.url), ["/api/cancel"]);
  assert.equal(p.dom["assistant-input"].disabled, false);
  assert.match(allText(p.dom["assistant-messages"]), /Preview cancelled/);
});

test("answer and sorting replies never auto-prepare even if they contain a draft ID", async () => {
  for (const action of ["answer", "sort", "unknown"]) {
    const p = await page({ answer: suggestion(action) }); await p.ask("Explain the cups");
    assert.deepEqual(p.posts().map((item) => item.url), ["/api/assistant/ask"]);
    assert.equal(buttons(p.dom["assistant-messages"]).length, 0);
  }
});

test("confirmation rechecks token, destinations, scan, device, mode, and recovery against fresh state", async () => {
  const changes = [
    {proposal: realProposal({token:"87654321"})}, {proposal: realProposal({placements:[{id:11}]})},
    {inventory:{capture:"other-scan"}}, {device_epoch:"new-owner"}, {instance_id:"new-controller"},
    {mode:"demo"}, {devices:{active_id:"pi"}}, {recovery_required:true}, {stop_requested:true},
    {scan_usable:false}, {proposal:realProposal({expires_in:0})}, {busy:true},
  ];
  for (const change of changes) {
    const p = await page({answer:suggestion()}); await p.ask("Pick cup 2");
    p.setState(change); const confirm=actionButton(p,"Confirm pick"); await confirm.click(); await confirm.click();
    assert.equal(p.posts().filter((item)=>item.url==="/api/confirm").length,0,JSON.stringify(change));
    assert.equal(confirm.disabled,true);
  }
});

test("expiry and a disconnected controller disable an existing confirmation", async () => {
  const expired=await page({answer:suggestion()}); await expired.ask("Pick cup 2"); await expired.advance(61000);
  assert.equal(actionButton(expired,"Confirm pick").disabled,true);
  assert.match(allText(expired.dom["assistant-messages"]),/expired/);
  const disconnected=await page({answer:suggestion()}); await disconnected.ask("Pick cup 2");
  await disconnected.window.fire("assistant-disconnected");
  await actionButton(disconnected,"Confirm pick").click();
  assert.equal(disconnected.posts().filter((item)=>item.url==="/api/confirm").length,0);
});

test("Stop during a model response prevents automatic preparation", async () => {
  let complete; const pending=new Promise(resolve=>{complete=resolve;});
  const p=await page({fetch:async(url)=>url==="/api/assistant/ask"?pending:null});
  const asking=p.ask("Pick cup 2"); await tick(); await p.window.fire("assistant-local-stop");
  complete({ok:true,json:async()=>suggestion()}); await asking;
  assert.deepEqual(p.posts().map((item)=>item.url),["/api/assistant/ask"]);
  assert.match(allText(p.dom["assistant-messages"]),/No new pick was prepared/);
});

test("normal live motion recovery journaling remains progress until the controller result", async () => {
  const p=await page({answer:suggestion()}); await p.ask("Pick cup 2"); await actionButton(p,"Confirm pick").click();
  await p.publish({busy:true,operation:"confirm",recovery_required:true,progress:{completed:3,total:12},message:"Moving to handover…"});
  assert.match(allText(p.dom["assistant-messages"]),/Moving to handover… · 3 \/ 12 steps/);
  assert.doesNotMatch(allText(p.dom["assistant-messages"]),/Recovery inspection is required/);
  await p.publish({busy:false,recovery_required:false,error:null,message:"Cup placed successfully."});
  assert.match(allText(p.dom["assistant-messages"]),/Cup placed successfully/);
  assert.equal(p.dom["assistant-input"].disabled,false);
});

test("assistant shows full-grid waiting and resumes progress without another confirmation", async () => {
  const p = await page({ answer: suggestion("batch", { cup_ids: [2, 3] }), proposal: {
    action: "goto_all", dynamic_batch: true, cup_ids: [2, 3], placements: [{ id: 9 }, { id: 10 }],
    between_cup_check: true, between_cup_grid_check: true,
  } });
  await p.ask("Pick all cups");
  await actionButton(p, "Confirm Pick all").click();
  await p.publish({ busy: true, batch: { state: "waiting_for_grid", waiting_cup_ids: [3], pending_cups: [] },
    progress: { completed: 7, total: 7 }, message: "Placement completed." });
  let text = allText(p.dom["assistant-messages"]);
  assert.match(text, /Waiting for a clear grid cell · 1 cup queued/);
  assert.match(text, /batch resumes when a cell is clear/);
  assert.doesNotMatch(text, /7 \/ 7 steps/);
  await p.publish({ busy: true, batch: { state: "running", waiting_cup_ids: [], pending_cups: [3] },
    progress: { completed: 8, total: 14 }, message: "Picking cup 3." });
  text = allText(p.dom["assistant-messages"]);
  assert.match(text, /Picking cup 3. · 8 \/ 14 steps/);
  assert.equal(p.posts().filter((item) => item.url === "/api/confirm").length, 1);
});

test("an uncertain confirmation never sends a second confirmation", async () => {
  const p=await page({answer:suggestion(),fetch:async(url)=>{if(url==="/api/confirm")throw new Error("Connection lost");return null;}});
  await p.ask("Pick cup 2"); const confirm=actionButton(p,"Confirm pick"); await confirm.click(); await confirm.click();
  assert.equal(p.posts().filter((item)=>item.url==="/api/confirm").length,1);
  assert.match(allText(p.dom["assistant-messages"]),/Confirmation was not retried/);
});

test("Cancel cannot cancel a replacement proposal and works over ordinary HTTP", async () => {
  const replaced=await page({answer:suggestion()}); await replaced.ask("Pick cup 2");
  replaced.setState({proposal:realProposal({token:"87654321",assistant_request_id:"other"})}); await actionButton(replaced,"Cancel").click();
  assert.equal(replaced.posts().filter((item)=>item.url==="/api/cancel").length,0);
  const phone=await page({answer:suggestion(),insecure:true}); await phone.ask("Pick cup 2"); await actionButton(phone,"Cancel").click();
  const cancel=phone.posts().find((item)=>item.url==="/api/cancel"); assert.match(JSON.parse(cancel.body).request_id,/^[0-9a-f]{32}$/);
  assert.equal(JSON.parse(cancel.body).token,"abcdef12","cancellation is bound to this exact preview");
  assert.equal(phone.dom["assistant-input"].disabled,false);
});

test("save references stay explicit and new messages invalidate older unsubmitted saves", async () => {
  const p=await page({answer:suggestion("save_cup",{cup_name:"Kasun's cup"})}); await p.ask("Remember cup 2");
  const older=actionButton(p,"Save cup reference"); await p.ask("Remember it as my cup instead");
  assert.equal(older.textContent,"Ask again"); assert.equal(older.disabled,true);
  await older.click(); assert.equal(p.posts().filter((item)=>item.url==="/api/assistant/prepare").length,0);
});

test("the complete inline review is scrolled into view after its buttons are rendered", async () => {
  const p=await page({answer:suggestion()}); const messages=p.dom["assistant-messages"];
  Object.defineProperty(messages,"scrollHeight",{get:()=>actionButton(p,"Confirm pick")?900:200});
  await p.ask("Pick cup 2"); assert.equal(messages.scrollTop,900);
});
