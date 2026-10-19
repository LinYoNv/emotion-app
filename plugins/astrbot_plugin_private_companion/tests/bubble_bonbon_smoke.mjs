/**
 * 泡泡元气岛 · 冒烟测试
 *
 * 在 Node 里用一套假的 DOM / Canvas 跑起整份游戏代码，
 * 然后模拟输入推进游戏帧，检查：启动、移动、放泡泡、爆炸、
 * 波次结算、三选一、乱斗判定、大招、融合反应、捕获与拾取。
 *
 * 运行： node tests/bubble_bonbon_smoke.mjs
 */

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const gameDir = path.join(root, 'games', 'bubble_bonbon');
const htmlPath = path.join(gameDir, 'index.html');

const html = fs.readFileSync(htmlPath, 'utf8');
const scripts = [...html.matchAll(/<script\s+src="([^"]+)"\s*><\/script>/g)].map((m) => m[1]);
if (scripts.length === 0) throw new Error('index.html 里没有找到游戏脚本');

/* ------------------------------------------------------------------ 假 DOM */
const missingCtx = new Set();

function makeClassList() {
  const set = new Set();
  return {
    add(...names) { names.forEach((n) => set.add(n)); },
    remove(...names) { names.forEach((n) => set.delete(n)); },
    toggle(name, force) {
      if (force === undefined) {
        if (set.has(name)) set.delete(name);
        else set.add(name);
      } else if (force) set.add(name);
      else set.delete(name);
      return set.has(name);
    },
    contains(name) { return set.has(name); },
  };
}

function makeEl(id) {
  const listeners = new Map();
  const el = {
    id,
    tagName: 'DIV',
    children: [],
    style: {},
    dataset: {},
    classList: makeClassList(),
    attributes: new Map(),
    innerHTML: '',
    textContent: '',
    clientWidth: 960,
    clientHeight: 780,
    offsetWidth: 960,
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    removeEventListener() {},
    dispatch(type, ev) { (listeners.get(type) || []).forEach((fn) => fn.call(el, ev || {})); },
    appendChild(child) { el.children.push(child); return child; },
    setAttribute(k, v) { el.attributes.set(k, String(v)); },
    getAttribute(k) { return el.attributes.has(k) ? el.attributes.get(k) : null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { left: 0, top: 0, right: 120, bottom: 120, width: 120, height: 120 }; },
    setPointerCapture() {},
    releasePointerCapture() {},
    blur() {},
    focus() {},
    click() { el.dispatch('click', {}); },
  };
  return el;
}

const gradientStub = { addColorStop() {} };
const canvasEl = Object.assign(makeEl('game'), {
  width: 960,
  height: 780,
  getContext() { return ctxProxy; },
});

const ctxBase = {
  canvas: canvasEl,
  createLinearGradient: () => gradientStub,
  createRadialGradient: () => gradientStub,
  createPattern: () => null,
  measureText: (s) => ({ width: String(s).length * 8 }),
  save() {}, restore() {}, setTransform() {}, resetTransform() {},
  translate() {}, scale() {}, rotate() {}, transform() {},
  beginPath() {}, closePath() {}, moveTo() {}, lineTo() {},
  quadraticCurveTo() {}, bezierCurveTo() {}, arc() {}, arcTo() {}, ellipse() {},
  rect() {}, roundRect() {}, fill() {}, stroke() {}, clip() {},
  fillRect() {}, strokeRect() {}, clearRect() {},
  fillText() {}, strokeText() {}, setLineDash() {},
  drawImage() {}, putImageData() {}, getImageData: () => ({ data: [] }),
  createImageData: () => ({ data: [] }),
};

const ctxProxy = new Proxy(ctxBase, {
  get(target, prop) {
    if (prop in target) return target[prop];
    if (typeof prop === 'string' && /^[a-z]/.test(prop)) {
      missingCtx.add(prop);
      return () => {};
    }
    return undefined;
  },
  set(target, prop, value) {
    target[prop] = value;
    return true;
  },
});

