import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  createConfigRequestCoordinator,
  isSuccessfulConfigPayload,
  normalizeApiError,
  providerNeedsManualInput,
  requestPluginApi,
} from "../pages/主动回复设置/frontend-core.mjs";
import {
  buildConfigSaveBody,
  configSaveKeys,
  createConfigIo,
} from "../pages/主动回复设置/config-io.mjs";
import {
  renderPromptTemplateHtml,
  summarizeWhitelist,
  uniqueWhitelistItems,
  validateWhitelistLines,
} from "../pages/主动回复设置/config-form.mjs";
import { THEME_KEY } from "../pages/主动回复设置/theme.mjs";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const pageDir = join(root, "pages", "主动回复设置");
const MAX_SOURCE_LINE = 200;
const TEST_REVISION = `sha256:${"a".repeat(64)}`;

test("bridge rejection is normalized by the shared API request", async () => {
  const bridge = {
    apiGet: async () => {
      throw new Error("Failed to fetch");
    },
  };

  await assert.rejects(
    requestPluginApi({
      getBridge: async () => bridge,
      endpoint: "config",
      method: "GET",
      params: {},
      fetchImpl: async () => {
        throw new Error("fetch fallback must not run");
      },
      pageUrl: "http://localhost/",
    }),
    (error) => error instanceof Error && error.message === "无法连接插件 API，请重载页面或重启 AstrBot 后重试"
  );
});

test("bridge discovery and calls share the request deadline", async () => {
  const pending = () => new Promise(() => {});
  const cases = [
    { method: "GET", getBridge: pending },
    { method: "GET", getBridge: async () => ({ apiGet: pending }) },
    { method: "POST", getBridge: async () => ({ apiPost: pending }) },
  ];

  for (const testCase of cases) {
    const request = requestPluginApi({
      ...testCase,
      endpoint: "config",
      fetchImpl: async () => {
        throw new Error("fetch fallback must not run");
      },
      pageUrl: "http://localhost/",
      timeoutMs: 20,
    });
    const observed = Promise.race([
      request,
      new Promise((resolve) => setTimeout(() => resolve("outer-timeout"), 250)),
    ]);
    await assert.rejects(
      observed,
      (error) => error instanceof Error && error.message === "请求超时，请稍后重试"
    );
  }
});

test("fetch response parsing is covered by the same deadline", async () => {
  const request = requestPluginApi({
    getBridge: async () => null,
    pluginId: "plugin-id",
    endpoint: "config",
    method: "GET",
    fetchImpl: async () => ({
      ok: true,
      status: 200,
      json: () => new Promise(() => {}),
    }),
    pageUrl: "http://localhost/",
    timeoutMs: 20,
  });
  const observed = Promise.race([
    request,
    new Promise((resolve) => setTimeout(() => resolve("outer-timeout"), 250)),
  ]);

  await assert.rejects(
    observed,
    (error) => error instanceof Error && error.message === "请求超时，请稍后重试"
  );
});

test("fetch fallback keeps the established GET and POST request shapes", async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      status: 200,
      json: async () => ({ ok: true }),
    };
  };
  const base = {
    getBridge: async () => null,
    pluginId: "plugin-id",
    fetchImpl,
    pageUrl: "https://dashboard.example/path",
  };

  await requestPluginApi({ ...base, endpoint: "config", method: "GET", params: { page: 2 } });
  await requestPluginApi({ ...base, endpoint: "config", method: "POST", body: { enabled: true } });

  assert.equal(calls[0].url, "https://dashboard.example/api/plug/plugin-id/config?page=2");
  assert.equal(calls[0].options.credentials, "include");
  assert.equal(calls[0].options.method, undefined);
  assert.equal(calls[1].options.method, "POST");
  assert.deepEqual(calls[1].options.headers, { "Content-Type": "application/json" });
  assert.equal(calls[1].options.body, '{"enabled":true}');
});

