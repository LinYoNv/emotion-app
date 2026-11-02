"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../pages/宿主管理/app.js"), "utf8");
const draw = source.slice(source.indexOf("  function drawTopologyLinks()"),
  source.indexOf("  function scheduleTopologyLinks()"));
const rect = (left, top, width, height) =>
  ({left, top, width, height, right: left + width, bottom: top + height});

function harness(positions) {
  const viewport = rect(700, 100, 200, 300);
  const nodes = [
    {key: "account", bounds: rect(0, 200, 200, 100)},
    {key: "private", bounds: rect(350, 180, 200, 100)},
    {key: "group", bounds: rect(350, 300, 200, 100)},
    ...positions.map((bounds, index) => ({key: `session-${index}`, bounds, session: true})),
  ].map(node => ({...node, dataset: {topologyKey: node.key},
    getBoundingClientRect() { return this.bounds; },
    closest() { return this.session ? {getBoundingClientRect: () => viewport} : null; }}));
  const paths = [];
  const svg = {setAttribute() {}, querySelectorAll: () => [...paths], append: item => paths.push(item)};
  const canvas = {hidden: false, getBoundingClientRect: () => rect(0, 0, 900, 520),
    querySelectorAll: () => nodes};
  const context = {topologySnapshot: {
    personas: ["private", "group"].map(key => ({key, accountKeys: new Set(["account"])})),
    sessions: positions.map((_, index) => ({key: `session-${index}`,
      personaKeys: [index % 2 ? "group" : "private"]})),
  }, $: id => id === "topology-canvas" ? canvas : svg,
  paintTopologySelection() {}, document: {createElementNS() {
    return {dataset: {}, attrs: {}, classList: {add() {}}, style: {setProperty() {}},
      setAttribute(key, value) { this.attrs[key] = value; },
      remove() { paths.splice(paths.indexOf(this), 1); }};
  }}};
  vm.createContext(context);
  vm.runInContext(draw + "\nthis.redraw = drawTopologyLinks;", context);
  return {nodes, paths, redraw: context.redraw};
}

test("offscreen session cards do not leave connectors below the scroll list", () => {
  const page = harness([rect(705, 150, 190, 100), rect(705, 420, 190, 100)]);
  page.redraw();
  assert.deepEqual(page.paths.map(line => line.dataset.topologyTarget),
    ["private", "group", "session-0"]);
});

test("a partially visible card connects inside its visible portion", () => {
  const page = harness([rect(705, 380, 190, 100)]);
  page.redraw();
  const line = page.paths.find(item => item.dataset.topologyTarget === "session-0");
  assert.match(line.attrs.d, /, 705 390$/);
});

test("redrawing after scrolling removes clipped cards and keeps persona ownership", () => {
  const page = harness([rect(705, 150, 190, 100), rect(705, 420, 190, 100)]);
  page.redraw();
  page.nodes.find(node => node.key === "session-0").bounds = rect(705, -50, 190, 100);
  page.nodes.find(node => node.key === "session-1").bounds = rect(705, 220, 190, 100);
  page.redraw();
  const lines = page.paths.filter(line => line.dataset.topologyTarget.startsWith("session-"));
  assert.equal(lines.length, 1);
  assert.equal(lines[0].dataset.topologySource, "group");
  assert.equal(lines[0].dataset.topologyTarget, "session-1");
});

test("geometry updates retain existing paths so scrolling does not restart their animation", () => {
  const page = harness([rect(705, 150, 190, 100)]);
  page.redraw();
  const original = [...page.paths];
  const before = original.at(-1).attrs.d;
  page.nodes.find(node => node.key === "session-0").bounds = rect(705, 170, 190, 100);
  page.redraw();
  assert.deepEqual(page.paths, original);
  assert.notEqual(page.paths.at(-1).attrs.d, before);
});
