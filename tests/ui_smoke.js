/* UI 冒烟测试：截图 + 收集 JS 运行时错误 */
const path = require("path");
const pwDir = path.join(process.env.TEMP || process.env.TMP, "pwcli", "node_modules", "playwright");
const { chromium } = require(pwDir);

const URL = "http://127.0.0.1:8765";
const ROOT = path.resolve(__dirname, "..");
const OUT = path.join(ROOT, "logs");

(async () => {
  const errors = [];
  let browser;
  try {
    browser = await chromium.launch({ channel: "msedge" });
  } catch (e) {
    browser = await chromium.launch();
  }
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });

  await page.goto(URL, { waitUntil: "networkidle" });
  await page.waitForTimeout(3500); // 等 WS hello + 首批 metrics

  const conn = await page.textContent("#conn-text");
  console.log("连接状态:", conn);

  // 概览截图
  await page.screenshot({ path: `${OUT}/ui-1-overview.png` });

  // 各视图截图
  for (const [view, file] of [["chat", "ui-2-chat"], ["logs", "ui-3-logs"], ["alerts", "ui-4-alerts"], ["memory", "ui-5-memory"], ["settings", "ui-6-settings"]]) {
    await page.click(`.nav button[data-view="${view}"]`);
    await page.waitForTimeout(800);
    await page.screenshot({ path: `${OUT}/${file}.png` });
  }

  // 回到日志页写入 ERROR 测试实时行
  await page.click('.nav button[data-view="logs"]');
  await page.waitForTimeout(600);
  const fs = require("fs");
  fs.appendFileSync(path.join(ROOT, "logs", "demo.log"), "ERROR playwright ui smoke test line\n");

  // 回对话页发消息（未配置 LLM key 应得到错误气泡，验证链路）
  await page.click('.nav button[data-view="chat"]');
  await page.fill("#chat-input", "现在系统状态怎么样？");
  await page.click("#chat-send");
  await page.waitForTimeout(6000);
  await page.screenshot({ path: `${OUT}/ui-7-chat-send.png` });

  const lastMsg = await page.evaluate(() => {
    const els = document.querySelectorAll("#chat-list .msg");
    const last = els[els.length - 1];
    return last ? last.textContent.slice(0, 120) : "(无消息)";
  });
  console.log("最后一条消息:", lastMsg.trim());

  console.log("JS 错误:", errors.length ? errors : "无");
  await browser.close();
  if (errors.length) process.exit(2);
})().catch((e) => { console.error("FAIL:", e); process.exit(1); });
