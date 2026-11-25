const $ = id => document.getElementById(id);
const D = { server: "http://127.0.0.1:6277", token: "", intercept: true, minSizeMB: 1 };
chrome.storage.sync.get(D, c => {
  $("server").value = c.server; $("token").value = c.token; $("min").value = c.minSizeMB; $("intercept").checked = c.intercept;
});
$("save").onclick = () => chrome.storage.sync.set({
  server: $("server").value.trim(), token: $("token").value.trim(),
  minSizeMB: +$("min").value || 0, intercept: $("intercept").checked
}, () => { $("ok").textContent = "Saved ✓"; });
