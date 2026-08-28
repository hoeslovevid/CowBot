const JSON_HEADERS = { Accept: "application/json" };

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function prefix() {
  return document.body.dataset.prefix || "?";
}

function showToast(message, kind = "success") {
  const host = document.getElementById("toasts");
  if (!host || !message) return;
  const toast = document.createElement("p");
  toast.className = `toast ${kind}`;
  toast.textContent = message;
  host.append(toast);
  setTimeout(() => toast.remove(), 4200);
}

async function readJson(response) {
  try {
    return await response.json();
  } catch (_error) {
    return {};
  }
}

async function postAction(url, body) {
  const isForm = body instanceof FormData;
  const response = await fetch(url, {
    method: "POST",
    headers: isForm ? JSON_HEADERS : { ...JSON_HEADERS, "Content-Type": "application/json" },
    body: isForm ? body : JSON.stringify(body),
  });
  const data = await readJson(response);
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || "Request failed");
  }
  return data;
}

function setPill(status) {
  const pill = document.querySelector("[data-live='pill']");
  if (!pill) return;
  const live = Boolean(status.bot_reachable && status.connected);
  pill.className = `pill ${live ? "live" : "down"}`;
  const label = pill.querySelector("span");
  if (label) {
    label.textContent = live ? "Live on stream" : (status.bot_reachable ? "Connecting" : "Bot offline");
  }
}

function setText(name, value) {
  document.querySelectorAll(`[data-live="${name}"]`).forEach((node) => {
    node.textContent = value;
  });
}

function giveawayMeta(status) {
  const winners = status.giveaway_winners || [];
  if (status.giveaway_drawing) return `Drawing · ${status.giveaway_entry_count || 0} entered`;
  if (status.active_giveaway) {
    const count = Number(status.giveaway_winner_count || 1);
    return count > 1
      ? `${status.giveaway_entry_count || 0} entered · ${count} winners`
      : `${status.giveaway_entry_count || 0} entered`;
  }
  if (winners.length === 1) return `Winner ${winners[0]}`;
  if (winners.length > 1) return `Winners ${winners.join(", ")}`;
  return "No winner yet";
}

function renderLeaderboard(rows) {
  const board = document.querySelector("[data-live='leaderboard']");
  if (!board) return;
  if (!rows || !rows.length) {
    board.innerHTML = `<li class="empty">No points yet. Chat can use <code>${escapeHtml(prefix())}daily</code> to start.</li>`;
    return;
  }
  board.innerHTML = rows.map((row, index) => {
    const place = index < 3 ? index + 1 : "rest";
    return `<li class="place-${place}"><span class="rank">${index + 1}</span><span class="user">${escapeHtml(row.user)}</span><span class="points">${escapeHtml(row.points)}</span></li>`;
  }).join("");
}

function renderPoll(status) {
  const box = document.querySelector("[data-live='poll_results']");
  if (!box) return;
  if (!status.active_poll) {
    box.innerHTML = `<p class="muted">No poll running. Start one below, then chat votes with <code>${escapeHtml(prefix())}poll vote &lt;option&gt;</code>.</p>`;
    return;
  }
  const results = status.poll_results || [];
  const maxVotes = Math.max(1, ...results.map((row) => Number(row.votes) || 0));
  const rows = results.map((row) => {
    const width = Math.round((Number(row.votes) / maxVotes) * 100);
    return `<li><div class="option-row"><span>${escapeHtml(row.option)}</span><b>${escapeHtml(row.votes)}</b></div><div class="bar"><i style="width: ${width}%"></i></div></li>`;
  }).join("");
  box.innerHTML = `<p class="live-title">${escapeHtml(status.poll_question || status.active_poll)}</p><ul>${rows}</ul>`;
}

function winnerListHtml(winners) {
  if (!winners.length) return "";
  const rows = winners.map((user, index) => `
    <li class="winner-row">
      <span>${index + 1}. ${escapeHtml(user)}</span>
      <button type="button" class="ghost" data-reroll-winner="${escapeHtml(user)}">Reroll</button>
    </li>
  `).join("");
  return `<p class="live-title">${winners.length === 1 ? "Winner" : "Winners"}</p><ul class="winner-list">${rows}</ul>`;
}

