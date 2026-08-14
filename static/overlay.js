function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function setupOverlayWheel() {
  const stage = document.getElementById("overlay-wheel");
  const canvas = document.getElementById("overlay-wheel-canvas");
  const disc = stage?.querySelector(".wheel-disc");
  const kicker = document.getElementById("overlay-kicker");
  const title = document.getElementById("overlay-title");
  const winnerLabel = document.getElementById("overlay-winner");
  if (!stage || !canvas || !disc) return;

  let seenId = null;
  let spinning = false;
  let primed = false;

  async function playSpin(payload) {
    const winner = payload.winner;
    const reroll = Boolean(payload.reroll);
    const { slices, target } = buildSlices(payload.entries || [], winner);
    disc.style.transform = "rotate(0deg)";
    drawWheel(canvas, slices);
    stage.hidden = false;
    stage.classList.remove("revealed", "leaving");
    kicker.textContent = reroll ? "Reroll" : "Giveaway";
    title.textContent = reroll ? "Spinning again" : (payload.name ? payload.name : "Giveaway");
    winnerLabel.hidden = true;
    winnerLabel.textContent = "";

    const duration = prefersReducedMotion() ? 400 : 6500;
    await spinTo(disc, destinationAngle(disc, slices, target), duration);

    title.textContent = reroll ? "New winner" : "Winner";
    winnerLabel.hidden = false;
    winnerLabel.textContent = winner;
    stage.classList.add("revealed");
    await sleep(prefersReducedMotion() ? 1800 : 9000);
    stage.classList.add("leaving");
    await sleep(700);
    stage.hidden = true;
    stage.classList.remove("revealed", "leaving");
  }

  async function poll() {
    if (spinning) return;
    try {
      const response = await fetch("/overlay/giveaway/state", { headers: { "Accept": "application/json" } });
      if (!response.ok) return;
      const data = await response.json();
      const spin = data && data.spin;
      const spinId = spin && spin.id;
      if (!primed) {
        seenId = spinId || null;
        primed = true;
        return;
      }
      if (!spinId || spinId === seenId) return;
      seenId = spinId;
      spinning = true;
      await playSpin(spin);
    } catch (_error) {
      // Keep the overlay idle if the dashboard or bot is briefly unreachable.
    }
    spinning = false;
  }

  poll();
  setInterval(poll, 750);
}

setupOverlayWheel();
