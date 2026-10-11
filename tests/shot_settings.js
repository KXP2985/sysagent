const path = require("path");
const pwDir = path.join(process.env.TEMP || process.env.TMP, "pwcli", "node_modules", "playwright");
const { chromium } = require(pwDir);
const ROOT = path.resolve(__dirname, "..");

(async () => {
  const b = await chromium.launch({ channel: "msedge" });
  const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
  await p.goto("http://127.0.0.1:8765");
  await p.waitForSelector("#chat-list .msg", { state: "attached", timeout: 15000 });
  await p.click('.nav button[data-view="settings"]');
  await p.waitForSelector(".llm-card", { state: "attached", timeout: 15000 });
  await p.waitForTimeout(400);
  await p.screenshot({ path: path.join(ROOT, "logs", "ui-settings-models.png") });
  const cards = await p.$$eval(".llm-card", (els) =>
    els.map((e) => e.textContent.replace(/\s+/g, " ").slice(0, 80)));
  console.log("cards:", cards);
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