function renderGiveaway(status) {
  const box = document.querySelector("[data-live='giveaway_live']");
  if (!box) return;
  const winners = status.giveaway_winners || [];
  const winnerBlock = winnerListHtml(winners);
  if (!status.active_giveaway && !status.giveaway_drawing) {
    if (winnerBlock) {
      box.innerHTML = `<p class="muted">Giveaway closed. Reroll a specific winner below.</p>${winnerBlock}`;
      return;
    }
    box.innerHTML = `<p class="muted">No giveaway running. Start one below, then chat joins with <code>${escapeHtml(prefix())}giveaway</code>.</p>`;
    return;
  }
  const count = Number(status.giveaway_winner_count || 1);
  const locked = status.giveaway_drawing
    ? `Entries are locked. Spinning ${count} winner${count === 1 ? "" : "s"}.`
    : `${status.giveaway_entry_count || 0} entered${count > 1 ? ` · ${count} winners` : ""} · type <code>${escapeHtml(prefix())}giveaway</code> to join`;
  const entries = (status.giveaway_entries || []).map((user) => `<li>${escapeHtml(user)}</li>`).join("");
  const list = entries
    ? `<ul class="entry-list">${entries}</ul>`
    : `<p class="muted">Waiting for entries. Ask chat to type <code>${escapeHtml(prefix())}giveaway</code>.</p>`;
  box.innerHTML = `<p class="live-title">${escapeHtml(status.active_giveaway)}</p><p class="muted">${locked}</p>${list}${status.giveaway_drawing ? "" : winnerBlock}`;
}

let customCommands = [];
let editingCommandId = "";

function commandForm() {
  return document.getElementById("custom-command-form");
}

function resetCommandForm() {
  const form = commandForm();
  if (!form) return;
  editingCommandId = "";
  form.querySelector("[name='command_id']").value = "";
  form.querySelector("[name='command_name']").value = "";
  form.querySelector("[name='command_aliases']").value = "";
  form.querySelector("[name='command_response']").value = "";
  form.querySelector("[name='command_cooldown']").value = "0";
  const save = document.getElementById("custom-command-save");
  const cancel = document.getElementById("custom-command-cancel");
  if (save) {
    save.value = "create";
    save.textContent = "Save command";
  }
  if (cancel) cancel.hidden = true;
  document.querySelectorAll(".schedule-item.is-editing").forEach((item) => item.classList.remove("is-editing"));
}

function fillCommandForm(command) {
  const form = commandForm();
  if (!form || !command) return;
  editingCommandId = String(command.id);
  form.querySelector("[name='command_id']").value = command.id;
  form.querySelector("[name='command_name']").value = command.name || "";
  form.querySelector("[name='command_aliases']").value = command.aliases_text || (command.aliases || []).join(", ");
  form.querySelector("[name='command_response']").value = command.response || "";
  form.querySelector("[name='command_cooldown']").value = String(command.cooldown_seconds || 0);
  const save = document.getElementById("custom-command-save");
  const cancel = document.getElementById("custom-command-cancel");
  if (save) {
    save.value = "update";
    save.textContent = "Update command";
  }
  if (cancel) cancel.hidden = false;
  form.scrollIntoView({ behavior: "smooth", block: "nearest" });
  form.querySelector("[name='command_response']")?.focus();
}

function renderCustomCommands(rows) {
  const host = document.querySelector("[data-live='custom_commands']");
  if (!host) return;
  customCommands = rows || [];
  if (editingCommandId && !customCommands.some((row) => String(row.id) === String(editingCommandId))) {
    resetCommandForm();
  }
  if (!customCommands.length) {
    host.innerHTML = `<p class="muted">No custom commands yet. Add one below, for example <code>${escapeHtml(prefix())}discord</code>.</p>`;
    return;
  }
  host.innerHTML = customCommands.map((row) => {
    const aliases = (row.aliases || []).map((name) => `${prefix()}${name}`).join(", ");
    const extras = [
      aliases ? `also ${aliases}` : "",
      row.cooldown_seconds ? `${row.cooldown_seconds}s cooldown` : "",
      row.use_count ? `used ${row.use_count}` : "",
    ].filter(Boolean).join(" · ");
    const editing = String(row.id) === String(editingCommandId);
    return `
    <article class="schedule-item ${row.enabled ? "" : "off"}${editing ? " is-editing" : ""}" data-command-id="${row.id}">
      <p><code>${escapeHtml(prefix())}${escapeHtml(row.name)}</code></p>
      <span class="schedule-meta">${escapeHtml(row.response)}${extras ? ` · ${escapeHtml(extras)}` : ""}</span>
      <div class="actions">
        <button type="button" class="ghost js-edit-command" data-id="${row.id}">Edit</button>
        <button type="button" class="ghost js-item" data-url="/commands" data-id="${row.id}" data-action="toggle">${row.enabled ? "Pause" : "Resume"}</button>
        <button type="button" class="ghost js-item" data-url="/commands" data-id="${row.id}" data-action="delete" data-confirm="Delete ${prefix()}${row.name}?">Delete</button>
      </div>
    </article>
  `;
  }).join("");
}

