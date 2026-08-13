export function normalizeApiError(error) {
  const message = String(error?.message || error || "");
  if (
    message === "Failed to fetch" ||
    message === "NetworkError" ||
    message === "Load failed" ||
    message === "NetworkError when attempting to fetch resource."
  ) {
    return new Error("无法连接插件 API，请重载页面或重启 AstrBot 后重试");
  }
  return error;
}
export function isSuccessfulConfigPayload(config) {
  return (
    !!config &&
    typeof config === "object" &&
    config.ok === true &&
    typeof config.enabled === "boolean" &&
    Array.isArray(config.whitelist_sessions)
  );
}

const REQUEST_TIMEOUT_MESSAGE = "请求超时，请稍后重试";

async function withDeadline(operation, timeoutMs, onTimeout) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      reject(new Error(REQUEST_TIMEOUT_MESSAGE));
      onTimeout?.();
    }, timeoutMs);
  });
  try {
    return await Promise.race([operation(), timeout]);
  } finally {
    clearTimeout(timer);
  }
}

export async function requestPluginApi({
  getBridge,
  pluginId,
  endpoint,
  method,
  params = {},
  body = {},
  fetchImpl,
  pageUrl,
  timeoutMs = 15000,
}) {
  let controller;
  try {
    return await withDeadline(
      async () => {
        const bridge = await getBridge();
        if (bridge) {
          return method === "POST"
            ? await bridge.apiPost(endpoint, body)
            : await bridge.apiGet(endpoint, params);
        }
        const url = new URL(`/api/plug/${pluginId}/${endpoint}`, pageUrl);
        Object.entries(params).forEach(([key, value]) => {
          if (value !== undefined && value !== null && value !== "") {
            url.searchParams.set(key, value);
          }
        });
        controller = new AbortController();
        const response = await fetchImpl(url, {
          method: method === "POST" ? "POST" : undefined,
          credentials: "include",
          headers: method === "POST" ? { "Content-Type": "application/json" } : undefined,
          body: method === "POST" ? JSON.stringify(body) : undefined,
          signal: controller.signal,
        });
        let result;
        try {
          result = await response.json();
        } catch {
          throw new Error(
            response.ok ? "响应不是有效 JSON" : `请求失败 (${response.status})`
          );
        }
        if (!response.ok) {
          throw new Error(result?.error || `请求失败 (${response.status})`);
        }
        return result;
      },
      timeoutMs,
      () => controller?.abort()
    );
  } catch (error) {
    throw normalizeApiError(error);
  }
}
export function providerNeedsManualInput(providerId, providers, listAvailable) {
  if (!listAvailable) return true;
  const value = String(providerId || "").trim();
  return value !== "" && !providers.some((provider) => provider?.id === value);
}
