import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import { Readable } from "node:stream";

import worker from "../frontend/server/index.js";

const resourcesRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const clientRoot = join(resourcesRoot, "frontend", "client");
const port = Number(process.argv[2] || 3000);

const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".ico": "image/x-icon",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8",
  ".webp": "image/webp",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

function clientPath(pathname) {
  const decoded = decodeURIComponent(pathname).replace(/^\/+/, "");
  const candidate = normalize(join(clientRoot, decoded));
  return candidate.startsWith(clientRoot) ? candidate : "";
}

function assetResponse(request) {
  const path = clientPath(new URL(request.url).pathname);
  if (!path || !existsSync(path) || !statSync(path).isFile()) {
    return new Response("Not found", { status: 404 });
  }
  return new Response(Readable.toWeb(createReadStream(path)), {
    headers: { "Content-Type": contentTypes[extname(path)] || "application/octet-stream" },
  });
}

const server = createServer(async (incoming, outgoing) => {
  try {
    const url = new URL(incoming.url || "/", `http://127.0.0.1:${port}`);
    const staticPath = clientPath(url.pathname);
    if (staticPath && existsSync(staticPath) && statSync(staticPath).isFile()) {
      outgoing.writeHead(200, { "Content-Type": contentTypes[extname(staticPath)] || "application/octet-stream" });
      createReadStream(staticPath).pipe(outgoing);
      return;
    }

    const chunks = [];
    for await (const chunk of incoming) chunks.push(chunk);
    const body = chunks.length ? Buffer.concat(chunks) : undefined;
    const request = new Request(url, {
      method: incoming.method,
      headers: incoming.headers,
      body: ["GET", "HEAD"].includes(incoming.method || "GET") ? undefined : body,
    });
    const response = await worker.fetch(
      request,
      { ASSETS: { fetch: async (assetRequest) => assetResponse(assetRequest) } },
      { waitUntil() {}, passThroughOnException() {} },
    );
    const headers = Object.fromEntries(response.headers.entries());
    outgoing.writeHead(response.status, headers);
    if (response.body) Readable.fromWeb(response.body).pipe(outgoing);
    else outgoing.end();
  } catch (error) {
    outgoing.writeHead(500, { "Content-Type": "text/plain; charset=utf-8" });
    outgoing.end(`中建探员页面服务启动失败：${error instanceof Error ? error.message : String(error)}`);
  }
});

server.listen(port, "127.0.0.1", () => {
  process.stdout.write(`中建探员页面服务：http://127.0.0.1:${port}\n`);
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