const elements = new Map();
const modeChips = ['coop', 'battle'].map((mode, i) => {
  const el = makeEl('mode-chip-' + i);
  el.setAttribute('data-mode', mode);
  if (i === 0) el.classList.add('active');
  return el;
});
const countChips = [1, 2, 3, 4].map((n, i) => {
  const el = makeEl('count-chip-' + i);
  el.setAttribute('data-count', String(n));
  if (n === 2) el.classList.add('active');
  return el;
});

const document = {
  readyState: 'complete',
  getElementById(id) {
    if (id === 'game') return canvasEl;
    if (!elements.has(id)) elements.set(id, makeEl(id));
    return elements.get(id);
  },
  createElement(tag) { return makeEl(tag); },
  querySelectorAll(sel) {
    if (sel === '#mode-group .chip') return modeChips;
    if (sel === '#count-group .chip') return countChips;
    return [];
  },
  addEventListener() {},
};

const storage = new Map();
const sandbox = {
  console,
  document,
  navigator: { getGamepads: () => [] },
  location: { search: '', href: 'file:///game/index.html' },
  localStorage: {
    getItem(k) { return storage.has(k) ? storage.get(k) : null; },
    setItem(k, v) { storage.set(k, String(v)); },
    removeItem(k) { storage.delete(k); },
  },
  performance: { now: () => Date.now() },
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  requestAnimationFrame: () => 0,
  cancelAnimationFrame: () => {},
  devicePixelRatio: 2,
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  __NO_RAF: true,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

const winListeners = new Map();
sandbox.addEventListener = (type, fn) => {
  if (!winListeners.has(type)) winListeners.set(type, []);
  winListeners.get(type).push(fn);
};
sandbox.removeEventListener = () => {};
sandbox.dispatchEvent = (ev) => {
  (winListeners.get(ev && ev.type) || []).forEach((fn) => fn(ev));
};

const context = vm.createContext(sandbox);

const code = scripts
  .map((rel) => {
    const file = path.join(gameDir, rel);
    return '/* ---- ' + rel + ' ---- */\n' + fs.readFileSync(file, 'utf8');
  })
  .join('\n');
vm.runInContext(code, context, { filename: 'bubble_bonbon.js' });

/* ------------------------------------------------------------------ 测试 */
const testSource = `
(function () {
  var out = { fails: [], logs: [] };
  function check(cond, msg) {
    if (!cond) out.fails.push(msg);
    else out.logs.push('ok  ' + msg);
  }
  function frames(n, each) {
    for (var i = 0; i < n; i++) {
      if (each) each(i);
      window.BubbleGame.update(1 / 60);
      if (i % 3 === 0) window.BubbleGame.render();
      window.BubbleGame.input.endFrame();
    }
  }
  try {
    var B = window.BubbleGame;
    var G = B.G;
    var Input = B.input;

    check(!!B, '暴露调试接口');
    check(G.mode === 'title', '启动后停在标题场景（实际 ' + G.mode + '）');
    check(G.players.length === 2, '标题场景有 2 个展示角色');
    B.render();

    /* ---- 合作模式 ---- */
    B.startRun('coop', 2);
    check(G.mode === 'playing', '合作模式进入 playing');
    check(G.players.length === 2, '合作模式 2 名玩家');
    check(G.wave === 1, '从第 1 波开始');

    var p1 = G.players[0];
    var x0 = p1.x;
    Input.keys.add('KeyD');
    frames(40);
    Input.keys.delete('KeyD');
    check(p1.x > x0 + 8, '按 D 向右移动（' + x0.toFixed(1) + ' -> ' + p1.x.toFixed(1) + '）');

    var y0 = p1.y;
    Input.keys.add('KeyS');
    frames(20);
    Input.keys.delete('KeyS');
    check(p1.y >= y0 - 0.01, '按 S 不会向上跑');

    Input.keys.add('KeyJ');
    frames(2);
    Input.keys.delete('KeyJ');
    check(G.bombs.length >= 1, '按 J 放下泡泡');

    var guard = 0;
    while (G.bombs.length > 0 && guard++ < 500) {
      B.update(1 / 60);
      if (guard % 4 === 0) B.render();
      Input.endFrame();
    }
    check(guard < 500, '泡泡会在引信结束后爆炸');

    var badPos = false;
    for (var i = 0; i < G.players.length; i++) {
      var p = G.players[i];
      if (!isFinite(p.x) || !isFinite(p.y)) badPos = true;
    }
    check(!badPos, '玩家坐标没有 NaN');

    /* ---- 敌人与流场：跑 900 帧 ---- */
    frames(900, function (i) {
      if (i % 90 === 0) {
        Input.keys.clear();
        var keys = ['KeyW', 'KeyA', 'KeyS', 'KeyD'];
        Input.keys.add(keys[(i / 90) % 4]);
      }
      if (i % 30 === 0) {
        Input.keys.add('KeyJ');
      }
      if (i % 30 === 4) {
        Input.keys.delete('KeyJ');
      }
    });
    Input.keys.clear();
    check(G.enemies.length + G.captured.length > 0, '敌人会刷新出来');
    check(isFinite(G.score), '分数是有效数字');

    /* ---- 波次结算（重开一局，避免压力测试里全员阵亡干扰后续断言） ---- */
    B.startRun('coop', 2);
    G.enemies.length = 0;
    G.captured.length = 0;
    G.spawnQueue.length = 0;
    var step = 0;
    while (G.mode === 'playing' && step++ < 200) {
      B.update(1 / 60);
      Input.endFrame();
    }
    check(G.mode === 'upgrade', '清场后进入三选一（实际 ' + G.mode + '）');
    B.render();
    var waveBefore = G.wave;
    B.chooseUpgrade(0);
    check(G.mode === 'playing', '选完强化继续游戏');
    check(G.wave === waveBefore + 1, '进入下一波');
    check(Object.keys(G.up).length >= 1, '强化等级被记录');

    /* ---- 元素融合反应 ---- */
    G.spawnQueue.length = 0;
    G.enemies.length = 0;
    G.captured.length = 0;
    var fusionsBefore = G.players[0].fusions || 0;
    B.triggerReaction(0, 1, { cx: 5, cy: 5 }, 0);
    check(G.reactions.length >= 1, '融合反应生成特效');
    check((G.players[0].fusions || 0) === fusionsBefore + 1, '融合次数 +1');

    /* ---- 捕获与碰撞爆开 ---- */
    G.enemies.length = 0;
    G.captured.length = 0;
    var foe = B.spawnEnemy('jelly', 9, 6);
    foe.spawnT = 0;
    B.damageEnemy(foe, 5, 0, 0);
    check(G.captured.length === 1, '敌人被打成捕获泡泡');
    var cap = G.captured[0];
    cap.born = 1;
    G.players[0].x = cap.x;
    G.players[0].y = cap.y;
    frames(6);
    check(G.captured.length === 0, '碰到捕获泡泡即爆开');
    check(G.combo >= 1, '捕获连击计数生效');

    /* ---- 元素风暴 ---- */
    B.startRun('coop', 1);
    G.spawnQueue.length = 0;
    G.enemies.length = 0;
    var hero = G.players[0];
    hero.invuln = 0;
    var victim = B.spawnEnemy('jelly', 5, 5);
    victim.spawnT = 0;
    hero.resonance = 100;
    B.tryUltimate(hero);
    check(G.ultimates.length === 1, '元素风暴生成');
    frames(120);
    check(G.enemies.length === 0, '元素风暴清掉范围内敌人');
    check(hero.resonance < 100, '元素风暴消耗共鸣');

    /* ---- 乱斗模式 ---- */
    B.startRun('battle', 3);
    check(G.players.length === 3, '乱斗 3 名玩家');
    frames(30);
    var a = G.players[0];
    var b = G.players[1];
    var c = G.players[2];
    a.invuln = 0; b.invuln = 0; c.invuln = 0;
    B.damagePlayer(b, 99, -1, 'enemy');
    B.damagePlayer(c, 99, -1, 'enemy');
    check(G.mode === 'over', '乱斗剩一人即结算（实际 ' + G.mode + '）');
    check(G.winner === a, '最后存活者获胜');
    check(document.getElementById('over-stats').innerHTML.indexOf('泡泡乱斗') >= 0, '结算面板写入模式信息');
    B.render();

    /* ---- 4 人合作渲染 ---- */
    B.startRun('coop', 4);
    check(G.players.length === 4, '合作模式支持 4 人');
    frames(40);
    B.render();
    check(G.players[3].x > 0, '4 号位玩家有坐标');

    /* ---- 电脑玩家（合作） ---- */
    B.setSlot(2, 'bot');
    B.setSlot(3, 'bot');
    check(G.slots[2] === 'bot' && G.slots[3] === 'bot', '大厅可以把席位切成电脑');
    B.startRun('coop', 4);
    check(G.players[2].isBot === true && G.players[3].isBot === true, '电脑席位生成了 AI 玩家');
    G.spawnQueue.length = 0;
    G.enemies.length = 0;
    var dummy = B.spawnEnemy('tank', 8, 3);
    dummy.spawnT = 0;
    dummy.stun = 999;
    var botA = G.players[2];
    var botB = G.players[3];
    var botStart = { x: botA.x, y: botA.y };
    frames(360);
    var botMoved = Math.hypot(botA.x - botStart.x, botA.y - botStart.y);
    check(botMoved > 20 || botA.placed > 0, '电脑玩家会自己走位（位移 ' + botMoved.toFixed(1) + '）');
    check(botA.placed + botB.placed > 0, '电脑玩家会放泡泡');
    check(dummy.hp < dummy.maxHp || dummy.dead, '电脑玩家会攻击敌人');

    /* ---- 电脑玩家（乱斗） ---- */
    B.setSlot(0, 'bot');
    B.setSlot(1, 'bot');
    B.startRun('battle', 3);
    check(G.players[0].isBot && G.players[1].isBot, '乱斗里也能全是电脑');
    var b0 = G.players[0];
    var bs = { x: b0.x, y: b0.y };
    frames(240);
    check(Math.hypot(b0.x - bs.x, b0.y - bs.y) > 12 || b0.placed > 0, '乱斗电脑会主动进攻');

    /* ---- 自定义键位 ---- */
    B.setSlot(0, 'keyboard');
    B.setSlot(1, 'keyboard');
    check(B.findKeyOwner(0, 'bomb', 'KeyW') !== null, '键位冲突能被检测出来');
    B.keymaps[0].bomb = ['KeyZ'];
    B.startRun('coop', 2);
    Input.keys.add('KeyZ');
    frames(2);
    Input.keys.delete('KeyZ');
    check(G.bombs.length >= 1, '自定义键位生效（Z 放泡泡）');
    B.keymaps[0].bomb = ['KeyJ'];

    /* ---- 回到标题 ---- */
    B.setupTitleScene();
    check(G.mode === 'title', '可以回到标题');
    frames(10);
  } catch (err) {
    out.fails.push('抛出异常: ' + (err && err.stack ? err.stack : err));
  }
  return out;
})()
`;

const result = vm.runInContext(testSource, context, { filename: 'smoke-body.js' });

const ok = result.logs.length;
const failed = result.fails.length;
for (const line of result.logs) console.log('  ✓ ' + line.slice(4));
if (failed) {
  console.log('');
  for (const line of result.fails) console.log('  ✗ ' + line);
}
if (missingCtx.size) {
  console.log('\n  ! Canvas 桩里缺少的方法（自动降级为 no-op）：' + [...missingCtx].join(', '));
}
console.log('\n通过 ' + ok + ' 项，失败 ' + failed + ' 项');
process.exit(failed ? 1 : 0);
