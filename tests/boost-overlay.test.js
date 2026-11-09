const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/boost-overlay.js', 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

function overlay(preview = false) {
  let clock = Date.now();
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [clock])); }
    static now() { return clock; }
  }
  const element = () => ({ hidden: true, textContent: '', children: [],
    append(...items) { this.children.push(...items); }, replaceChildren() { this.children = []; } });
  const elements = Object.fromEntries(['celebration', 'active-boosts', 'boost-action', 'boost-buyer', 'boost-effect', 'boost-detail'].map(id => [id, element()]));
  const backend = { active: [], events: [], fail: false, requests: 0 };
  const intervals = [];
  const timeouts = [];
  const context = vm.createContext({
    Date: Clock, URLSearchParams, AbortSignal,
    location: { search: preview ? '?preview=1' : '' },
    document: { getElementById: id => elements[id], createElement: element },
    setInterval(fn, ms) { intervals.push({ fn, ms }); },
    setTimeout(fn, ms) { timeouts.push({ fn, ms }); },
    async fetch() {
      backend.requests++;
      if (backend.fail) throw new Error('Offline');
      return { ok: true, async json() { return { ok: true, server_time: new Clock().toISOString(), ...backend }; } };
    },
  });
  vm.runInContext(source, context);
  return { backend, elements, timeouts,
    advance(ms) { clock += ms; },
    async poll() { await intervals.find(i => i.ms === 2000).fn(); },
    tick() { intervals.find(i => i.ms === 1000).fn(); },
    event(id, user = 'Viewer') { return { id, user, label: 'Double Watch', summary: 'Double points', cost: 50, minutes: 15,
      created_at: new Clock().toISOString(), until: new Clock(clock + 900000).toISOString() }; },
  };
}

test('queues separate purchases without replaying duplicate events', async () => {
  const app = overlay();
  await settle();
  app.backend.events = [app.event('one', 'First'), app.event('two', 'Second')];
  await app.poll();
  assert.equal(app.elements['boost-buyer'].textContent, 'First');
  await app.poll();
  assert.equal(app.timeouts.length, 1);
  app.timeouts.shift().fn();
  assert.equal(app.elements.celebration.hidden, true);
  app.timeouts.shift().fn();
  assert.equal(app.elements['boost-buyer'].textContent, 'Second');
  app.timeouts.shift().fn();
  app.timeouts.shift().fn();
  assert.equal(app.elements.celebration.hidden, true);
});

test('countdowns expire even when the server is unavailable', async () => {
  const app = overlay();
  await settle();
  app.backend.active = [app.event('one')];
  await app.poll();
  assert.equal(app.elements['active-boosts'].children[0].children[2].textContent, '15:00');
  app.backend.fail = true;
  await app.poll();
  app.advance(901000);
  app.tick();
  assert.equal(app.elements['active-boosts'].children.length, 0);
});

test('stale events do not celebrate', async () => {
  const app = overlay();
  await settle();
  app.backend.events = [app.event('old')];
  app.advance(121000);
  await app.poll();
  assert.equal(app.elements.celebration.hidden, true);
});

test('first connection restores active timers without replaying old purchases', async () => {
  const app = overlay();
  const prior = app.event('existing');
  app.backend.events = [prior];
  app.backend.active = [prior];
  await settle();
  assert.equal(app.elements.celebration.hidden, true);
  assert.equal(app.elements['active-boosts'].children.length, 1);
  await app.poll();
  assert.equal(app.elements.celebration.hidden, true);
  app.backend.events.push({ ...app.event('extension'), extended: true });
  await app.poll();
  assert.equal(app.elements['boost-action'].textContent, 'Double Watch extended');
});

test('sample preview shows both timers and makes no requests', () => {
  const app = overlay(true);
  assert.equal(app.backend.requests, 0);
  assert.equal(app.elements.celebration.hidden, false);
  assert.equal(app.elements['active-boosts'].children.length, 2);
});
