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
  return `已识别 ${unique.length} 个有效会话（${parts.join("，")}）${dupText}`;
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

export function validateWhitelistLines(text) {
  const lines = String(text || "").split("\n");
  const errors = [];
  for (let i = 0; i < lines.length; i += 1) {
    const raw = lines[i];
    const line = raw.trim();
    if (!line) continue;
    if (WHITELIST_ILLEGAL_RE.test(line)) {
      errors.push({
        line: i + 1,
        item: line,
        reason: "含非法字符（引号/反斜杠/控制字符）",
      });
    } else if (line.length > WHITELIST_ITEM_MAX_LEN) {
      errors.push({
        line: i + 1,
        item: line,
        reason: `超出 ${WHITELIST_ITEM_MAX_LEN} 字符上限`,
      });
    }
  }
  return errors;
}
