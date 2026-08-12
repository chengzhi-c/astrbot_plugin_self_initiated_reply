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
export function whitelistLineOk(line) {
  const s = String(line || "").trim();
  if (!s) return true;
  if (/^\d+$/.test(s)) return true;
  return /^[\w.-]+:[\w.-]+:[\w.-]+$/.test(s);
}
export const PROMPT_PREVIEW_VALUES = {
  session: "aiocqhttp:GroupMessage:123456789",
  trigger: "message_delay",
  bot_aliases: "阿绪, 咕咕",
  latest_message: "这个问题有没有更稳一点的做法？",
  recent_messages: [
    "[小林] 我刚试了下，直接改参数好像会让回复变得太积极。",
    "[阿茶] 是不是应该先看最近几条消息有没有明确空位？",
    "[小林] 对，我担心它在别人聊天正热的时候插进来。",
    "[阿茶] 这个问题有没有更稳一点的做法？",
  ].join("\n"),
  last_message_age_sec: "65",
  last_reply_age_sec: "900",
};
export function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
export function renderPromptTemplateHtml(template, values = PROMPT_PREVIEW_VALUES) {
  const escapedTemplate = escapeHtml(template);
  return escapedTemplate.replace(/\{([a-zA-Z0-9_]+)\}/g, (match, key) => {
    if (Object.prototype.hasOwnProperty.call(values, key)) {
      const val = escapeHtml(values[key]);
      return `<span class="prompt-var-tag" title="变量 {${key}}">${val}</span>`;
    }
    return match;
  });
}