function renderScheduled(rows, streamLive = false) {
  const host = document.querySelector("[data-live='scheduled_messages']");
  if (!host) return;
  if (!rows || !rows.length) {
    host.innerHTML = `<p class="muted">No scheduled messages yet. They post in chat while the stream is live.</p>`;
    return;
  }
  const pauseNote = streamLive
    ? ""
    : `<p class="muted">Stream is offline. Timers are paused until you go live. Send now still posts immediately.</p>`;
  host.innerHTML = pauseNote + rows.map((row) => {
    const last = row.last_sent_at ? ` · Last sent ${String(row.last_sent_at).replace("T", " ").slice(0, 16)} UTC` : "";
    return `
      <article class="schedule-item ${row.enabled ? "" : "off"}">
        <p>${escapeHtml(row.message)}</p>
        <span class="schedule-meta">Every ${escapeHtml(row.interval_minutes)} min · ${row.enabled ? "On" : "Paused"}${escapeHtml(last)}</span>
        <div class="actions">
          <button type="button" class="ghost js-item" data-url="/schedule" data-id="${row.id}" data-action="toggle">${row.enabled ? "Pause" : "Resume"}</button>
          <button type="button" class="ghost js-item" data-url="/schedule" data-id="${row.id}" data-action="send">Send now</button>
          <button type="button" class="ghost js-item" data-url="/schedule" data-id="${row.id}" data-action="delete" data-confirm="Delete this scheduled message?">Delete</button>
        </div>
      </article>
    `;
  }).join("");
}

function renderBuiltinCommands(groups) {
  const host = document.getElementById("builtin-commands");
  if (!host || !groups) return;
  const focused = document.activeElement?.getAttribute?.("name");
  host.innerHTML = groups.map((group) => {
    const commands = (group.commands || []).map((cmd) => `
      <label class="module-toggle">
        <input type="checkbox" name="${escapeHtml(cmd.name)}" value="1" data-command="${escapeHtml(cmd.name)}" ${cmd.enabled ? "checked" : ""}>
        <span>
          <strong><code>${escapeHtml(prefix())}${escapeHtml(cmd.name)}</code></strong>
          <em>${escapeHtml(cmd.blurb)}</em>
        </span>
      </label>
    `).join("");
    return `
      <div class="command-group ${group.module_enabled ? "" : "module-off"}">
        <h3>${escapeHtml(group.label)}${group.module_enabled ? "" : " · module off"}</h3>
        <div class="module-grid">${commands}</div>
      </div>
    `;
  }).join("");
  if (focused) {
    const again = host.querySelector(`[name="${CSS.escape(focused)}"]`);
    if (again) again.focus();
  }
}

function applyModuleCards(features) {
  document.querySelectorAll("[data-module-card]").forEach((card) => {
    const key = card.getAttribute("data-module-card");
    const enabled = Boolean(features?.[key]?.enabled);
    card.classList.toggle("is-off", !enabled);
  });
}

let forceCommandRender = true;
let refreshInFlight = false;

function applyStatus(status, { commands = false } = {}) {
  const settings = status.settings || {};
  if (settings.primary_prefix) document.body.dataset.prefix = settings.primary_prefix;
  setPill(status);
  setText("channel", `#${status.channel || "offline"}`);
  setText("uptime", status.uptime || "Waiting for bot");
  setText("bot_name", status.bot_name || "CowBot");
  setText("prefix", settings.primary_prefix || "?");
  setText("active_giveaway", status.active_giveaway || "Idle");
  setText("giveaway_meta", giveawayMeta(status));
  const winnersInput = document.querySelector("[name='giveaway_winners']");
  if (winnersInput && status.giveaway_open && status.giveaway_winner_count) {
    winnersInput.value = String(status.giveaway_winner_count);
  }
  setText("active_poll", status.active_poll || "Idle");
  setText("poll_question", status.poll_question || "No active question");
  setText("active_raffle", status.active_raffle || "Idle");
  setText("raffle_cost", status.raffle_cost ? `${status.raffle_cost} points to enter` : "Waiting to start");
  renderLeaderboard(status.leaderboard);
  renderPoll(status);
  renderGiveaway(status);
  renderCustomCommands(status.custom_commands);
  renderScheduled(status.scheduled_messages, Boolean(status.stream_live));
  applyModuleCards(status.features);
  if (commands || forceCommandRender) {
    renderBuiltinCommands(status.builtin_command_groups);
    forceCommandRender = false;
  }
}

