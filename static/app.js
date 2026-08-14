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

setInterval(refreshStatus, 15000);
