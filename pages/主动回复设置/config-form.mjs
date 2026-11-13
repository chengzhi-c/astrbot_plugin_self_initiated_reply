// 空串在此即非法：validateField 凭它拦下保存，序列化侧因此不必再兜底。
// integer 由控件 data-integer 标注，与后端 kind=="int" 一一对应（契约测试钉）：
// 后端 _strict_int 拒绝一切非 int、_strict_float 接受任意有限小数，step 属性
// 只是滑杆增量（message_delay_sec step=5 但 47 合法），不能拿来当整除判据。
export function numberFieldError(raw, min, max, integer = false) {
  if (raw === "" || raw === undefined || raw === null) return "该项不能为空";
  const value = Number(raw);
  if (!Number.isFinite(value)) return "请输入有效数字";
  if (integer && !Number.isInteger(value)) return "请输入整数";
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
// UMO 是 platform:消息类型:会话 ID；单 token 是裸群键（Telegram 负数 chat_id、
// 飞书 oc_ 开头的会话 ID 都靠它命中），两段或含空白在运行期一定匹配不到。
const BARE_KEY_RE = /^[^\s:]+$/;
const FULL_UMO_RE = /^[^\s:]+:[^\s:]+:[^\s]+$/;
export function whitelistKind(item) {
  if (/^\d+$/.test(item)) return "number";
  if (FULL_UMO_RE.test(item)) return "umo";
  if (BARE_KEY_RE.test(item)) return "key";
  return "unknown";
}
export function uniqueWhitelistItems(text, parsedItems = null) {
  // ``parsedItems`` 是可选优化口：``summarizeWhitelist`` 已 parse 过时直接复用，
  // 避免对同一份文本重复切分（输入事件每键触发一次，白名单几百行时不浪费）。
  const items = parsedItems || parseWhitelist(text);
  // 裸群键（纯群号或非数字会话键）与它的群 UMO 是同一个会话，后端按 group_id
  // 精确匹配，所以折叠集合必须包含两类而不只是纯数字。
  const bareIds = new Set(items.filter((item) => ["number", "key"].includes(whitelistKind(item))));
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
export function summarizeWhitelist(text, parsedItems = null) {
  const items = parsedItems || parseWhitelist(text);
  const unique = uniqueWhitelistItems(text, items);
  if (unique.length === 0) return "未配置会话（留空则不主动回复任何会话）";
  let pureNumbers = 0;
  let keys = 0;
  let umos = 0;
  let unknown = 0;
  for (const item of unique) {
    const kind = whitelistKind(item);
    if (kind === "number") pureNumbers += 1;
    else if (kind === "key") keys += 1;
    else if (kind === "umo") umos += 1;
    else unknown += 1;
  }
  const parts = [];
  if (pureNumbers > 0) parts.push(`${pureNumbers} 个纯群号`);
  if (keys > 0) parts.push(`${keys} 个群键`);
  if (umos > 0) parts.push(`${umos} 个完整 UMO`);
  if (unknown > 0) parts.push(`${unknown} 条格式无法识别`);
  const dupText = items.length > unique.length ? ` · 存在 ${items.length - unique.length} 处重复` : "";
  // 说「共 N 条」不说「有效」：这里不校验字符集与长度，保存时会被后端拦下的
  // 非法条目一样被计数。用「有效」会让运营者以为这些会话真的生效了。
  const base = `共 ${unique.length} 条（${parts.join("，")}）${dupText}`;
  if (unique.length > WHITELIST_MAX_COUNT)
    return `${base} · 超过 ${WHITELIST_MAX_COUNT} 条上限，多余条目会被截断或拒绝`;
  return base;
}
const PROMPT_PREVIEW_VALUES = {
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
function escapeHtml(str) {
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

const WHITELIST_ITEM_MAX_LEN = 200;
export const WHITELIST_ILLEGAL_RE = /[\x00-\x1f"'\\]/;
// 与后端 models.MAX_WHITELIST_SIZE 同值：只做计数警告，不拦截，后端为准。
const WHITELIST_MAX_COUNT = 1000;

export function validateWhitelistLines(text) {
  // 必须走 parseWhitelist：保存按条目发包，校验按同一份切分报序号。
  // 各写一遍时，`a,b` 写在一行会让校验算 1 行、保存算 2 条。
  const items = parseWhitelist(text);
  const errors = [];
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i];
    if (WHITELIST_ILLEGAL_RE.test(item)) {
      errors.push({
        line: i + 1,
        item,
        reason: "含非法字符（引号/反斜杠/控制字符）",
      });
    } else if ([...item].length > WHITELIST_ITEM_MAX_LEN) {
      errors.push({
        line: i + 1,
        item,
        reason: `超出 ${WHITELIST_ITEM_MAX_LEN} 字符上限`,
      });
    } else if (whitelistKind(item) === "unknown") {
      errors.push({
        line: i + 1,
        item,
        reason: "不是群号、群键或完整 UMO（含空白或缺段）",
      });
    }
  }
  return errors;
}
