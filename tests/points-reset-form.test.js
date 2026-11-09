const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');
const formsCode = source.slice(source.indexOf('function setupForms()'), source.indexOf('async function saveFeatures()'));

function resetForm(approved) {
  let onSubmit;
  const requests = [];
  const prompts = [];
  const button = { disabled: false };
  const form = {
    id: 'reset-all-points-form',
    dataset: { action: '/points/reset', confirm: 'Reset EVERY saved viewer balance to 0 points?' },
    addEventListener(_type, handler) { onSubmit = handler; },
    getAttribute() { return null; }, setAttribute() {}, removeAttribute() {},
    querySelectorAll() { return [button]; },
  };
  vm.runInNewContext(formsCode + '\nsetupForms();', {
    document: { querySelectorAll() { return [form]; } },
    window: { confirm(message) { prompts.push(message); return approved; } },
    FormData: class { constructor() { this.scope = 'all'; } },
    async postAction(url, body) { requests.push({ url, body }); return { ok: true }; },
    showToast() {}, async refreshStatus() {},
  });
  return { prompts, requests, button,
    submit: () => onSubmit({ preventDefault() {}, submitter: null }),
  };
}

test('cancelling reset prevents any request, including submissions without a button', async () => {
  const form = resetForm(false);
  await form.submit();
  assert.equal(form.prompts.length, 1);
  assert.equal(form.requests.length, 0);
});

test('confirmed reset sends the request and restores the button', async () => {
  const form = resetForm(true);
  await form.submit();
  assert.equal(form.prompts.length, 1);
  assert.equal(form.requests.length, 1);
  assert.equal(form.requests[0].url, '/points/reset');
  assert.equal(form.requests[0].body.scope, 'all');
  assert.equal(form.button.disabled, false);
});
