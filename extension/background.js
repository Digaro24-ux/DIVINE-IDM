// DIVINE IDM browser integration (Chrome / Edge / Brave; Firefox 121+ with MV3 background scripts).
const DEFAULTS = { server: "http://127.0.0.1:6277", token: "", intercept: true, minSizeMB: 1 };

async function cfg() { return { ...DEFAULTS, ...(await chrome.storage.sync.get(DEFAULTS)) }; }

async function send(url) {
  const c = await cfg();
  if (!c.token) { chrome.runtime.openOptionsPage(); return false; }
  try {
    const r = await fetch(c.server.replace(/\/$/, "") + "/api/add", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Token": c.token },
      body: JSON.stringify({ url })
    });
    const d = await r.json();
    if (!r.ok) throw new Error((d.errors && d.errors[0] && d.errors[0].error) || d.error || r.statusText);
    notify("Sent to DIVINE IDM", url);
    return true;
  } catch (e) {
    notify("DIVINE IDM unreachable", String(e.message || e) + " — is server.py running?");
    return false;
  }
}

function notify(title, message) {
  chrome.notifications.create({ type: "basic", iconUrl: "icon.png", title, message: message.slice(0, 200) });
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "link", title: "Download link with DIVINE IDM", contexts: ["link"] });
  chrome.contextMenus.create({ id: "media", title: "Download media with DIVINE IDM", contexts: ["video", "audio", "image"] });
  chrome.contextMenus.create({ id: "page", title: "Download this page's video (YouTube/Bilibili…) with DIVINE IDM", contexts: ["page"] });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const url = info.linkUrl || info.srcUrl || info.pageUrl || (tab && tab.url);
  if (url) send(url);
});

// Take over the browser's own downloads: cancel them and hand them to DIVINE IDM.
const handled = new Set();
chrome.downloads.onCreated.addListener(async item => {
  const c = await cfg();
  if (!c.intercept || !c.token || handled.has(item.id)) return;
  const url = item.finalUrl || item.url;
  if (!/^(https?|ftp):/i.test(url)) return;                       // skip blob:, data:, file:
  if (item.fileSize > 0 && item.fileSize < c.minSizeMB * 1024 * 1024) return; // let small files through
  handled.add(item.id);
  const ok = await send(url);
  if (ok) {
    chrome.downloads.cancel(item.id, () => chrome.downloads.erase({ id: item.id }));
  }
});
