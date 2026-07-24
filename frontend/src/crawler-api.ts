const configuredApiBase = import.meta.env.VITE_CRAWLER_API_BASE?.replace(/\/$/, "");
const apiToken = import.meta.env.VITE_CRAWLER_API_TOKEN ?? "";

export const CRAWLER_API_BASE = configuredApiBase || (
  typeof window !== "undefined"
    ? `http://${window.location.hostname}:8765/api/v1`
    : "http://127.0.0.1:8765/api/v1"
);

function authHeaders(): Record<string, string> {
  return apiToken ? { "X-API-Token": apiToken } : {};
}

// <img>/<a> 标签无法携带请求头，令牌开启时以查询参数形式附加。
export function crawlerAssetUrl(path: string): string {
  const separator = path.includes("?") ? "&" : "?";
  return apiToken
    ? `${CRAWLER_API_BASE}${path}${separator}token=${encodeURIComponent(apiToken)}`
    : `${CRAWLER_API_BASE}${path}`;
}

function connectionMessage() {
  return `无法连接采集 API（${CRAWLER_API_BASE}）。请确认后端已运行，且访问设备与这台电脑处于同一局域网。`;
}

export async function crawlerApiJson<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${CRAWLER_API_BASE}${path}`, {
      ...init,
      cache: "no-store",
      headers: {
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...authHeaders(),
        ...(init?.headers ?? {}),
      },
    });
  } catch {
    throw new Error(connectionMessage());
  }
  const payload = await response.json().catch(() => ({})) as T & { detail?: string; error?: string };
  if (!response.ok) throw new Error(payload.detail || payload.error || `接口请求失败（${response.status}）`);
  return payload;
}

function filenameFromDisposition(value: string | null) {
  if (!value) return "creditchina-export.xlsx";
  const encoded = value.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (encoded) return decodeURIComponent(encoded);
  return value.match(/filename="?([^";]+)"?/i)?.[1] || "creditchina-export.xlsx";
}

export async function downloadRealWorkbook(mode: "current" | "penalties" | "all", company = "") {
  const query = company.trim() ? `?company=${encodeURIComponent(company.trim())}` : "";
  let response: Response;
  try {
    response = await fetch(`${CRAWLER_API_BASE}/exports/${mode}${query}`, { cache: "no-store", headers: authHeaders() });
  } catch {
    throw new Error(connectionMessage());
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new Error(payload.detail || "导出接口请求失败");
  }
  const filename = filenameFromDisposition(response.headers.get("Content-Disposition"));
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  return filename;
}

export async function downloadEvidencePackage(announcementId: number) {
  let response: Response;
  try {
    response = await fetch(`${CRAWLER_API_BASE}/monitor/announcements/${announcementId}/package`, { cache: "no-store", headers: authHeaders() });
  } catch {
    throw new Error(connectionMessage());
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new Error(payload.detail || "证据包生成失败");
  }
  const filename = filenameFromDisposition(response.headers.get("Content-Disposition")).replace(/\.xlsx$/i, ".zip");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  return filename;
}

export async function downloadCompanyEvidencePackage(captureId: number) {
  let response: Response;
  try {
    response = await fetch(`${CRAWLER_API_BASE}/monitor/evidence/${captureId}/package`, { cache: "no-store", headers: authHeaders() });
  } catch {
    throw new Error(connectionMessage());
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new Error(payload.detail || "公司证据包生成失败");
  }
  const filename = filenameFromDisposition(response.headers.get("Content-Disposition")).replace(/\.xlsx$/i, ".zip");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  return filename;
}
