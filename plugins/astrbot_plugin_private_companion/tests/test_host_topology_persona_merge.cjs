"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "pages", "\u5bbf\u4e3b\u7ba1\u7406", "app.js"), "utf8");
const slice = source.slice(source.indexOf("  function mergeSharedRolePersonas("),
  source.indexOf("  function makeTopologySnapshot("));
const context = vm.createContext({});
vm.runInContext(slice + "\nthis.mergeSharedRolePersonas = mergeSharedRolePersonas;", context);

function persona(key, roleKey, personaId, accountKey, sessionKey) {
  return {key, roleId: roleKey.replace("role:", ""), roleKey, personaId,
    accountKeys: new Set([accountKey]), sessionKeys: new Set([sessionKey])};
}

function snapshot(personas, roleKey = "role:shared") {
  return {
    personas,
    roles: [{key: roleKey, personaKeys: new Set(personas.map(item => item.key))}],
    accounts: personas.map(item => ({key: [...item.accountKeys][0], personaKeys: new Set([item.key])})),
    sessions: personas.map(item => ({key: [...item.sessionKeys][0], personaKeys: [item.key]})),
  };
}

test("same native persona under one shared role renders once and unions connections", () => {
  const result = context.mergeSharedRolePersonas(snapshot([
    persona("persona:bot-a:private", "role:shared", "shared-private", "account:a", "session:a"),
    persona("persona:bot-b:private", "role:shared", "shared-private", "account:b", "session:b"),
  ]));
  assert.equal(result.personas.length, 1);
  const merged = result.personas[0];
  assert.equal(JSON.stringify([...merged.accountKeys]), JSON.stringify(["account:a", "account:b"]));
  assert.equal(JSON.stringify([...merged.sessionKeys]), JSON.stringify(["session:a", "session:b"]));
  assert.equal(JSON.stringify([...result.roles[0].personaKeys]), JSON.stringify(["persona:bot-a:private"]));
  assert.equal(JSON.stringify([...result.accounts[1].personaKeys]), JSON.stringify(["persona:bot-a:private"]));
  assert.equal(JSON.stringify(result.sessions[1].personaKeys), JSON.stringify(["persona:bot-a:private"]));
});

test("same persona id in different roles or with different ids stays separate", () => {
  const differentRoles = context.mergeSharedRolePersonas(snapshot([
    persona("persona:a", "role:first", "shared-private", "account:a", "session:a"),
    persona("persona:b", "role:second", "shared-private", "account:b", "session:b"),
  ], "role:first"));
  assert.equal(differentRoles.personas.length, 2);

  const differentIds = context.mergeSharedRolePersonas(snapshot([
    persona("persona:a", "role:shared", "private-a", "account:a", "session:a"),
    persona("persona:b", "role:shared", "private-b", "account:b", "session:b"),
  ]));
  assert.equal(differentIds.personas.length, 2);
});
