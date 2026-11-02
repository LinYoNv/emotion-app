"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.resolve(__dirname, "../pages/陪伴小屋/confirmation-dialog.js"), "utf8");
const confirmationClients = [
  "../pages/陪伴小屋/memory-page.js",
  "../pages/陪伴小屋/maintenance-page.js",
  "../pages/陪伴小屋/semantic-page.js",
  "../pages/陪伴小屋/weather-source-page.js",
  "../pages/关系与记忆/group-memory.js",
];

function control() {
  const listeners = new Map();
  return {
    textContent: "", dataset: {}, open: false, returnValue: "", children: [],
    appendChild(node) {this.children.push(node);},
    setAttribute() {}, removeAttribute() {},
    addEventListener(type, callback) {
      const group = listeners.get(type) || [];
      group.push(callback); listeners.set(type, group);
    },
    dispatch(type, event = {}) { for (const callback of listeners.get(type) || []) callback(event); },
    showModal() { this.open = true; },
    close(value) { this.open = false; this.returnValue = value; this.dispatch("close"); },
    focus() {},
    getBoundingClientRect() { return {left: 0, top: 0, right: 100, bottom: 100}; },
  };
}

function harness() {
  const elements = Object.fromEntries(["confirmationDialog", "confirmationTitle", "confirmationMessage",
    "confirmationCancel", "confirmationAccept", "confirmationSave", "confirmationDetails", "confirmationStatus"].map(id => [id, control()]));
  const document = {querySelector: selector => elements[selector.slice(1)] || null, createElement: () => control()};
  const window = {};
  vm.runInNewContext(source, {window, document});
  return {window, elements};
}

test("confirmation dialogs use text content, serialize requests, and resolve cancel safely", async () => {
  const {window, elements} = harness();
  const first = window.CompanionConfirm({title: "删除素材", message: "<img src=x>", confirmText: "删除", danger: true});
  const second = window.CompanionConfirm({title: "离开页面", message: "丢弃未保存修改？"});

  assert.equal(elements.confirmationDialog.open, true);
  assert.equal(elements.confirmationTitle.textContent, "删除素材");
  assert.equal(elements.confirmationMessage.textContent, "<img src=x>");
  assert.equal(elements.confirmationDialog.dataset.intent, "danger");
  assert.equal(elements.confirmationAccept.textContent, "删除");

  elements.confirmationAccept.dispatch("click");
  assert.equal(await first, true);
  await Promise.resolve();
  assert.equal(elements.confirmationTitle.textContent, "离开页面");
  assert.equal(elements.confirmationDialog.dataset.intent, "normal");
  elements.confirmationCancel.dispatch("click");
  assert.equal(await second, false);
  assert.equal(elements.confirmationDialog.open, false);
});

test("Escape and backdrop interactions cancel the active request", async () => {
  const {window, elements} = harness();
  const escaped = window.CompanionConfirm("确认第一步");
  let prevented = false;
  elements.confirmationDialog.dispatch("cancel", {preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
  assert.equal(await escaped, false);

  const backdrop = window.CompanionConfirm("确认第二步");
  elements.confirmationDialog.dispatch("click", {target: elements.confirmationDialog, clientX: 150, clientY: 50});
  assert.equal(await backdrop, false);
});

test("unsaved changes show field names and wait for a confirmed save before leaving", async () => {
  const {window, elements: e} = harness();
  let resolveSave, calls = 0;
  const decision = window.CompanionConfirm({message:'以下配置还没有保存。',
    details:[{title:'设备观察契机',fields:['每天主动观察次数','<img src=x>']}],
    onSave:() => {calls++;return new Promise(resolve => {resolveSave=resolve;});}});
  assert.equal(e.confirmationDetails.children[0].textContent, '设备观察契机');
  assert.equal(e.confirmationDetails.children[1].children[1].textContent, '<img src=x>');
  assert.equal(e.confirmationSave.hidden, false);
  e.confirmationSave.dispatch('click'); e.confirmationSave.dispatch('click');
  e.confirmationCancel.dispatch('click');
  assert.equal(calls, 1);
  assert.equal(e.confirmationSave.disabled, true);
  assert.equal(e.confirmationDialog.open, true);
  resolveSave({error:'每天主动观察次数：请填写有效整数。'});
  await new Promise(setImmediate);
  assert.equal(e.confirmationDialog.open, true);
  assert.match(e.confirmationStatus.textContent, /每天主动观察次数/);
  assert.equal(e.confirmationSave.disabled, false);
  e.confirmationSave.dispatch('click'); resolveSave(true);
  assert.equal(await decision, true);
  assert.equal(e.confirmationDialog.open, false);
});

test("discard callback completes before navigation is allowed", async () => {
  const {window, elements: e} = harness();
  let discarded = false;
  const decision = window.CompanionConfirm({message:'未保存的改动',onConfirm:() => {discarded=true;return true;}});
  e.confirmationAccept.dispatch('click');
  assert.equal(await decision, true);
  assert.equal(discarded, true);
  assert.equal(e.confirmationSave.hidden, true);
});

test("destructive pages use in-page confirmation instead of the browser confirm API", () => {
  for (const file of confirmationClients) {
    const text = fs.readFileSync(path.resolve(__dirname, file), "utf8");
    assert.doesNotMatch(text, /window\.confirm\s*\(/, file);
  }
});
