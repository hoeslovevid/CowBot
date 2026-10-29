const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const source = fs.readFileSync('static/tts-player.js', 'utf8');
const settle = () => new Promise((resolve) => setImmediate(resolve));

function player({ key = 'sample-key', failAcknowledgment = false } = {}) {
  const backend = { current: null, paused: false, claims: 0, finishes: 0 };
  let ticker;
  const handlers = {};
  const audio = {
    paused: true, src: '', volume: 1,
    async play() { this.paused = false; },
    pause() { this.paused = true; },
    load() {},
    removeAttribute() { this.src = ''; },
    addEventListener(name, listener) { handlers[name] = listener; },
  };
  const notice = { text: '', replaceChildren(value) { this.text = value; }, append() {} };
  const context = vm.createContext({
    URLSearchParams, AbortController, crypto: webcrypto, Date,
    location: { search: key ? `?key=${key}` : '' },
    setTimeout, clearTimeout, setInterval(callback) { ticker = callback; },
    document: {
      getElementById(id) { return id === 'speech' ? audio : notice; },
      createTextNode(text) { return text; },
      createElement() { return { addEventListener() {} }; },
    },
    async fetch(url, options) {
      const operation = url.split('/').pop().split('?')[0];
      let data = { ok: true };
      if (operation === 'state') data = { ...data, current_id: backend.current, paused: backend.paused, volume: 35 };
      if (operation === 'claim') {
        backend.claims++;
        if (backend.claims === 1) {
          backend.current = 'a'.repeat(32);
          data.job = { id: backend.current, volume: 35, text: 'Sample speech', duration: 1 };
        } else data.job = null;
      }
      if (operation === 'finish') {
        backend.finishes++;
        if (failAcknowledgment && backend.finishes === 1) throw new Error('Network interruption');
        backend.current = null;
      }
      return { ok: true, async json() { return data; } };
    },
  });
  vm.runInContext(source, context);
  return { audio, notice, backend, handlers, tick: () => ticker() };
}

test('claims speech, plays audio, and acknowledges completion', async () => {
  const app = player();
  await settle();
  assert.match(app.audio.src, /\/tts\/player\/audio/);
  assert.equal(app.audio.paused, false);
  app.handlers.ended();
  await settle();
  assert.equal(app.backend.finishes, 1);
  assert.equal(app.audio.src, '');
});

test('pause, resume, and skip control current audio', async () => {
  const app = player();
  await settle();
  app.backend.paused = true;
  await app.tick();
  assert.equal(app.audio.paused, true);
  app.backend.paused = false;
  await app.tick();
  assert.equal(app.audio.paused, false);
  app.backend.current = null;
  await app.tick();
  assert.equal(app.audio.paused, true);
  assert.equal(app.audio.src, '');
});

test('retries an interrupted completion acknowledgment', async () => {
  const app = player({ failAcknowledgment: true });
  await settle();
  app.handlers.ended();
  await settle();
  assert.equal(app.backend.finishes, 1);
  await app.tick();
  assert.equal(app.backend.finishes, 2);
  assert.equal(app.backend.current, null);
});

test('missing player key displays setup instructions', async () => {
  const app = player({ key: '' });
  await settle();
  assert.match(app.notice.text, /Copy the complete OBS URL/);
  assert.equal(app.backend.claims, 0);
});