test("provider list failure forces manual input for every provider control", () => {
  assert.equal(providerNeedsManualInput("", [], false), true);
  assert.equal(providerNeedsManualInput("provider-x", [], false), true);
  assert.equal(providerNeedsManualInput("", [], true), false);
  assert.equal(providerNeedsManualInput("provider-x", [{ id: "provider-x" }], true), false);
  assert.equal(providerNeedsManualInput("provider-x", [{ id: "provider-y" }], true), true);
});

test("API errors retain a stable user-facing message", () => {
  assert.equal(
    normalizeApiError(new Error("Failed to fetch")).message,
    "无法连接插件 API，请重载页面或重启 AstrBot 后重试"
  );
  const original = new Error("validation failed");
  assert.equal(normalizeApiError(original), original);
  const upstream = new Error("failed to fetch session history");
  assert.equal(normalizeApiError(upstream), upstream);
});

test("HTTP 200 non-JSON response fails closed", async () => {
  await assert.rejects(
    requestPluginApi({
      getBridge: async () => null,
      pluginId: "plugin-id",
      endpoint: "config",
      method: "GET",
      fetchImpl: async () => ({
        ok: true,
        status: 200,
        json: async () => {
          throw new SyntaxError("Unexpected token");
        },
      }),
      pageUrl: "https://dashboard.example/",
    }),
    (error) => error instanceof Error && error.message === "响应不是有效 JSON"
  );
});

test("config request coordinator protects load epochs and unknown writes", () => {
  const coordinator = createConfigRequestCoordinator();
  const first = coordinator.beginLoad();
  const second = coordinator.beginLoad();
  assert.equal(coordinator.canApplyLoad(first, false), false);
  assert.equal(coordinator.canApplyLoad(second, true), false);
  const forced = coordinator.beginLoad(true);
  coordinator.markEdited();
  assert.equal(coordinator.canApplyLoad(forced, true, true), false);
  coordinator.markWriteUnknown();
  assert.equal(coordinator.writeUnknown, true);
  coordinator.clearWriteUnknown();
  assert.equal(coordinator.writeUnknown, false);
});

test("config payload requires ok true and write-critical fields", () => {
  assert.equal(isSuccessfulConfigPayload(null), false);
  assert.equal(isSuccessfulConfigPayload({}), false);
  assert.equal(isSuccessfulConfigPayload({ ok: false, error: "x" }), false);
  assert.equal(
    isSuccessfulConfigPayload({ ok: true, enabled: true }),
    false
  );
  assert.equal(
    isSuccessfulConfigPayload({
      ok: true,
      enabled: false,
      whitelist_sessions: "not-array",
      config_revision: TEST_REVISION,
    }),
    false
  );
  assert.equal(
    isSuccessfulConfigPayload(
      {
        ok: true,
        enabled: true,
        whitelist_sessions: [],
        config_revision: TEST_REVISION,
      },
      ["cooldown_sec"],
    ),
    false,
  );
});

test("config load failure names the missing fields", async () => {
  // 后端返回 ok:true 但缺表单声明的键时，错误文案必须点出缺哪个键；
  // 统一一句"配置加载失败"让排障者无从下手。
  const field = (key) => ({ dataset: { configKey: key } });
  const form = {
    classList: { add() {}, remove() {}, toggle() {} },
    inert: false,
    querySelector: () => null,
    querySelectorAll: () => [field("cooldown_sec"), field("min_silence_sec")],
  };
  const io = createConfigIo({
    getEls: () => ({ configForm: form }),
    getState: () => ({
      configLoaded: false,
      savingConfig: false,
      configRevision: "",
      isDirty: false,
    }),
    setState() {},
    apiGet: async () => ({
      ok: true,
      enabled: true,
      whitelist_sessions: [],
      config_revision: TEST_REVISION,
      cooldown_sec: 900,
    }),
    apiPost: async () => ({}),
    showToast() {},
    setStatState() {},
    renderPromptPreview() {},
    judgeProviderControl: { value: () => "", sync() {} },
    visionProviderControl: { value: () => "", sync() {} },
    visionJudgeProviderControl: { value: () => "", sync() {} },
    fmtBool: String,
  });
  await assert.rejects(() => io.loadConfig(), /缺少字段 min_silence_sec/);
});

