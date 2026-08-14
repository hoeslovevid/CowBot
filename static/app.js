const SLICE_COLORS = ["#f4e7c3", "#e8c547", "#7dce7a", "#c45c4a", "#f0d3a8", "#9ec9a3"];

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

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function easeOutQuart(value) {
  return 1 - Math.pow(1 - value, 4);
}

function buildSlices(entries, winner) {
  const source = (entries && entries.length ? entries : [winner]).map((name) => String(name));
  let slices = [...source];
  while (slices.length < 8) slices = slices.concat(source);
  const winnerKey = String(winner).toLowerCase();
  const matches = slices
    .map((name, index) => (name.toLowerCase() === winnerKey ? index : -1))
    .filter((index) => index >= 0);
  return { slices, target: matches[Math.floor(matches.length / 2)] ?? 0 };
}

function contrastColor(hex) {
  const value = hex.replace("#", "");
  const red = parseInt(value.slice(0, 2), 16);
  const green = parseInt(value.slice(2, 4), 16);
  const blue = parseInt(value.slice(4, 6), 16);
  const luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255;
  return luminance > 0.62 ? "#17140f" : "#fff8e8";
}

function drawWheel(canvas, slices) {
  const ratio = window.devicePixelRatio || 1;
  const size = canvas.clientWidth || 420;
  canvas.width = size * ratio;
  canvas.height = size * ratio;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  const radius = size / 2;
  const arc = (Math.PI * 2) / slices.length;
  ctx.clearRect(0, 0, size, size);
  ctx.save();
  ctx.translate(radius, radius);

  slices.forEach((name, index) => {
    const start = index * arc - Math.PI / 2;
    const color = SLICE_COLORS[index % SLICE_COLORS.length];
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.arc(0, 0, radius - 2, start, start + arc);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
    ctx.strokeStyle = "rgba(23, 20, 15, 0.18)";
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.save();
    ctx.rotate(start + arc / 2);
    ctx.fillStyle = contrastColor(color);
    ctx.font = `700 ${Math.max(10, Math.min(16, radius / 11))}px Figtree, sans-serif`;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const label = name.length > 14 ? `${name.slice(0, 13)}…` : name;
    ctx.fillText(label, radius - 18, 0);
    ctx.restore();
  });

  ctx.restore();
}

function spinTo(element, degrees, duration) {
  return new Promise((resolve) => {
    const start = performance.now();
    function frame(now) {
      const progress = Math.min((now - start) / duration, 1);
      const angle = degrees * easeOutQuart(progress);
      element.style.transform = `rotate(${angle}deg)`;
      if (progress < 1) {
        requestAnimationFrame(frame);
      } else {
        resolve();
      }
    }
    requestAnimationFrame(frame);
  });
}

function setupGiveawayWheel() {
  const modal = document.getElementById("giveaway-wheel");
  const form = document.getElementById("giveaway-form");
  const pickButton = document.getElementById("pick-giveaway-winner");
  const canvas = document.getElementById("giveaway-wheel-canvas");
  const disc = modal?.querySelector(".wheel-disc");
  const title = document.getElementById("wheel-title");
  const status = modal?.querySelector(".wheel-status");
  const result = modal?.querySelector(".wheel-result");
  const closeButton = modal?.querySelector("[data-wheel-close]");
  if (!modal || !form || !pickButton || !canvas || !disc) return;

  let spinning = false;

  function resetWheel() {
    modal.hidden = true;
    modal.classList.remove("revealed");
    disc.style.transform = "rotate(0deg)";
    result.hidden = true;
    result.textContent = "";
    closeButton.hidden = true;
    status.hidden = false;
    status.textContent = "Entries are locked. The wheel picks one name.";
    title.textContent = "Spinning for a winner";
    pickButton.disabled = false;
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

  async function startDraw(event) {
    event.preventDefault();
    if (spinning) return;
    spinning = true;
    pickButton.disabled = true;

    let payload = {};
    try {
      const response = await fetch("/giveaway/draw", {
        method: "POST",
        headers: { "Accept": "application/json" },
      });
      payload = await response.json();
      if (!response.ok || !payload.ok) {
        throw new Error(payload.error || "Could not draw a winner.");
      }
    } catch (error) {
      spinning = false;
      pickButton.disabled = false;
      window.alert(error.message || "Could not draw a winner.");
      return;
    }

    const winner = payload.winner;
    const { slices, target } = buildSlices(payload.entries || [], winner);
    drawWheel(canvas, slices);
    modal.hidden = false;
    title.textContent = payload.name ? `Giveaway ${payload.name}` : "Giveaway";
    result.hidden = true;
    closeButton.hidden = true;
    status.hidden = false;

    const arc = 360 / slices.length;
    const jitter = (Math.random() - 0.5) * arc * 0.55;
    const destination = 360 * 6 - ((target + 0.5) * arc) + jitter;
    const duration = prefersReducedMotion() ? 400 : 6200;
    await spinTo(disc, destination, duration);

    title.textContent = "Winner";
    status.hidden = true;
    result.hidden = false;
    result.textContent = winner;
    closeButton.hidden = false;
    modal.classList.add("revealed");
    await completeDraw();
    spinning = false;
    pickButton.disabled = false;
  }

  pickButton.type = "button";
  pickButton.addEventListener("click", startDraw);
  closeButton.addEventListener("click", resetWheel);
  modal.addEventListener("click", (event) => {
    if (event.target === modal && !closeButton.hidden) resetWheel();
  });
}

setInterval(refreshStatus, 15000);
setupGiveawayWheel();
