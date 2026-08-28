const SLICE_COLORS = ["#f4e7c3", "#e8c547", "#7dce7a", "#c45c4a", "#f0d3a8", "#9ec9a3"];

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function easeOutQuart(value) {
  return 1 - Math.pow(1 - value, 4);
}

function secureRandom() {
  if (globalThis.crypto && typeof globalThis.crypto.getRandomValues === "function") {
    const buffer = new Uint32Array(1);
    globalThis.crypto.getRandomValues(buffer);
    return buffer[0] / 4294967296;
  }
  return Math.random();
}

function normalizeName(name) {
  return String(name || "").trim().toLowerCase();
}

function uniqueNames(entries) {
  const seen = new Set();
  const names = [];
  for (const entry of entries || []) {
    const label = String(entry || "").trim();
    const key = normalizeName(label);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    names.push(label);
  }
  return names;
}

function shuffleNames(entries) {
  const names = [...entries];
  for (let index = names.length - 1; index > 0; index -= 1) {
    const swap = Math.floor(secureRandom() * (index + 1));
    [names[index], names[swap]] = [names[swap], names[index]];
  }
  return names;
}

function wheelEntries(entries, winner, exclude) {
  const blocked = new Set((exclude || []).map(normalizeName).filter(Boolean));
  const winnerKey = normalizeName(winner);
  blocked.delete(winnerKey);
  const source = uniqueNames(entries).filter((name) => !blocked.has(normalizeName(name)));
  if (winner && !source.some((name) => normalizeName(name) === winnerKey)) {
    source.push(String(winner));
  }
  return shuffleNames(source.length ? source : (winner ? [String(winner)] : []));
}

function buildSlices(entries, winner, exclude) {
  const source = wheelEntries(entries, winner, exclude);
  let slices = [...source];
  while (slices.length && slices.length < 8) slices = slices.concat(source);
  const winnerKey = normalizeName(winner);
  const matches = slices
    .map((name, index) => (normalizeName(name) === winnerKey ? index : -1))
    .filter((index) => index >= 0);
  const target = matches.length ? matches[Math.floor(secureRandom() * matches.length)] : 0;
  return { slices, target };
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
    ctx.font = `700 ${Math.max(10, Math.min(18, radius / 11))}px Figtree, sans-serif`;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const label = name.length > 14 ? `${name.slice(0, 13)}…` : name;
    ctx.fillText(label, radius - 18, 0);
    ctx.restore();
  });

  ctx.restore();
}

function currentRotation(element) {
  const match = /rotate\(([-\d.]+)deg\)/.exec(element.style.transform || "");
  return match ? parseFloat(match[1]) : 0;
}

function spinTo(element, degrees, duration) {
  const from = currentRotation(element);
  const delta = degrees - from;
  return new Promise((resolve) => {
    const start = performance.now();
    let done = false;
    function finish() {
      if (done) return;
      done = true;
      element.style.transform = `rotate(${degrees}deg)`;
      resolve();
    }
    function paint(now) {
      if (done) return;
      const progress = Math.min((now - start) / duration, 1);
      element.style.transform = `rotate(${from + delta * easeOutQuart(progress)}deg)`;
      if (progress >= 1) {
        finish();
        return;
      }
      requestAnimationFrame(paint);
    }
    requestAnimationFrame(paint);
    // OBS browser sources can stall requestAnimationFrame after the first spin.
    const timer = setInterval(() => {
      paint(performance.now());
      if (done) clearInterval(timer);
    }, 16);
    setTimeout(finish, duration + 400);
  });
}

function destinationAngle(disc, slices, target) {
  const arc = 360 / slices.length;
  const jitter = (secureRandom() - 0.5) * arc * 0.55;
  let desired = -((target + 0.5) * arc) + jitter;
  desired = ((desired % 360) + 360) % 360;
  const current = currentRotation(disc);
  let destination = desired;
  while (destination < current + 360 * 5) destination += 360;
  return destination;
}
