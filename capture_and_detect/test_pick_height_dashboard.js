/* Exercise the actual height buttons and request guards without a robot/server. */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const js = fs.readFileSync(path.join(__dirname, 'web_static/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, 'web_static/index.html'), 'utf8');
function section(a, b) {
  const start = js.indexOf(a), end = js.indexOf(b, start);
  assert.ok(start >= 0 && end > start);
  return js.slice(start, end);
}
function dashboard() {
  const requests = [], dom = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(m => {
    const handlers = {};
    return [m[1], { disabled: false, hidden: false, textContent: '', attributes: {},
      setAttribute(k, v) { this.attributes[k] = v; },
      addEventListener(k, f) { handlers[k] = f; }, click() { handlers.click?.(); } }];
  }));
  const state = { ready: true, busy: false, recovery_required: false, stop_requested: false,
    csrf_token: 'csrf', instance_id: 'dashboard', device_epoch: 'owner-1',
    devices: { active_id: 'mac', switching: false },
    motion: { camera_z: false, pick_height_supported: true } };
  const context = vm.createContext({ state, dom, connected: true, posting: false, stopPosting: false,
    localStopInhibit: false, quickGeneration: 0, releaseReview: null, transientError: '',
    text: (id, value) => { dom[id].textContent = value; }, hide: (id, value) => { dom[id].hidden = value; },
    asMessage: String, readState: async () => {}, AbortController, setTimeout: () => 1, clearTimeout() {},
    crypto: { randomUUID: () => 'unique-request' },
    fetch: async (url, options) => { requests.push({ url, body: JSON.parse(options.body), headers: JSON.parse(JSON.stringify(options.headers)) });
      return context.reply || { ok: true, json: async () => ({ accepted: true }) }; },
  });
  vm.runInContext(section('  const idle =', '  const isStop =')
    + section('  function robotRequestSupported(', '  const deviceSpeedKeys =')
    + section('  async function post(', '  async function showQuick(')
    + section('  for (const [id, camera] of [["pick-height-fixed", false], ["pick-height-camera", true]]) {\n    dom[id].addEventListener', '  dom["robot-speed-form"].addEventListener')
    + '\nglobalThis.render = renderPickHeight; globalThis.send = post;', context);
  context.render();
  return { context, state, dom, requests, render: () => context.render(),
    async click(id) { dom[id].click(); await new Promise(resolve => setImmediate(resolve)); } };
}
test('fixed default reflects server state; camera click includes strict bool and device identity', async () => {
  const page = dashboard();
  assert.equal(page.dom['pick-height-fixed'].attributes['aria-pressed'], 'true');
  assert.equal(page.dom['pick-height-fixed'].disabled, true);
  assert.equal(page.dom['pick-height-camera'].disabled, false);
  await page.click('pick-height-camera');
  assert.deepEqual(page.requests, [{ url: '/api/pick-height', body: { camera_z: true, _device_id: 'mac',
    _device_epoch: 'owner-1', request_id: 'unique-request' }, headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': 'csrf' } }]);
  assert.equal(page.dom['pick-height-fixed'].attributes['aria-pressed'], 'true', 'accepted is not an applied state');
  page.state.motion.camera_z = true; page.render();
  assert.equal(page.dom['pick-height-camera'].attributes['aria-pressed'], 'true');
  assert.match(page.dom['pick-height-status'].textContent, /20 mm.*40 mm/);
  await page.click('pick-height-fixed');
  assert.equal(page.requests[1].body.camera_z, false);
});
test('busy, Stop, recovery, switching, offline and unsupported states disable both choices', async () => {
  for (const alter of [p => p.state.busy = true, p => p.state.stop_requested = true,
    p => p.state.stop_in_flight = true, p => p.state.recovery_required = true,
    p => p.state.devices.switching = true, p => p.context.connected = false,
    p => p.state.motion.pick_height_supported = false, p => p.state.motion.camera_z = null]) {
    const page = dashboard(); alter(page); page.render();
    assert.equal(page.dom['pick-height-camera'].disabled, true);
    assert.equal(page.dom['pick-height-fixed'].disabled, true);
    await page.click('pick-height-camera'); assert.equal(page.requests.length, 0);
  }
});
test('late invalid or blocked requests cannot bypass the button; no retry after rejection', async () => {
  const page = dashboard();
  for (const value of [1, 'true', null, undefined]) await page.context.send('/api/pick-height', { camera_z: value });
  page.state.busy = true;
  await page.context.send('/api/pick-height', { camera_z: true });
  assert.equal(page.requests.length, 0);
  page.state.busy = false;
  page.context.reply = { ok: false, json: async () => ({ detail: 'Batch is active on Pi' }) };
  await page.click('pick-height-camera');
  assert.equal(page.requests.length, 1);
  assert.equal(page.dom['pick-height-fixed'].attributes['aria-pressed'], 'true');
  assert.match(page.context.transientError, /Batch is active/);
});