async function refreshStatus(options = {}) {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const response = await fetch("/api/status", { headers: JSON_HEADERS, cache: "no-store" });
    if (!response.ok) return;
    applyStatus(await response.json(), options);
  } catch (_error) {
    // Keep the last rendered state if the bot or dashboard is briefly unavailable.
  }
  refreshInFlight = false;
}

function setupTabs() {
  const buttons = [...document.querySelectorAll(".tab")];
  const panels = [...document.querySelectorAll("[data-panel]")];
  function show(name) {
    const tab = name || "live";
    buttons.forEach((button) => button.classList.toggle("is-active", button.dataset.tab === tab));
    panels.forEach((panel) => {
      const active = panel.dataset.panel === tab;
      panel.hidden = !active;
      panel.classList.toggle("is-active", active);
    });
    localStorage.setItem("cowbot-tab", tab);
  }
  buttons.forEach((button) => button.addEventListener("click", () => show(button.dataset.tab)));
  show(localStorage.getItem("cowbot-tab") || "live");
}

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function applyTheme(theme) {
  const next = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("cowbot-theme", next);
  const toggle = document.getElementById("theme-toggle");
  if (toggle) {
    const toLight = next === "dark";
    toggle.setAttribute("aria-label", toLight ? "Switch to light mode" : "Switch to dark mode");
    toggle.title = toLight ? "Light mode" : "Dark mode";
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = next === "light" ? "#f4f4f5" : "#09090b";
}

function setupTheme() {
  applyTheme(currentTheme());
  document.getElementById("theme-toggle")?.addEventListener("click", () => {
    applyTheme(currentTheme() === "dark" ? "light" : "dark");
  });
}

function setupForms() {
  document.querySelectorAll(".js-form").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submitter = event.submitter;
      const confirmText = submitter?.dataset.confirm;
      if (confirmText && !window.confirm(confirmText)) return;
      const body = new FormData(form);
      if (submitter?.name) body.set(submitter.name, submitter.value || "1");
      try {
        const data = await postAction(form.dataset.action, body);
        if (form.id === "custom-command-form") {
          resetCommandForm();
        } else if (submitter?.value === "create" || submitter?.classList.contains("primary")) {
          form.querySelectorAll("input:not([type='hidden']):not([type='number']), textarea").forEach((field) => {
            if (!field.readOnly && field.name !== "lurk_message" && !["daily_min", "daily_max", "starting_points", "default_raffle_cost", "watchtime_points", "watchtime_minutes", "prefixes", "interval_minutes"].includes(field.name)) {
              field.value = "";
            }
          });
        }
        showToast(data.message || "Saved.");
        await refreshStatus({ commands: true });
      } catch (error) {
        showToast(error.message || "Could not save.", "error");
      }
    });
  });
}

async function saveFeatures() {
  const flags = {};
  document.querySelectorAll("[data-feature]").forEach((input) => {
    flags[input.dataset.feature] = input.checked ? "1" : "0";
  });
  const data = await postAction("/features", flags);
  showToast(data.message || "Modules updated.");
  await refreshStatus({ commands: true });
}

async function saveCommands() {
  const commands = {};
  document.querySelectorAll("[data-command]").forEach((input) => {
    commands[input.name] = input.checked ? "1" : "0";
  });
  const data = await postAction("/builtin-commands", { commands });
  showToast(data.message || "Commands updated.");
  await refreshStatus({ commands: true });
}

function setupInstantToggles() {
  document.getElementById("module-grid")?.addEventListener("change", async (event) => {
    if (!event.target.matches("[data-feature]")) return;
    try {
      await saveFeatures();
    } catch (error) {
      event.target.checked = !event.target.checked;
      showToast(error.message || "Could not update module.", "error");
    }
  });
  document.getElementById("builtin-commands")?.addEventListener("change", async (event) => {
    if (!event.target.matches("[data-command]")) return;
    try {
      await saveCommands();
    } catch (error) {
      event.target.checked = !event.target.checked;
      showToast(error.message || "Could not update command.", "error");
    }
  });
}

