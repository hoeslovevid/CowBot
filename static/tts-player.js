(() => {
  const key = new URLSearchParams(location.search).get("key");
  const audio = document.getElementById("speech");
  const notice = document.getElementById("player-notice");
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  const client = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  let current = null;
  let claiming = false;
  let polling = false;
  let finishing = false;
  let blocked = false;
  let pendingFinish = null;
  let lastContact = Date.now();

  function message(text) { notice.replaceChildren(document.createTextNode(text)); }
  async function api(operation, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), operation === "claim" ? 50000 : 5000);
    try {
      const response = await fetch(`/tts/player/${operation}?client=${encodeURIComponent(client)}`, {
        method: body ? "POST" : "GET", cache: "no-store", signal: controller.signal,
        headers: { "X-TTS-Key": key, ...(body ? { "Content-Type": "application/json" } : {}) },
        ...(body ? { body: JSON.stringify({ ...body, client }) } : {}),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || "Speech player unavailable.");
      lastContact = Date.now();
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Connection interrupted. Reconnecting to CowBot…");
      throw error;
    } finally { clearTimeout(timer); }
  }
  async function flushFinish() {
    if (!pendingFinish) return;
    await api("finish", pendingFinish);
    pendingFinish = null;
  }
  async function finish(failed = false) {
    if (!current || finishing) return;
    finishing = true;
    const id = current.id;
    current = null;
    blocked = false;
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    pendingFinish = { id, failed };
    try { await flushFinish(); } catch (error) { message(error.message); }
    finishing = false;
  }
  function enableAudio() {
    blocked = true;
    message("Audio needs permission in this browser. In OBS, use Control audio via OBS.");
    const button = document.createElement("button");
    button.textContent = "Enable audio";
    button.addEventListener("click", async () => {
      try {
        const state = await api("state");
        if (!current || state.current_id !== current.id || state.paused) {
          blocked = false;
          message("");
          return;
        }
        await audio.play(); blocked = false; message("");
      }
      catch (_error) { message("Could not start audio. Check this browser's audio permissions."); }
    });
    notice.append(button);
  }
  async function next() {
    if (claiming || current || pendingFinish || finishing) return;
    claiming = true;
    try {
      const data = await api("claim", {});
      if (!data.job) return;
      current = data.job;
      const state = await api("state");
      if (!current || state.current_id !== current.id) { current = null; return; }
      audio.volume = data.job.volume / 100;
      audio.src = `/tts/player/audio?key=${encodeURIComponent(key)}&id=${encodeURIComponent(current.id)}&client=${encodeURIComponent(client)}`;
      if (state.paused) return;
      try {
        await audio.play();
        blocked = false;
        message("");
      } catch (error) {
        if (error.name === "NotAllowedError") enableAudio();
        else await finish(true);
      }
    } catch (error) { message(error.message); }
    finally { claiming = false; }
  }
  audio.addEventListener("ended", () => finish());
  audio.addEventListener("error", () => { if (current) finish(true); });
  async function tick() {
    if (polling || !key) return;
    polling = true;
    try {
      if (pendingFinish) await flushFinish();
      const state = await api("state");
      if (!current && !claiming && state.current_id) {
        await api("finish", { id: state.current_id, failed: true });
      }
      if (current) {
        if (state.current_id !== current.id) {
          audio.pause();
          audio.removeAttribute("src");
          audio.load();
          current = null;
          blocked = false;
        } else {
          audio.volume = state.volume / 100;
          if (state.paused && !audio.paused) audio.pause();
          if (!state.paused && audio.paused && !blocked) {
            try { await audio.play(); }
            catch (error) { if (error.name === "NotAllowedError") enableAudio(); else await finish(true); }
          }
        }
      }
      if (!blocked) message("");
      if (!current && !state.paused) next();
    } catch (error) {
      // Stop audio after losing control connectivity; don't speak through a missed skip.
      if (Date.now() - lastContact > 5000) audio.pause();
      message(error.message);
    } finally { polling = false; }
  }
  if (!key) message("Copy the complete OBS URL from CowBot → Text to speech.");
  else { tick(); setInterval(tick, 1000); }
})();
