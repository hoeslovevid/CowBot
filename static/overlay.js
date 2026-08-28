function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isFreshSpin(spin) {
  const created = Date.parse(spin && spin.created_at);
  if (Number.isNaN(created)) return Boolean(spin && spin.id);
  return Date.now() - created < 30000;
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
    const winners = (payload.winners && payload.winners.length) ? payload.winners : [payload.winner];
    const reroll = Boolean(payload.reroll);
    stage.hidden = false;
    stage.classList.remove("revealed", "leaving");
    void stage.offsetWidth;

    for (let index = 0; index < winners.length; index += 1) {
      const winner = winners[index];
      const { slices, target } = buildSlices(payload.entries || [], winner);
      disc.style.transform = "rotate(0deg)";
      drawWheel(canvas, slices);
      kicker.textContent = reroll
        ? (payload.replaced ? `${payload.replaced} is out` : "Reroll")
        : (winners.length > 1 ? `Winner ${index + 1} of ${winners.length}` : "Giveaway");
      title.textContent = reroll
        ? "Spinning again"
        : (payload.name ? payload.name : "Giveaway");
      winnerLabel.hidden = true;
      winnerLabel.textContent = "";
      stage.classList.remove("revealed");

      const duration = prefersReducedMotion() ? 400 : 6500;
      await spinTo(disc, destinationAngle(disc, slices, target), duration);

      title.textContent = reroll ? "New winner" : (winners.length > 1 ? `Winner ${index + 1} of ${winners.length}` : "Winner");
      winnerLabel.hidden = false;
      winnerLabel.textContent = winner;
      stage.classList.add("revealed");
      await sleep(prefersReducedMotion() ? 900 : (index === winners.length - 1 ? 9000 : 2800));
    }

    if (winners.length > 1) {
      title.textContent = "Winners";
      winnerLabel.textContent = winners.join(" · ");
      await sleep(prefersReducedMotion() ? 1200 : 4000);
    }
    stage.classList.add("leaving");
    await sleep(700);
    stage.hidden = true;
    stage.classList.remove("revealed", "leaving");
  }

  async function poll() {
    if (spinning) return;
    try {
      const response = await fetch("/overlay/giveaway/state", {
        cache: "no-store",
        headers: { "Accept": "application/json" },
      });
      if (!response.ok) return;
      const data = await response.json();
      const spin = data && data.spin;
      const spinId = spin && spin.id;
      if (!primed) {
        primed = true;
        if (spinId && isFreshSpin(spin)) {
          spinning = true;
          await playSpin(spin);
          seenId = spinId;
          spinning = false;
        } else {
          seenId = spinId || null;
        }
        return;
      }
      if (!spinId || spinId === seenId) return;
      spinning = true;
      await playSpin(spin);
      seenId = spinId;
    } catch (_error) {
      // Keep the overlay idle if the dashboard or bot is briefly unreachable.
    }
    spinning = false;
  }

  poll();
  setInterval(poll, 500);
}

setupOverlayWheel();
