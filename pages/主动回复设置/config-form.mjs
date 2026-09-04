export function toNumberOrFallback(value, fallback) {
  if (value === "" || value === undefined || value === null) return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}
// 空串在此即非法：validateField 凭它拦下保存，避免静默沿用旧值。
// toNumberOrFallback 的 fallback 只留作序列化兜底（可达路径上走不到）。
export function numberFieldError(raw, min, max) {
  if (raw === "" || raw === undefined || raw === null) return "该项不能为空";
  const value = Number(raw);
  if (!Number.isFinite(value)) return "请输入有效数字";
  if (min != null && value < min) return `不能小于 ${min}`;
  if (max != null && value > max) return `不能大于 ${max}`;
  return "";
}
export function parseWhitelist(text) {
  return String(text || "")
    .split(/[\n,，]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}
function whitelistGroupId(item) {
  const parts = String(item || "").split(":");
  if (parts.length === 3 && parts[1].toLowerCase().includes("group")) {
    return parts[2].trim();
  }
  return "";
}
export function uniqueWhitelistItems(text) {
  const items = parseWhitelist(text);
  const bareIds = new Set(items.filter((item) => /^\d+$/.test(item)));
  const seen = new Set();
  const unique = [];
  for (const item of items) {
    const groupId = whitelistGroupId(item);
    if (groupId && bareIds.has(groupId)) continue;
    if (seen.has(item)) continue;
    seen.add(item);
    unique.push(item);
  }
  return unique;
}
export function summarizeWhitelist(text) {
  const items = parseWhitelist(text);
  const unique = uniqueWhitelistItems(text);
  if (unique.length === 0) return "未配置会话（留空则不主动回复任何会话）";
  const pureNumbers = unique.filter((i) => /^\d+$/.test(i)).length;
  const umos = unique.length - pureNumbers;
  const parts = [];
  if (pureNumbers > 0) parts.push(`${pureNumbers} 个纯群号`);
  if (umos > 0) parts.push(`${umos} 个完整 UMO`);
  const dupText = items.length > unique.length ? ` · 存在 ${items.length - unique.length} 处重复` : "";
  const base = `已识别 ${unique.length} 个有效会话（${parts.join("，")}）${dupText}`;
  if (unique.length > WHITELIST_MAX_COUNT)
    return `${base} · 超过 ${WHITELIST_MAX_COUNT} 条上限，多余条目会被截断或拒绝`;
  return base;
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
    if (Object.hasOwn(values, key)) {
      const val = escapeHtml(values[key]);
      return `<span class="prompt-var-tag" title="变量 {${key}}">${val}</span>`;
    }
    return match;
  });
}

export const WHITELIST_ITEM_MAX_LEN = 200;
export const WHITELIST_ILLEGAL_RE = /[\x00-\x1f"'\\]/;
// 与后端 models.MAX_WHITELIST_SIZE 同值：只做计数警告，不拦截，后端为准。
export const WHITELIST_MAX_COUNT = 1000;

export function validateWhitelistLines(text) {
  // 与 parseWhitelist 同一切分：保存按条目发包，校验必须按条目报，否则
  // `a,b` 写在一行时校验算 1 行、保存算 2 条，序号错位。
  const items = String(text || "")
    .split(/[\n,，]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  const errors = [];
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i];
    if (WHITELIST_ILLEGAL_RE.test(item)) {
      errors.push({
        line: i + 1,
        item,
        reason: "含非法字符（引号/反斜杠/控制字符）",
      });
    } else if (item.length > WHITELIST_ITEM_MAX_LEN) {
      errors.push({
        line: i + 1,
        item,
        reason: `超出 ${WHITELIST_ITEM_MAX_LEN} 字符上限`,
      });
    }
  }
  return errors;
}
