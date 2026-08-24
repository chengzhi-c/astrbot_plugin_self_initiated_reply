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
  summarizeWhitelist,
  uniqueWhitelistItems,
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
});

test("dark accent tokens are declared once and reused", async () => {
  const css = await readFile(join(pageDir, "style.css"), "utf8");
  assert.equal((css.match(/#e0a040/g) || []).length, 1);
  assert.match(css, /--accent:\s*light-dark\(/);
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