test("page exposes the accessibility and narrow-layout contracts", async () => {
  const [html, app, css, chrome, configIo, providers] = await Promise.all([
    readFile(join(pageDir, "index.html"), "utf8"),
    readFile(join(pageDir, "app.js"), "utf8"),
    readFile(join(pageDir, "style.css"), "utf8"),
    readFile(join(pageDir, "chrome.mjs"), "utf8"),
    readFile(join(pageDir, "config-io.mjs"), "utf8"),
    readFile(join(pageDir, "providers.mjs"), "utf8"),
  ]);
  const fe = [app, chrome, configIo, providers].join("\n");
  assert.match(html, /<main\b[^>]*class="layout"/);
  assert.match(html, /<form id="configForm"[^>]*\binert/);
  assert.match(html, /class="skip-link"[^>]*href="#selfStat"/);
  assert.match(app, /requestPluginApi\(/);
  assert.match(app, /createProviderControl\(/);
  assert.match(
    providers,
    /providerNeedsManualInput\(next, getOptions\(\), isListAvailable\(\)\)/
  );
  assert.match(
    chrome,
    /event\.key !== "Escape" \|\| !media\.matches \|\| els\.moreActionsMenu\.hidden/
  );
  assert.match(html, /id="moreActionsBtn"[^>]*aria-controls="moreActionsMenu"[^>]*aria-expanded="false"/);
  assert.match(html, /id="judgeProviderInput"[^>]*hidden/);
  assert.match(html, /超时则本次不主动回应/);
  assert.doesNotMatch(html, /超时按默认放行/);
  assert.match(fe, /isSuccessfulConfigPayload\(/);
  assert.match(fe, /btn\.disabled = blocked/);
  assert.match(css, /@media \(max-width: 360px\)/);
  assert.match(css, /more-actions-menu\[hidden\]/);
  assert.doesNotMatch(css, /\.master, \.panel, \.form-actions/);
  assert.doesNotMatch(css, /\.form > \*:nth-child\([2-6]\) \{ animation-delay/);
});

test("every data-config-control in the page is registered in config-io", async () => {
  // configControlValue 直接 providerControls[configControl].value()，未注册即裸
  // TypeError。当前 HTML 声明与注册表一致故运行时不可达；这条守的是"新增控件
  // 忘了注册"的漂移，而不是给生产路径加死检查。
  const [html, configIo] = await Promise.all([
    readFile(join(pageDir, "index.html"), "utf8"),
    readFile(join(pageDir, "config-io.mjs"), "utf8"),
  ]);
  const declared = new Set(
    [...html.matchAll(/data-config-control="([^"]+)"/g)].map((m) => m[1]),
  );
  assert.ok(declared.size > 0, "index.html declares no data-config-control");
  const registry = configIo.match(/const providerControls = \{([\s\S]*?)\};/);
  assert.ok(registry, "config-io.mjs providerControls registry not found");
  const registered = new Set(
    [...registry[1].matchAll(/(\w+):/g)].map((m) => m[1]),
  );
  const missing = [...declared].filter((name) => !registered.has(name));
  assert.deepEqual(
    missing,
    [],
    `data-config-control 未在 providerControls 注册：${missing}`,
  );
});

test("context-history setting describes its fallback behavior", async () => {
  const html = await readFile(join(pageDir, "index.html"), "utf8");

  assert.match(html, /判断时参考的聊天记录/);
  assert.match(html, /少于多少条时补读历史/);
  assert.match(
    html,
    /本插件记录的文字消息少于此数时，会尝试读取同一会话的旧消息，帮助判断要不要接话。设为\s+0 时，只看插件已记录的消息。/
  );
  assert.doesNotMatch(html, /上下文至少几条消息才判断接话/);
});

test("CI runs the dependency-free frontend gate", async () => {
  const workflow = await readFile(join(root, ".github", "workflows", "ci.yml"), "utf8");
  assert.match(workflow, /^ {2}frontend:\r?\n/m);
  assert.match(workflow, /actions\/setup-node@v4/);
  // 覆盖全部设置页 JS/MJS，而不是只检查入口两文件。
  assert.match(workflow, /pages\/主动回复设置\/\*\.\{js,mjs\}/);
  assert.match(workflow, /node --check/);
  assert.match(workflow, /node --test tests\/frontend_contract\.test\.mjs/);
});

test("theme localStorage key stays single-sourced with the HTML bootstrap", async () => {
  const html = await readFile(join(pageDir, "index.html"), "utf8");
  const htmlKey = html.match(/localStorage\.getItem\("([^"]+)"\)/)?.[1];
  assert.equal(htmlKey, THEME_KEY);
  assert.equal(html.split(THEME_KEY).length - 1, 1);
});

test("frontend plugin id matches the backend package identity", async () => {
  const app = await readFile(join(pageDir, "app.js"), "utf8");
  const models = await readFile(join(root, "models.py"), "utf8");
  const feId = app.match(/const PLUGIN_ID = "([^"]+)"/)?.[1];
  const beId = models.match(/PLUGIN_ID = "([^"]+)"/)?.[1];
  assert.equal(feId, "astrbot_plugin_self_initiated_reply");
  assert.equal(feId, beId);
});

test("dark accent tokens are declared once and reused", async () => {
  const css = await readFile(join(pageDir, "style.css"), "utf8");
  assert.equal((css.match(/#e0a040/g) || []).length, 1);
  assert.match(css, /--accent:\s*var\(--accent-light\)/);
  assert.match(css, /:root\[data-theme="dark"\]/);
  assert.match(css, /prefers-color-scheme:\s*dark/);
});

test("the two dark token blocks stay token-identical", async () => {
  // 深色令牌写了两份：:root[data-theme="dark"]（显式深色）与
  // @media (prefers-color-scheme: dark) 下的 :root:not([data-theme])（跟随系统）。
  // 合并成一份需要引入"选中态 vs 解析态"双状态机（currentTheme 从 data-theme
  // 读取，auto 一旦被解析成具体值就不可表示，nextTheme 的三态循环会断），
  // 复杂度大于收益，故保留两份并用这条测试把"改一处忘另一处"变成红灯。
  const css = await readFile(join(pageDir, "style.css"), "utf8");
  const explicit = css.match(
    /:root\[data-theme="dark"\]\s*\{([\s\S]*?)\n\}/,
  );
  const system = css.match(
    /@media \(prefers-color-scheme: dark\) \{\s*\n\t:root:not\(\[data-theme\]\) \{([\s\S]*?)\n\t\}/,
  );
  assert.ok(explicit && system, "dark token blocks not found in style.css");
  const normalize = (body) =>
    body
      .split("\n")
      .map((line) => line.replace(/\s+/g, " ").trim())
      .filter((line) => line && !line.startsWith("color-scheme"))
      .join("\n");
  assert.equal(
    normalize(system[1]),
    normalize(explicit[1]),
    "两份深色令牌块漂移：改一处必须改另一处",
  );
});

test("page wires the manual image cache cleanup control to the API", async () => {
  // 清理按钮必须接入页面与 API，而不是只能重载插件。此前这条断言住在
  // tests/test_vision.py 里读前端源码——前端改名即红，与识图无关，搬回契约测试。
  const [html, configIo] = await Promise.all([
    readFile(join(pageDir, "index.html"), "utf8"),
    readFile(join(pageDir, "config-io.mjs"), "utf8"),
  ]);
  assert.match(html, /id="cleanupImageCacheBtn"/);
  assert.match(configIo, /apiPost\("image-cache\/cleanup"/);
});

test("number inputs keep their hint in aria-describedby", async () => {
  // 校验错误此前独占 aria-describedby，读屏用户聚焦输入框时听不到"建议 30–120
  // 秒"这类操作必需提示。这条守两件事：HTML 里 hint 有 id 且被 input 引用；
  // config-io 合并而非覆盖 aria-describedby。
  const [html, configIo] = await Promise.all([
    readFile(join(pageDir, "index.html"), "utf8"),
    readFile(join(pageDir, "config-io.mjs"), "utf8"),
  ]);
  const blocks = [...html.matchAll(/<label class="field">([\s\S]*?)<\/label>/g)].map(
    (m) => m[1],
  );
  let checked = 0;
  for (const block of blocks) {
    const inputTag = block.match(/<input\b[\s\S]*?\/>/);
    if (!inputTag || !/type="number"/.test(inputTag[0])) continue;
    const id = inputTag[0].match(/id="(\w+)"/);
    const described = inputTag[0].match(/aria-describedby="([^"]+)"/);
    const hint = block.match(/<p class="field-hint" id="(\w+)"/);
    assert.ok(id, `number input without id: ${inputTag[0].slice(0, 40)}`);
    assert.ok(hint, `number ${id[1]} has a field-hint with no id`);
    assert.ok(
      described && described[1].split(" ").includes(hint[1]),
      `number ${id[1]} aria-describedby 未引用其 hint`,
    );
    checked += 1;
  }
  // 与全文独立计数交叉比对：label 块正则一旦失配（改结构、换 class），
  // checked 会静默变小甚至归零而测试仍绿。不写死字段个数，新增字段自动纳入。
  const totalNumberInputs = (html.match(/type="number"/g) || []).length;
  assert.ok(
    checked > 0 && checked === totalNumberInputs,
    `仅核对 ${checked} 个 number 字段，页面共 ${totalNumberInputs} 个：label 结构可能已变`,
  );
  assert.match(
    configIo,
    /getAttribute\("aria-describedby"\)/,
    "setupValidation 必须合并已有 aria-describedby，不能覆盖 hint 关联",
  );
});

test("theme label names match between CSS content and JS labels", async () => {
  // 主题名（跟随系统/慈爱之惠/审判之司）写了两处：style.css 的 .theme-label::after
  // content（短标签）与 theme.mjs 的 THEME_LABELS（带"浅色 ·"/"深色 ·"前缀，用于
  // aria-label）。改一处忘另一处会让可见文字与读屏播报不一致，且无人报错。
  // 这条守的是"CSS 短标签必须是 JS 完整标签的子串"，不要求字面相等。
  const [css, theme] = await Promise.all([
    readFile(join(pageDir, "style.css"), "utf8"),
    readFile(join(pageDir, "theme.mjs"), "utf8"),
  ]);
  const cssLabels = [...css.matchAll(/\.theme-label::after \{\s*\n\s*content: "([^"]+)"/g)].map(
    (m) => m[1],
  );
  assert.equal(cssLabels.length, 3, "expected 3 theme-label content rules");
  const jsLabels = [...theme.matchAll(/(?:auto|light|dark): "([^"]+)"/g)].map((m) => m[1]);
  assert.equal(jsLabels.length, 3, "expected 3 THEME_LABELS entries");
  for (const cssLabel of cssLabels) {
    assert.ok(
      jsLabels.some((js) => js.includes(cssLabel)),
      `CSS 主题标签 "${cssLabel}" 未出现在任何 JS THEME_LABELS 中`,
    );
  }
});

test("script load failure fallback does not depend on the module", async () => {
  const html = await readFile(join(pageDir, "index.html"), "utf8");
  const chrome = await readFile(join(pageDir, "chrome.mjs"), "utf8");
  assert.match(html, /脚本加载失败，请刷新/);
  assert.match(html, /__selfreplyAppStarted/);
  assert.doesNotMatch(html, /<script type="module">[\s\S]*脚本加载失败/);
  assert.match(chrome, /桌面菜单常显/);
  assert.match(chrome, /removeAttribute\("aria-expanded"\)/);
});

test("config save path follows the form-declared writable keys", async () => {
  const expectedKeys = [
    "abandon_stale_on_new_message",
    "cooldown_sec",
    "decision_history_min_messages",
    "decision_model_enabled",
    "decision_prompt_template",
    "decision_temperature",
    "decision_timeout_sec",
    "enabled",
    "enabled_private_sessions",
    "judge_provider_id",
    "message_delay_sec",
    "min_silence_sec",
    "proactive_inherit_tools",
    "vision_image_age_sec",
    "vision_judge_enabled",
    "vision_judge_provider_id",
    "vision_main_enabled",
    "vision_max_images",
    "vision_provider_id",
    "vision_skip_stickers",
    "vision_timeout_sec",
    "whitelist_sessions",
  ];
  const html = await readFile(join(pageDir, "index.html"), "utf8");
  const htmlKeys = [...html.matchAll(/data-config-key="([a-z0-9_]+)"/g)].map((match) => match[1]);
  assert.deepEqual(htmlKeys.sort(), expectedKeys);

  const field = (key, value = "", dataset = {}) => ({
    dataset: { configKey: key, ...dataset },
    type: "text",
    value,
  });
  const number = (key, value) => ({ dataset: { configKey: key }, type: "number", value });
  const checkbox = (key, checked = false) => ({
    dataset: { configKey: key },
    type: "checkbox",
    checked,
  });
  const providerField = (key, control) => ({
    dataset: { configKey: key, configControl: control },
  });
  const classList = { add() {}, remove() {}, toggle() {} };
  const fields = [
    checkbox("enabled", true),
    checkbox("enabled_private_sessions", true),
    checkbox("abandon_stale_on_new_message", false),
    checkbox("decision_model_enabled", true),
    providerField("judge_provider_id", "judge"),
    number("decision_temperature", "0.3"),
    number("decision_timeout_sec", "21"),
    field("decision_prompt_template", "  prompt  ", { configTransform: "trim" }),
    number("decision_history_min_messages", "6"),
    number("message_delay_sec", "61"),
    number("min_silence_sec", "46"),
    number("cooldown_sec", "901"),
    checkbox("vision_judge_enabled", true),
    checkbox("vision_main_enabled", true),
    checkbox("vision_skip_stickers", true),
    providerField("vision_provider_id", "vision"),
    providerField("vision_judge_provider_id", "visionJudge"),
    number("vision_max_images", "3"),
    number("vision_image_age_sec", "301"),
    number("vision_timeout_sec", "22"),
    checkbox("proactive_inherit_tools", true),
    field("whitelist_sessions", "group:a\ngroup:b", { configTransform: "whitelist" }),
  ];
  const form = {
    classList,
    inert: false,
    querySelector: () => null,
    querySelectorAll: () => fields,
  };
  const elements = {
    configForm: form,
    whitelistInput: fields.at(-1),
    whitelistCount: { textContent: "" },
  };
  const state = {
    configLoaded: true,
    savingConfig: false,
    configRevision: TEST_REVISION,
    isDirty: true,
  };
  const posts = [];
  let lastToast = "";
  const provider = (value) => ({ value: () => value, sync() {} });
  const controls = {
    judge: provider("judge"),
    vision: provider("vision"),
    visionJudge: provider("vision-judge"),
  };
  assert.deepEqual(configSaveKeys(form).sort(), expectedKeys);
  assert.deepEqual(buildConfigSaveBody(form, controls).whitelist_sessions, ["group:a", "group:b"]);
  const io = createConfigIo({
    getEls: () => elements,
    getState: () => state,
    setState: (updates) => Object.assign(state, updates),
    apiGet: async () => {
      throw new Error("skip refresh");
    },
    apiPost: async (endpoint, body) => {
      posts.push({ endpoint, body });
      return {
        ok: true,
        enabled: true,
        whitelist_sessions: [],
        config_revision: TEST_REVISION,
      };
    },
    showToast(message) {
      lastToast = message;
    },
    setStatState() {},
    renderPromptPreview() {},
    judgeProviderControl: controls.judge,
    visionProviderControl: controls.vision,
    visionJudgeProviderControl: controls.visionJudge,
    fmtBool: String,
  });

  await io.saveConfig({ preventDefault() {} });

  assert.equal(posts.length, 1);
  assert.equal(posts[0].endpoint, "config");
  assert.deepEqual(
    Object.keys(posts[0].body).sort(),
    [...configSaveKeys(form), "base_revision"].sort(),
  );
  assert.equal(posts[0].body.base_revision, TEST_REVISION);
  assert.deepEqual(posts[0].body.whitelist_sessions, ["group:a", "group:b"]);
  assert.equal(posts[0].body.decision_prompt_template, "prompt");
  assert.equal(state.savingConfig, false);
  assert.equal(state.isDirty, true);
  assert.equal(state.requiresConfigRefresh, true);
  assert.match(lastToast, /保存状态未知/);
  assert.equal(form.inert, false);
});

test("successful save applies the returned config and clears dirty state", async () => {
  const field = (key, value = "", dataset = {}) => ({
    dataset: { configKey: key, ...dataset },
    type: "text",
    value,
  });
  const number = (key, value) => ({ dataset: { configKey: key }, type: "number", value });
  const checkbox = (key, checked = false) => ({
    dataset: { configKey: key },
    type: "checkbox",
    checked,
  });
  const providerField = (key, control) => ({
    dataset: { configKey: key, configControl: control },
  });
  const classList = { add() {}, remove() {}, toggle() {} };
  const fields = [
    checkbox("enabled", true),
    checkbox("enabled_private_sessions", true),
    checkbox("abandon_stale_on_new_message", false),
    checkbox("decision_model_enabled", true),
    providerField("judge_provider_id", "judge"),
    number("decision_temperature", "0.3"),
    number("decision_timeout_sec", "21"),
    field("decision_prompt_template", "prompt", { configTransform: "trim" }),
    number("decision_history_min_messages", "6"),
    number("message_delay_sec", "61"),
    number("min_silence_sec", "46"),
    number("cooldown_sec", "901"),
    checkbox("vision_judge_enabled", true),
    checkbox("vision_main_enabled", true),
    checkbox("vision_skip_stickers", true),
    providerField("vision_provider_id", "vision"),
    providerField("vision_judge_provider_id", "visionJudge"),
    number("vision_max_images", "3"),
    number("vision_image_age_sec", "301"),
    number("vision_timeout_sec", "22"),
    checkbox("proactive_inherit_tools", true),
    field("whitelist_sessions", "group:a", { configTransform: "whitelist" }),
  ];
  const form = {
    classList,
    inert: true,
    querySelector: () => null,
    querySelectorAll: () => fields,
  };
  const elements = {
    configForm: form,
    whitelistInput: fields.at(-1),
    whitelistCount: { textContent: "" },
    decisionPromptInput: { dataset: {}, value: "prompt" },
    selfStatus: { textContent: "" },
    decisionModelStatus: { textContent: "" },
    configSaveState: { textContent: "", classList },
  };
  const state = {
    configLoaded: true,
    savingConfig: false,
    configRevision: TEST_REVISION,
    isDirty: true,
    requiresConfigRefresh: false,
  };
  const provider = (value) => ({ value: () => value, sync() {} });
  const controls = {
    judge: provider("judge"),
    vision: provider("vision"),
    visionJudge: provider("vision-judge"),
  };
  const savedKeys = Object.fromEntries(configSaveKeys(form).map((key) => [key, true]));
  const io = createConfigIo({
    getEls: () => elements,
    getState: () => state,
    setState: (updates) => Object.assign(state, updates),
    apiGet: async () => {
      throw new Error("skip refresh");
    },
    apiPost: async () => ({
      ok: true,
      config: {
        ...savedKeys,
        ok: true,
        enabled: true,
        whitelist_sessions: ["group:a"],
        decision_prompt_default: "prompt",
        config_revision: TEST_REVISION,
      },
      config_revision: TEST_REVISION,
      runtime_enabled: true,
      adjusted_fields: [],
    }),
    showToast() {},
    setStatState() {},
    renderPromptPreview() {},
    judgeProviderControl: controls.judge,
    visionProviderControl: controls.vision,
    visionJudgeProviderControl: controls.visionJudge,
    fmtBool: String,
  });

  await io.saveConfig({ preventDefault() {} });

  assert.equal(state.configLoaded, true);
  assert.equal(state.isDirty, false);
  assert.equal(state.requiresConfigRefresh, false);
  assert.equal(state.configRevision, TEST_REVISION);
  assert.equal(elements.configSaveState.textContent, "已保存");
  assert.equal(form.inert, false);
});

test("empty number fields reuse last loaded values", () => {
  const field = {
    dataset: { configKey: "cooldown_sec" },
    type: "number",
    value: "",
  };
  const form = { querySelectorAll: () => [field] };
  const body = buildConfigSaveBody(form, {}, "", { cooldown_sec: 777 });
  assert.equal(body.cooldown_sec, 777);
});

test("whitelist format collapses a group UMO onto its bare group id", () => {
  const raw = [
    "1076958977",
    "1568455",
    "272372284",
    "阿c:FriendMessage:2381289480",
    "阿c:GroupMessage:272372284",
  ].join("\n");
  assert.deepEqual(uniqueWhitelistItems(raw), [
    "1076958977",
    "1568455",
    "272372284",
    "阿c:FriendMessage:2381289480",
  ]);
  assert.equal(
    summarizeWhitelist(raw),
    "已识别 4 个有效会话（3 个纯群号，1 个完整 UMO） · 存在 1 处重复",
  );
});

test("validateWhitelistLines reports line numbers and reasons for malformed whitelist input", () => {
  const input = [
    "valid_session_1",
    'illegal_session_"with_quotes"',
    "valid_session_2",
    "x".repeat(205),
  ].join("\n");
  const errors = validateWhitelistLines(input);
  assert.equal(errors.length, 2);
  assert.equal(errors[0].line, 2);
  assert.ok(errors[0].reason.includes("非法字符"));
  assert.equal(errors[1].line, 4);
  assert.ok(errors[1].reason.includes("字符上限"));
});

test("prompt preview keeps unknown variables verbatim", () => {
  const out = renderPromptTemplateHtml("hi {foo} {latest_message}");
  assert.ok(out.includes("{foo}"));
  assert.ok(!out.includes("undefined"));
});

test("settings page JS sources keep lines under the maintainability cap", async () => {
  const names = [
    "app.js",
    "chrome.mjs",
    "config-form.mjs",
    "config-io.mjs",
    "frontend-core.mjs",
    "providers.mjs",
    "theme.mjs",
  ];
  for (const name of names) {
    const text = await readFile(join(pageDir, name), "utf8");
    const lines = text.split(/\r?\n/);
    for (let i = 0; i < lines.length; i += 1) {
      assert.ok(
        lines[i].length <= MAX_SOURCE_LINE,
        `${name}:${i + 1} length ${lines[i].length} > ${MAX_SOURCE_LINE}`
      );
    }
  }
});