function setupCommandEditor() {
  const cancel = document.getElementById("custom-command-cancel");
  cancel?.addEventListener("click", () => resetCommandForm());
  document.addEventListener("click", (event) => {
    const button = event.target.closest(".js-edit-command");
    if (!button) return;
    const command = customCommands.find((row) => String(row.id) === String(button.dataset.id));
    if (!command) {
      showToast("Could not load that command.", "error");
      return;
    }
    fillCommandForm(command);
    document.querySelectorAll(".schedule-item[data-command-id]").forEach((item) => {
      item.classList.toggle("is-editing", item.dataset.commandId === String(command.id));
    });
  });
}

function setupItemActions() {
  document.addEventListener("click", async (event) => {
    const button = event.target.closest(".js-item");
    if (!button) return;
    if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
    const body = new FormData();
    body.set("action", button.dataset.action);
    body.set("id", button.dataset.id);
    try {
      const data = await postAction(button.dataset.url, body);
      showToast(data.message || "Updated.");
      await refreshStatus({ commands: true });
    } catch (error) {
      showToast(error.message || "Could not update.", "error");
    }
  });
}

function setupGiveawayWheel() {
  const modal = document.getElementById("giveaway-wheel");
  const form = document.getElementById("giveaway-form");
  const pickButton = document.getElementById("pick-giveaway-winner");
  const cardReroll = document.getElementById("reroll-giveaway");
  const canvas = document.getElementById("giveaway-wheel-canvas");
  const disc = modal?.querySelector(".wheel-disc");
  const title = document.getElementById("wheel-title");
  const status = modal?.querySelector(".wheel-status");
  const result = modal?.querySelector(".wheel-result");
  const closeButton = modal?.querySelector("[data-wheel-close]");
  const rerollButton = modal?.querySelector("[data-wheel-reroll]");
  if (!modal || !form || !pickButton || !canvas || !disc) return;

  let spinning = false;
  let multiWinner = false;

  function resetWheel() {
    modal.hidden = true;
    modal.classList.remove("revealed");
    disc.style.transform = "rotate(0deg)";
    result.hidden = true;
    result.textContent = "";
    closeButton.hidden = true;
    if (rerollButton) rerollButton.hidden = true;
    status.hidden = false;
    status.textContent = "Entries are locked. The wheel picks one name.";
    title.textContent = "Spinning for a winner";
    pickButton.disabled = false;
    if (cardReroll) cardReroll.disabled = false;
    spinning = false;
  }

  async function completeDraw() {
    try {
      await fetch("/giveaway/complete", { method: "POST", headers: JSON_HEADERS });
    } catch (_error) {
      // The wheel already showed the winner; chat announce is best-effort.
    }
    refreshStatus();
  }

  async function requestDraw(path, fallbackError, extra = {}) {
    const response = await fetch(path, {
      method: "POST",
      headers: { ...JSON_HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify(extra),
    });
    const payload = await readJson(response);
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || fallbackError);
    }
    return payload;
  }

  function winnerNames(payload) {
    if (payload.winners && payload.winners.length) return payload.winners;
    return payload.winner ? [payload.winner] : [];
  }

  async function playSpin(payload, { reroll = false, index = 0, total = 1, previous = [] } = {}) {
    const winner = payload.winner;
    const exclude = [...(payload.excluded || []), payload.replaced, ...previous].filter(Boolean);
    const { slices, target } = buildSlices(payload.entries || [], winner, exclude);
    drawWheel(canvas, slices);
    modal.hidden = false;
    modal.classList.remove("revealed");
    title.textContent = reroll
      ? "Rerolling"
      : (total > 1 ? `Winner ${index + 1} of ${total}` : (payload.name ? `Giveaway ${payload.name}` : "Giveaway"));
    result.hidden = true;
    closeButton.hidden = true;
    if (rerollButton) rerollButton.hidden = true;
    status.hidden = false;
    status.textContent = reroll
      ? `${payload.replaced ? `${payload.replaced} is out. ` : ""}Spinning a replacement.`
      : (total > 1 ? `Spinning winner ${index + 1} of ${total}.` : "Entries are locked. The wheel picks one name.");

    const duration = prefersReducedMotion() ? 400 : 6200;
    await spinTo(disc, destinationAngle(disc, slices, target), duration);

    title.textContent = reroll ? "New winner" : (total > 1 ? `Winner ${index + 1} of ${total}` : "Winner");
    status.hidden = true;
    result.hidden = false;
    result.textContent = winner;
    modal.classList.add("revealed");
  }

  async function playSpins(payload, { reroll = false } = {}) {
    const winners = winnerNames(payload);
    if (!reroll) multiWinner = winners.length > 1;
    const previous = [];
    for (let index = 0; index < winners.length; index += 1) {
      disc.style.transform = "rotate(0deg)";
      await playSpin(
        { ...payload, winner: winners[index] },
        { reroll, index, total: winners.length, previous: [...previous] },
      );
      previous.push(winners[index]);
      if (index < winners.length - 1) {
        await new Promise((resolve) => setTimeout(resolve, prefersReducedMotion() ? 250 : 900));
      }
    }
    if (winners.length > 1) {
      title.textContent = "Winners";
      result.textContent = winners.join(" · ");
    }
    closeButton.hidden = false;
    if (rerollButton) rerollButton.hidden = multiWinner;
    await completeDraw();
  }

  async function startDraw(event) {
    event.preventDefault();
    if (spinning) return;
    const count = Math.max(1, Number(form.querySelector("[name='giveaway_winners']")?.value || 1));
    const label = count === 1 ? "a winner" : `${count} winners`;
    if (!window.confirm(`Lock entries and spin ${label}? This also plays on the OBS overlay.`)) return;
    spinning = true;
    pickButton.disabled = true;
    if (cardReroll) cardReroll.disabled = true;
    try {
      const payload = await requestDraw("/giveaway/draw", "Could not draw a winner.");
      await playSpins(payload);
    } catch (error) {
      showToast(error.message || "Could not draw a winner.", "error");
    }
    spinning = false;
    pickButton.disabled = false;
    if (cardReroll) cardReroll.disabled = false;
  }

  async function startReroll(event, replace) {
    event?.preventDefault?.();
    if (spinning) return;
    const confirmText = replace
      ? `Reroll ${replace} and skip current winners?`
      : "Reroll the last winner and skip previous winners?";
    if (!window.confirm(confirmText)) return;
    spinning = true;
    pickButton.disabled = true;
    if (cardReroll) cardReroll.disabled = true;
    if (rerollButton) rerollButton.disabled = true;
    try {
      const payload = await requestDraw(
        "/giveaway/reroll",
        "Could not reroll the giveaway.",
        replace ? { replace } : {},
      );
      await playSpins(payload, { reroll: true });
    } catch (error) {
      showToast(error.message || "Could not reroll the giveaway.", "error");
    }
    spinning = false;
    pickButton.disabled = false;
    if (cardReroll) cardReroll.disabled = false;
    if (rerollButton) rerollButton.disabled = false;
  }

  pickButton.addEventListener("click", startDraw);
  if (cardReroll) cardReroll.addEventListener("click", (event) => startReroll(event));
  if (rerollButton) rerollButton.addEventListener("click", (event) => startReroll(event));
  document.querySelector("[data-live='giveaway_live']")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-reroll-winner]");
    if (!button) return;
    startReroll(event, button.dataset.rerollWinner);
  });
  closeButton.addEventListener("click", resetWheel);
  modal.addEventListener("click", (event) => {
    if (event.target === modal && !closeButton.hidden) resetWheel();
  });
}

