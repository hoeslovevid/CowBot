async function refreshStatus() {
  try {
    const response = await fetch("/", { headers: { "Accept": "text/html" } });
    if (!response.ok) return;
    const html = await response.text();
    const next = new DOMParser().parseFromString(html, "text/html");
    document.querySelectorAll("[data-status]").forEach((node) => {
      const key = node.getAttribute("data-status");
      const replacement = next.querySelector(`[data-status="${key}"]`);
      if (replacement) node.innerHTML = replacement.innerHTML;
    });
    const pill = document.querySelector(".pill");
    const nextPill = next.querySelector(".pill");
    if (pill && nextPill) pill.className = nextPill.className;
    if (pill && nextPill) pill.innerHTML = nextPill.innerHTML;
  } catch (_error) {
    // Keep the last rendered state if the bot or dashboard is briefly unavailable.
  }
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
      await fetch("/giveaway/complete", {
        method: "POST",
        headers: { "Accept": "application/json" },
      });
    } catch (_error) {
      // The wheel already showed the winner; chat announce is best-effort.
    }
    refreshStatus();
  }

  async function requestDraw(path, fallbackError) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Accept": "application/json" },
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || fallbackError);
    }
    return payload;
  }

  async function playSpin(payload, { reroll = false } = {}) {
    const winner = payload.winner;
    const { slices, target } = buildSlices(payload.entries || [], winner);
    drawWheel(canvas, slices);
    modal.hidden = false;
    modal.classList.remove("revealed");
    title.textContent = reroll
      ? "Rerolling"
      : (payload.name ? `Giveaway ${payload.name}` : "Giveaway");
    result.hidden = true;
    closeButton.hidden = true;
    if (rerollButton) rerollButton.hidden = true;
    status.hidden = false;
    status.textContent = reroll
      ? "Previous winner is out. Spinning again."
      : "Entries are locked. The wheel picks one name.";

    const duration = prefersReducedMotion() ? 400 : 6200;
    await spinTo(disc, destinationAngle(disc, slices, target), duration);

    title.textContent = reroll ? "New winner" : "Winner";
    status.hidden = true;
    result.hidden = false;
    result.textContent = winner;
    closeButton.hidden = false;
    if (rerollButton) rerollButton.hidden = false;
    modal.classList.add("revealed");
    await completeDraw();
  }

  async function startDraw(event) {
    event.preventDefault();
    if (spinning) return;
    spinning = true;
    pickButton.disabled = true;
    if (cardReroll) cardReroll.disabled = true;
    try {
      const payload = await requestDraw("/giveaway/draw", "Could not draw a winner.");
      disc.style.transform = "rotate(0deg)";
      await playSpin(payload);
    } catch (error) {
      window.alert(error.message || "Could not draw a winner.");
    }
    spinning = false;
    pickButton.disabled = false;
    if (cardReroll) cardReroll.disabled = false;
  }

  async function startReroll(event) {
    event.preventDefault();
    if (spinning) return;
    spinning = true;
    pickButton.disabled = true;
    if (cardReroll) cardReroll.disabled = true;
    if (rerollButton) rerollButton.disabled = true;
    try {
      const payload = await requestDraw("/giveaway/reroll", "Could not reroll the giveaway.");
      await playSpin(payload, { reroll: true });
    } catch (error) {
      window.alert(error.message || "Could not reroll the giveaway.");
    }
    spinning = false;
    pickButton.disabled = false;
    if (cardReroll) cardReroll.disabled = false;
    if (rerollButton) rerollButton.disabled = false;
  }

  pickButton.type = "button";
  pickButton.addEventListener("click", startDraw);
  if (cardReroll) cardReroll.addEventListener("click", startReroll);
  if (rerollButton) rerollButton.addEventListener("click", startReroll);
  closeButton.addEventListener("click", resetWheel);
  modal.addEventListener("click", (event) => {
    if (event.target === modal && !closeButton.hidden) resetWheel();
  });
}

function setupOverlayCopy() {
  const input = document.getElementById("overlay-url");
  const button = document.getElementById("copy-overlay-url");
  if (!input || !button) return;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(input.value);
      button.textContent = "Copied";
      setTimeout(() => {
        button.textContent = "Copy OBS URL";
      }, 1600);
    } catch (_error) {
      input.select();
      document.execCommand("copy");
    }
  });
}

setInterval(refreshStatus, 15000);
setupGiveawayWheel();
setupOverlayCopy();
