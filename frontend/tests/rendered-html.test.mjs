import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the administrative monitoring dashboard", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>中建探员 · 信息中国信息采集<\/title>/i);
  assert.match(html, /中建探员/);
  assert.match(html, /信息中国信息采集/);
  assert.match(html, /企业清单/);
  assert.match(html, /更新公告/);
  assert.match(html, /手动更新一轮/);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape|react-loading-skeleton/i);
});

test("keeps the dashboard wired to the real configurable API", async () => {
  const [apiSource, pageSource, packageSource] = await Promise.all([
    readFile(new URL("../app/crawler-api.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  assert.match(apiSource, /NEXT_PUBLIC_CRAWLER_API_BASE/);
  assert.match(apiSource, /window\.location\.hostname/);
  assert.match(apiSource, /8765\/api\/v1/);
  assert.match(pageSource, /crawlerApiJson/);
  assert.match(pageSource, /const flash = useCallback/);
  assert.match(pageSource, /\/monitor\/stop/);
  assert.match(pageSource, /停止全部/);
  assert.doesNotMatch(pageSource, /mockCompanies|demoCompanies|fakeData/i);
  assert.doesNotMatch(packageSource, /drizzle/);
});
