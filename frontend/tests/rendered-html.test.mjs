import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("构建产物包含监控看板页面", async () => {
  const html = await readFile(new URL("../dist/index.html", import.meta.url), "utf8");
  assert.match(html, /<title>中建探员 · 信息中国信息采集<\/title>/i);
  assert.match(html, /<div id="root"><\/div>/);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape|react-loading-skeleton/i);

  const scriptMatch = html.match(/src="(\/assets\/[^"]+\.js)"/);
  assert.ok(scriptMatch, "构建产物应引用打包后的 JS");
  const bundle = await readFile(new URL(`../dist${scriptMatch[1]}`, import.meta.url), "utf8");
  // 看板关键界面文案应进入打包产物
  for (const text of ["中建探员", "企业清单", "更新公告", "手动更新一轮"]) {
    assert.ok(bundle.includes(text), `打包产物缺少界面文案：${text}`);
  }
});

test("看板仍然连接真实的可配置采集 API", async () => {
  const [apiSource, pageSource, packageSource] = await Promise.all([
    readFile(new URL("../src/crawler-api.ts", import.meta.url), "utf8"),
    readFile(new URL("../src/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  assert.match(apiSource, /VITE_CRAWLER_API_BASE/);
  assert.match(apiSource, /window\.location\.hostname/);
  assert.match(apiSource, /8765\/api\/v1/);
  assert.match(pageSource, /crawlerApiJson/);
  assert.match(pageSource, /const flash = useCallback/);
  assert.match(pageSource, /\/monitor\/stop/);
  assert.match(pageSource, /停止全部/);
  assert.doesNotMatch(pageSource, /mockCompanies|demoCompanies|fakeData/i);
  assert.doesNotMatch(packageSource, /drizzle/);
});
