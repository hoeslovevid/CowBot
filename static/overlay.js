function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function winnerList(payload) {
  if (Array.isArray(payload.winners) && payload.winners.length) {
    return payload.winners.map((name) => String(name)).filter(Boolean);
  }
  if (typeof payload.winners === "string" && payload.winners.trim()) {
    return payload.winners.split(/[,·]/).map((name) => name.trim()).filter(Boolean);
  }
  return payload.winner ? [String(payload.winner)] : [];
}

function isFreshSpin(spin) {
  const created = Date.parse(spin && spin.created_at);
  if (Number.isNaN(created)) return Boolean(spin && spin.id);
  const count = Math.max(1, winnerList(spin).length);
  return Date.now() - created < Math.max(180000, count * 25000);
}

function resetDisc(disc) {
  disc.style.transform = "none";
  void disc.offsetWidth;
  disc.style.transform = "rotate(0deg)";
  void disc.offsetWidth;
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
  const queue = [];

  async function playSpin(payload) {
    const winners = winnerList(payload);
    if (!winners.length) return;
    const reroll = Boolean(payload.reroll);
    stage.hidden = false;
    stage.classList.remove("revealed", "leaving");
    void stage.offsetWidth;

    for (let index = 0; index < winners.length; index += 1) {
      const winner = winners[index];
      const { slices, target } = buildSlices(payload.entries || [], winner);
      resetDisc(disc);
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

  async function drain() {
    if (spinning) return;
    spinning = true;
    try {
      while (queue.length) {
        const next = queue.shift();
        seenId = next.id;
        try {
          await playSpin(next);
        } catch (_error) {
          // Keep draining later spins if one overlay frame errors.
        }
      }
    } finally {
      spinning = false;
      if (queue.length) drain();
    }
  }

  async function poll() {
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
        if (!(spinId && isFreshSpin(spin))) {
          seenId = spinId || null;
          return;
        }
      }
      if (!spinId || spinId === seenId) return;
      if (queue.some((item) => item.id === spinId)) return;
      queue.push(spin);
      drain();
    } catch (_error) {
      // Keep the overlay idle if the dashboard or bot is briefly unreachable.
    }
  }

  poll();
  setInterval(poll, 500);
}

setupOverlayWheel();
