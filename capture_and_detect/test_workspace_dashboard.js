/* A between-cup image refresh must preserve the reviewed selection identity. */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const script = fs.readFileSync(path.join(__dirname, "web_static/app.js"), "utf8");

function dashboard() {
  const dom = {}, imageLoads = [];
  for (const id of ["selection-overlay", "image-stage", "empty-camera", "camera-image",
                    "image-time", "scan-summary", "scan-state", "capture-id", "capture-details"]) {
    dom[id] = { textContent: "", hidden: false, classList: { toggle() {} },
      replaceChildren() {}, querySelector() { return { textContent: "" }; }, removeAttribute() {},
      set src(value) { imageLoads.push(value); } };
  }
  const context = vm.createContext({ dom, URL, connected: true, selectedId: 2,
    selectedCapture: "scan-1", imageCapture: null, imageLoaded: false,
    window: { location: { origin: "http://localhost:8087" } },
    state: { scan_usable: true, inventory: { capture: "scan-1", image_url: "/api/image/scan-1",
      cups: [{ id: 2, selectable: true }], saved_at: "now" } },
    text(id, value) { dom[id].textContent = String(value ?? ""); },
    hide(id, value) { dom[id].hidden = value; },
    clockText(value) { return value; }, renderCups() {},
  });
  const start = script.indexOf("  function renderInventory()"), end = script.indexOf("  function renderSelection()", start);
  assert.ok(start >= 0 && end > start);
  vm.runInContext("const cups = () => state.inventory?.cups || [];\n" + script.slice(start, end)
    + "\nglobalThis.render = renderInventory;", context);
  return { context, dom, imageLoads };
}

test("fresh workspace photo reloads without renumbering the selected cup", () => {
  const page = dashboard();
  page.context.render();
  assert.deepEqual(page.imageLoads, ["http://localhost:8087/api/image/scan-1"]);
  page.context.state.inventory.image_capture = "between-2";
  page.context.state.inventory.image_url = "/api/image/between-2";
  page.context.render();
  assert.equal(page.imageLoads.at(-1), "http://localhost:8087/api/image/between-2");
  assert.equal(page.context.selectedCapture, "scan-1");
  assert.equal(page.context.selectedId, 2);
  assert.equal(page.dom["capture-id"].textContent, "scan-1");
  page.context.render();
  assert.equal(page.imageLoads.length, 2);
});

test("a manual new scan still resets selection", () => {
  const page = dashboard();
  page.context.render();
  page.context.state.inventory = { capture: "scan-3", image_url: "/api/image/scan-3", cups: [] };
  page.context.render();
  assert.equal(page.context.selectedId, null);
  assert.equal(page.context.selectedCapture, "scan-3");
  assert.equal(page.imageLoads.at(-1), "http://localhost:8087/api/image/scan-3");
});
