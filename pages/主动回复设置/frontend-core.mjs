export function normalizeApiError(error) {
  const message = String(error?.message || error || "");
  if (message === "Failed to fetch" || message.includes("fetch")) {
    return new Error("无法连接插件 API，请重载页面或重启 AstrBot 后重试");
  }
  return error;
}

/** GET /config 成功体：ok 必须为 true，且含写回所需关键字段。 */
export function isSuccessfulConfigPayload(config) {
  return (
    !!config &&
    typeof config === "object" &&
    config.ok === true &&
    typeof config.enabled === "boolean" &&
    Array.isArray(config.whitelist_sessions)
  );
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
  } catch (error) {
    throw normalizeApiError(error);
  }
}

export function providerNeedsManualInput(providerId, providers, listAvailable) {
  if (!listAvailable) return true;
  const value = String(providerId || "").trim();
  return value !== "" && !providers.some((provider) => provider?.id === value);
}
