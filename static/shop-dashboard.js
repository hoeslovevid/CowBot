(() => {
  const panel = document.getElementById('panel-shop');
  if (!panel) return;
  const form = document.getElementById('shop-item-form');
  const field = name => form.elements.namedItem(name);
  const esc = escapeHtml;
  let state = null, offset = 0, userFilter = '', loading = false, acting = false, refreshVersion = 0;
  const labels = {challenge:'Streamer challenge', title:'Profile title', shoutout:'Shout-out', gift:'Community gift', cosmetic:'Temporary cosmetic'};
  const notice = text => { document.getElementById('shop-notice').textContent = text; };

  function categoryFields() {
    const category = field('category').value;
    panel.querySelector('[data-shop-field="value"]').hidden = !['title','cosmetic'].includes(category);
    panel.querySelector('[data-shop-field="gift"]').hidden = category !== 'gift';
    panel.querySelector('[data-shop-field="duration"]').hidden = category !== 'cosmetic';
    panel.querySelector('[data-shop-field="challenge-message"]').hidden = category !== 'challenge';
    document.getElementById('shop-value-label').textContent = category === 'cosmetic' ? 'Celebration color' : 'Title to unlock';
    field('value').required = ['title','cosmetic'].includes(category);
    const hints = {
      challenge:field('requires_message').checked ? 'The buyer must supply a challenge (up to 140 characters). You approve, perform, and complete it.' : 'A preset challenge: viewers buy using only its chat code. Use the description for instructions (or the item name if left blank). You approve, perform, and complete it.',
      shoutout:'The buyer supplies a message (up to 140 characters). Review the text before approving it for the on-screen overlay.',
      title:'Permanent unlock, automatically equipped on purchase. Viewers can switch between owned titles with the title command.',
      gift:'Awards this many points to each other viewer seen in the last 15 minutes. The buyer and known bots are excluded. No eligible viewers means no charge.',
      cosmetic:'Choose mint, gold, violet, or rose. Applies to the buyer’s shop and boost celebrations. Buying the same active color extends its duration.',
    };
    document.getElementById('shop-category-hint').textContent = hints[category];
  }

  function resetEditor() {
    form.reset(); field('id').value = '0'; field('version').value = '0';
    document.getElementById('shop-editor-heading').textContent = 'Add shop item';
    categoryFields();
  }

  function editItem(id) {
    const item = state?.items.find(item => item.id === id);
    if (!item) return;
    for (const [name,value] of Object.entries(item)) {
      const input = field(name);
      if (!input) continue;
      if (input.type === 'checkbox') input.checked = Boolean(value);
      else input.value = value;
    }
    document.getElementById('shop-editor-heading').textContent = 'Edit shop item';
    categoryFields();
    document.getElementById('shop-editor-card').scrollIntoView({behavior:'smooth', block:'start'});
    field('name').focus();
  }

  function orderHtml(order, controls = false) {
    const item = order.item;
    let result = order.result;
    if (item.category === 'gift' && order.status === 'completed') {
      try { const receipt = JSON.parse(result); result = `${receipt.points_each} points each → ${receipt.recipients.join(', ')}`; } catch (_) {}
    }
    const buttons = order.status === 'pending'
      ? `<button class="primary" data-review="approve" data-order="${order.id}">Approve</button><button class="ghost" data-review="reject" data-order="${order.id}">Reject & refund</button>`
      : order.status === 'approved' ? `<button class="primary" data-review="complete" data-order="${order.id}">Mark challenge completed</button><button class="ghost" data-review="reject" data-order="${order.id}">Cancel & refund</button>` : '';
    return `<article class="shop-row"><div class="shop-row-head"><h3>#${order.id} · ${esc(item.name)} · ${esc(order.user)}</h3><span class="shop-pill ${esc(order.status)}">${esc(order.status)}</span></div>
      <p class="shop-meta">${esc(labels[item.category])} · ${Number(item.price).toLocaleString()} points · ${esc(new Date(order.created_at).toLocaleString())}</p>
      ${order.message ? `<blockquote>${esc(order.message)}</blockquote>` : ''}<p>${esc(result)}</p>${controls ? `<div class="actions">${buttons}</div>` : ''}</article>`;
  }

  function renderProfiles() {
    if (!state) return;
    const search = document.getElementById('shop-profile-filter').value.trim().replace(/^@/,'').toLowerCase();
    const profiles = state.profiles.filter(profile => profile.user.includes(search));
    document.getElementById('shop-profiles').innerHTML = profiles.map(profile => `<article class="shop-row"><h3>${esc(profile.user)} ${profile.title ? `· ${esc(profile.title)}` : ''}</h3><p>Owned titles: ${esc(profile.titles.join(', ') || 'none')}</p><p class="shop-meta">${profile.cosmetic_until ? `${esc(profile.color)} celebrations until ${esc(new Date(profile.cosmetic_until).toLocaleString())}` : 'No active cosmetic'}</p></article>`).join('') || '<p class="muted">No matching viewer rewards yet.</p>';
  }

  function render() {
    const stats = [[state.items.filter(i => i.enabled && i.stock !== 0).length,'Available items'],[state.pending.filter(o => o.status === 'pending').length,'Awaiting approval'],[state.pending.filter(o => o.status === 'approved').length,'Challenges to perform'],[state.profiles.length,'Viewer collections']];
    document.getElementById('shop-stats').innerHTML = stats.map(([n,label]) => `<div class="shop-stat"><strong>${n}</strong><span>${label}</span></div>`).join('');
    document.getElementById('shop-items').innerHTML = state.items.map(item => `<article class="shop-row"><div class="shop-row-head"><h3>${esc(item.name)}</h3><span class="shop-pill">${item.enabled ? item.stock === 0 ? 'Sold out' : `${Number(item.price).toLocaleString()} pts` : 'Disabled'}</span></div><p>${esc(item.description)}</p><p class="shop-meta">${esc(labels[item.category])} · <code>${esc(prefix())}buy ${esc(item.slug)}${item.category === 'shoutout' || (item.category === 'challenge' && item.requires_message) ? ' &lt;message&gt;' : ''}</code><br>Stock: ${item.stock < 0 ? 'unlimited' : item.stock} · ${item.cooldown}s cooldown${item.category === 'gift' ? ` · ${item.gift_points} points per recipient` : ''}${item.category === 'cosmetic' ? ` · ${esc(item.value)}, ${item.duration} minutes` : ''}${item.category === 'title' ? ` · ${esc(item.value)}` : ''}${item.category === 'challenge' ? item.requires_message ? ' · Viewer-supplied challenge' : ' · Preset challenge' : ''}</p><button class="ghost" data-edit-item="${item.id}">Edit item</button></article>`).join('') || '<p class="muted">Create your first shop item.</p>';
    document.getElementById('shop-pending').innerHTML = state.pending.map(order => orderHtml(order, true)).join('') || '<p class="muted">No requests waiting. Your herd is all caught up.</p>';
    document.getElementById('shop-history').innerHTML = state.orders.map(order => orderHtml(order)).join('') || '<p class="muted">No purchases found.</p>';
    document.getElementById('shop-page').textContent = state.total ? `${offset + 1}–${Math.min(offset+50,state.total)} of ${state.total}` : 'No purchases';
    document.getElementById('shop-prev').disabled = offset === 0;
    document.getElementById('shop-next').disabled = offset + 50 >= state.total;
    panel.querySelectorAll('[data-shop-command]').forEach(el => { el.textContent = prefix()+el.dataset.shopCommand; });
    renderProfiles();
  }

  async function refresh() {
    if (loading || acting) return;
    loading = true;
    const version = ++refreshVersion;
    try {
      const response = await fetch(`/shop/state?${new URLSearchParams({user:userFilter,offset:String(offset)})}`, {headers:{Accept:'application/json'},cache:'no-store',signal:AbortSignal.timeout(10000)});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || 'Could not load the shop.');
      if (version !== refreshVersion || acting) return;
      state = data; render(); notice('');
    } catch(error) { if (version === refreshVersion) notice(error.message || 'Could not load the shop.'); }
    finally { if (version === refreshVersion) loading = false; }
  }

  async function act(data) {
    if (acting) return false;
    acting = true;
    refreshVersion += 1;
    loading = false;
    panel.querySelectorAll('button').forEach(button => { button.disabled = true; });
    let success = false;
    try { const result = await postAction('/shop', data); showToast(result.message); success = true; }
    catch(error) { showToast(error.message, 'error'); }
    finally { acting = false; panel.querySelectorAll('button').forEach(button => { button.disabled = false; }); }
    await refresh();
    return success;
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    data.enabled = field('enabled').checked ? '1' : '0'; data.action = 'save';
    data.requires_message = field('requires_message').checked ? '1' : '0';
    if (await act(data)) resetEditor();
  });
  field('category').addEventListener('change', categoryFields);
  field('requires_message').addEventListener('change', categoryFields);
  document.getElementById('shop-new').addEventListener('click', () => { resetEditor(); field('name').focus(); document.getElementById('shop-editor-card').scrollIntoView({behavior:'smooth'}); });
  document.getElementById('shop-cancel-edit').addEventListener('click', resetEditor);
  document.getElementById('shop-refresh').addEventListener('click', refresh);
  document.getElementById('shop-profile-filter').addEventListener('input', renderProfiles);
  document.getElementById('shop-history-filter').addEventListener('submit', event => { event.preventDefault(); userFilter = event.currentTarget.elements.namedItem('user').value.trim(); offset = 0; refresh(); });
  document.getElementById('shop-prev').addEventListener('click', () => { offset = Math.max(0,offset-50); refresh(); });
  document.getElementById('shop-next').addEventListener('click', () => { offset += 50; refresh(); });
  panel.addEventListener('click', event => {
    const edit = event.target.closest('[data-edit-item]');
    if (edit && !acting) editItem(Number(edit.dataset.editItem));
    const review = event.target.closest('[data-review]');
    if (review) {
      const action = review.dataset.review, id = Number(review.dataset.order);
      const order = state?.pending.find(order => order.id === id);
      if (!order) return;
      const prompt = action === 'reject' ? `Reject purchase #${id} and refund ${order.item.price} points to ${order.user}?` : action === 'approve' && order.item.category === 'shoutout' ? `Show this message on stream?\n\n${order.message}` : null;
      if (prompt && !window.confirm(prompt)) return;
      act({action,id});
    }
  });
  document.addEventListener('cowbot:tab', event => { if (event.detail === 'shop') refresh(); });
  categoryFields();
  if (!panel.hidden) refresh();
  setInterval(() => { if (!panel.hidden) refresh(); }, 8000);
})();
