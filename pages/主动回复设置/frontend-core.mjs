export function normalizeApiError(error) {
  const message = String(error?.message || error || "");
  if (message === "Failed to fetch" || message.includes("fetch")) {
    return new Error("无法连接插件 API，请重载页面或重启 AstrBot 后重试");
  }
  return error;
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
  try {
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
    const response = await fetchImpl(url, {
      method: method === "POST" ? "POST" : undefined,
      credentials: "include",
      headers: method === "POST" ? { "Content-Type": "application/json" } : undefined,
      body: method === "POST" ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(timeoutMs),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(result?.error || `请求失败 (${response.status})`);
    }
    return result;
  } catch (error) {
    throw normalizeApiError(error);
  }
}

export function providerNeedsManualInput(providerId, providers, listAvailable) {
  if (!listAvailable) return true;
  const value = String(providerId || "").trim();
  return value !== "" && !providers.some((provider) => provider?.id === value);
}
