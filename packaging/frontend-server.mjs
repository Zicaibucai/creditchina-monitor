import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const resourcesRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const staticRoot = join(resourcesRoot, "frontend", "static");
const port = Number(process.argv[2] || 3000);

const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".ico": "image/x-icon",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8",
  ".webp": "image/webp",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

function staticPath(pathname) {
  const decoded = decodeURIComponent(pathname).replace(/^\/+/, "");
  const candidate = normalize(join(staticRoot, decoded));
  return candidate.startsWith(staticRoot) ? candidate : "";
}

const server = createServer((incoming, outgoing) => {
  try {
    const url = new URL(incoming.url || "/", `http://127.0.0.1:${port}`);
    let path = staticPath(url.pathname);
    if (!path || !existsSync(path) || !statSync(path).isFile()) {
      // 单页应用：未命中的路径回退到入口页。
      path = join(staticRoot, "index.html");
    }
    if (!existsSync(path)) {
      outgoing.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
      outgoing.end("前端尚未构建");
      return;
    }
    outgoing.writeHead(200, { "Content-Type": contentTypes[extname(path)] || "application/octet-stream" });
    createReadStream(path).pipe(outgoing);
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