function setupCopyButtons() {
  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    const input = document.getElementById(button.dataset.copyTarget);
    if (!input) return;
    const original = button.textContent;
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(input.value);
        button.textContent = "Copied";
        setTimeout(() => {
          button.textContent = original;
        }, 1600);
      } catch (_error) {
        input.select();
        document.execCommand("copy");
      }
    });
  });
}

function showOAuthResult() {
  const result = new URLSearchParams(window.location.search).get("oauth");
  const messages = {
    ok: ["SimpleCowBot is connected. Watch points can see chatters now.", "success"],
    config: ["TWITCH_CLIENT_ID is missing on the dashboard service.", "error"],
    denied: ["Twitch login was cancelled.", "error"],
    bad: ["Twitch login failed. Try Authorize SimpleCowBot again.", "error"],
    redirect: ["Add the redirect URL to your Twitch app, then try again.", "error"],
    bot: ["The bot was offline, so the new login could not be saved. Start the bot and authorize again.", "error"],
  };
  const message = messages[result];
  if (!message) return;
  showToast(message[0], message[1]);
  const url = new URL(window.location.href);
  url.searchParams.delete("oauth");
  window.history.replaceState({}, "", url.pathname + url.search + url.hash);
}

setupTabs();
setupTheme();
setupForms();
setupInstantToggles();
setupCommandEditor();
setupItemActions();
setupGiveawayWheel();
setupCopyButtons();
showOAuthResult();
refreshStatus();
setInterval(refreshStatus, 8000);
