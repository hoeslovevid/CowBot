(() => {
  const celebration = document.getElementById("celebration");
  const activeHost = document.getElementById("active-boosts");
  const preview = new URLSearchParams(location.search).get("preview") === "1";
  const seen = new Set();
  const pending = [];
  let active = [];
  let clockOffset = 0;
  let primed = false;
  let polling = false;
  let showing = false;

  function now() { return Date.now() + clockOffset; }

  function remaining(until) {
    return Math.max(0, Math.ceil((Date.parse(until) - now()) / 1000));
  }

  function duration(seconds) {
    const minutes = Math.floor(seconds / 60);
    return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
  }

  function renderActive() {
    activeHost.replaceChildren();
    for (const boost of active) {
      const seconds = remaining(boost.until);
      if (!Number.isFinite(seconds) || seconds <= 0) continue;
      const card = document.createElement("article");
      card.className = "active-boost";
      const title = document.createElement("strong");
      title.textContent = boost.label;
      const buyer = document.createElement("span");
      buyer.textContent = `Thanks, ${boost.user}`;
      const timer = document.createElement("time");
      timer.textContent = duration(seconds);
      card.append(title, buyer, timer);
      activeHost.append(card);
    }
  }

  function playNext() {
    if (showing) return;
    let event;
    while (pending.length) {
      const candidate = pending.shift();
      if (now() - Date.parse(candidate.created_at) < 120000 && remaining(candidate.until) > 0) {
        event = candidate;
        break;
      }
    }
    if (!event) return;
    showing = true;
    celebration.setAttribute("data-color", ["mint", "gold", "violet", "rose"].includes(event.color) ? event.color : "mint");
    const badge = document.getElementById("reward-badge");
    if (badge) badge.textContent = event.kind === "shop" ? "★" : "×2";
    document.getElementById("boost-action").textContent = event.kind === "shop" ? event.label : `${event.label} ${event.extended ? "extended" : "activated"}`;
    document.getElementById("boost-buyer").textContent = event.user;
    document.getElementById("boost-effect").textContent = event.summary;
    const title = event.title ? `[${event.title}] · ` : "";
    document.getElementById("boost-detail").textContent = title + (event.detail || `+${event.minutes} minutes for everyone · ${Number(event.cost).toLocaleString()} points`);
    celebration.hidden = false;
    setTimeout(() => {
      celebration.hidden = true;
      showing = false;
      setTimeout(playNext, 350);
    }, 8000);
  }

  function acceptState(data) {
    const serverTime = Date.parse(data.server_time);
    if (Number.isFinite(serverTime)) clockOffset = serverTime - Date.now();
    active = Array.isArray(data.active) ? data.active : [];
    const events = Array.isArray(data.events) ? data.events : [];
    for (const event of events) {
      if (!event.id || seen.has(event.id)) continue;
      seen.add(event.id);
      // First connection restores timers without replaying earlier purchases.
      if (primed) pending.push(event);
    }
    // Bound memory while retaining IDs still present in the server's recent history.
    if (seen.size > 300) {
      seen.clear();
      events.forEach((event) => seen.add(event.id));
    }
    primed = true;
    renderActive();
    playNext();
  }

  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const response = await fetch("/overlay/boosts/state", { cache: "no-store", signal: AbortSignal.timeout(6000) });
      if (!response.ok) return;
      const data = await response.json();
      if (data.ok) acceptState(data);
    } catch (_error) {
      // Existing countdowns continue through a brief connection failure.
    } finally {
      polling = false;
    }
  }

  if (preview) {
    const event = { id: "preview", label: "Double Watch", user: "GenerousCow", summary: "Double watch points for everyone!",
      cost: 500, minutes: 15, extended: false, created_at: new Date().toISOString(), until: new Date(Date.now() + 900000).toISOString() };
    active = [event, { ...event, label: "Double Luck", user: "LuckyCow", until: new Date(Date.now() + 480000).toISOString() }];
    pending.push(event);
    renderActive();
    playNext();
  } else {
    poll();
    setInterval(poll, 2000);
  }
  setInterval(renderActive, 1000);
})();
