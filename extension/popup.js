const $ = id => document.getElementById(id);
chrome.storage.sync.get({ server: "http://127.0.0.1:6277", token: "", intercept: true }, c => {
  $("intercept").checked = c.intercept;
  $("intercept").onchange = () => chrome.storage.sync.set({ intercept: $("intercept").checked });
  $("opts").onclick = () => chrome.runtime.openOptionsPage();
  $("open").onclick = () => chrome.tabs.create({ url: c.server });
  $("send").onclick = async () => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!c.token) return chrome.runtime.openOptionsPage();
    const r = await fetch(c.server + "/api/add", { method: "POST",
      headers: { "Content-Type": "application/json", "X-Token": c.token }, body: JSON.stringify({ url: tab.url }) });
    $("send").textContent = r.ok ? "✓ Sent" : "✗ Failed (see app)";
  };
});
