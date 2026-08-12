/** 配置表单：默认值、形状校验、数值/白名单校验。 */

export const DEFAULT_CONFIG = {
  decision_temperature: 0.2,
  decision_timeout_sec: 20,
  decision_history_min_messages: 5,
  message_delay_sec: 60,
  min_silence_sec: 45,
  cooldown_sec: 900,
  vision_max_images: 2,
  vision_image_age_sec: 300,
  vision_timeout_sec: 20,
};

export function num(value, fallback) {
  if (value === "" || value === undefined || value === null) return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function parseWhitelist(text) {
  return String(text || "")
    .split(/[\n,，]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

/** 白名单行：允许 UMO 或纯数字群号。 */
export function whitelistLineOk(line) {
  const s = String(line || "").trim();
  if (!s) return true;
  if (/^\d+$/.test(s)) return true;
  // platform:type:id
  return /^[\w.-]+:[\w.-]+:[\w.-]+$/.test(s);
}
