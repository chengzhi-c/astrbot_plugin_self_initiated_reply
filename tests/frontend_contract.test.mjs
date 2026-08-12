import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  isSuccessfulConfigPayload,
  normalizeApiError,
  providerNeedsManualInput,
  requestPluginApi,
} from "../pages/主动回复设置/frontend-core.mjs";
import {
  CONFIG_SAVE_KEYS,
  createConfigIo,
} from "../pages/主动回复设置/config-io.mjs";
import { THEME_KEY } from "../pages/主动回复设置/theme.mjs";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const pageDir = join(root, "pages", "主动回复设置");
const MAX_SOURCE_LINE = 200;

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
    }),
    false
  );
  assert.equal(
    isSuccessfulConfigPayload({
      ok: true,
      enabled: false,
      whitelist_sessions: [],
    }),
    true
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
  assert.match(html, /id="whitelistInput"[^>]*aria-describedby="whitelistError"/);
  assert.match(fe, /setAttribute\("aria-describedby",/);
  assert.match(fe, /setAttribute\("aria-current", "location"\)/);
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
  assert.match(fe, /btn\.disabled = loading \|\| !configLoaded/);
  assert.match(css, /@media \(max-width: 360px\)/);
  assert.match(css, /more-actions-menu\[hidden\]/);
});

test("context-history setting describes its fallback behavior", async () => {
  const html = await readFile(join(pageDir, "index.html"), "utf8");

  assert.match(html, /判断时参考的聊天记录/);
  assert.match(html, /少于多少条时补读历史/);
  assert.match(
    html,
    /本插件记录的文字消息少于此数时，会尝试读取同一会话的旧消息，帮助判断要不要接话。设为 0 时，只看插件已记录的消息。/
  );
  assert.doesNotMatch(html, /上下文至少几条消息才判断接话/);
});

test("CI runs the dependency-free frontend gate", async () => {
  const workflow = await readFile(join(root, ".github", "workflows", "ci.yml"), "utf8");
  assert.match(workflow, /^  frontend:\r?\n/m);
  assert.match(workflow, /actions\/setup-node@v4/);
  // 覆盖全部设置页 JS/MJS，而不是只检查入口两文件。
  assert.match(workflow, /pages\/主动回复设置\/\*\.\{js,mjs\}/);
  assert.match(workflow, /node --check/);
  assert.match(workflow, /node --test tests\/frontend_contract\.test\.mjs/);
});

test("theme localStorage key stays single-sourced with the HTML bootstrap", async () => {
  const html = await readFile(join(pageDir, "index.html"), "utf8");
  assert.equal(THEME_KEY, "selfreply-theme");
  assert.match(html, new RegExp(`localStorage\\.getItem\\("${THEME_KEY}"\\)`));
  assert.equal((html.match(/selfreply-theme/g) || []).length, 1);
});

test("config save path posts exactly the declared writable keys", async () => {
  assert.deepEqual([...CONFIG_SAVE_KEYS].sort(), [
    "cooldown_sec",
    "decision_history_min_messages",
    "decision_model_enabled",
    "decision_prompt_template",
    "decision_temperature",
    "decision_timeout_sec",
    "enabled",
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
  ]);

  const field = (value = "") => ({ value });
  const checkbox = (checked = false) => ({ checked });
  const classList = { add() {}, remove() {}, toggle() {} };
  const form = { classList, inert: false, querySelector: () => null };
  const elements = {
    configForm: form,
    enabledInput: checkbox(true),
    decisionModelInput: checkbox(true),
    decisionTempInput: field("0.3"),
    decisionTimeoutInput: field("21"),
    decisionPromptInput: field("  prompt  "),
    minContextInput: field("6"),
    messageDelayInput: field("61"),
    minSilenceInput: field("46"),
    cooldownInput: field("901"),
    visionJudgeEnabledInput: checkbox(true),
    visionMainEnabledInput: checkbox(true),
    visionSkipStickersInput: checkbox(true),
    visionMaxImagesInput: field("3"),
    visionImageAgeInput: field("301"),
    visionTimeoutInput: field("22"),
    proactiveInheritToolsInput: checkbox(true),
    whitelistInput: field("group:a\ngroup:b"),
    whitelistCount: { textContent: "" },
  };
  const state = { configLoaded: true, savingConfig: false };
  const posts = [];
  const provider = (value) => ({ value: () => value });
  const io = createConfigIo({
    getEls: () => elements,
    getState: () => state,
    setState: (updates) => Object.assign(state, updates),
    apiGet: async () => {
      throw new Error("skip refresh");
    },
    apiPost: async (endpoint, body) => {
      posts.push({ endpoint, body });
      return { ok: true };
    },
    showToast() {},
    setStatState() {},
    renderPromptPreview() {},
    judgeProviderControl: provider("judge"),
    visionProviderControl: provider("vision"),
    visionJudgeProviderControl: provider("vision-judge"),
    fmtBool: String,
  });

  await io.saveConfig({ preventDefault() {} });

  assert.equal(posts.length, 1);
  assert.equal(posts[0].endpoint, "config");
  assert.deepEqual(Object.keys(posts[0].body).sort(), [...CONFIG_SAVE_KEYS].sort());
  assert.deepEqual(posts[0].body.whitelist_sessions, ["group:a", "group:b"]);
  assert.equal(posts[0].body.decision_prompt_template, "prompt");
  assert.equal(state.savingConfig, false);
  assert.equal(form.inert, false);
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
