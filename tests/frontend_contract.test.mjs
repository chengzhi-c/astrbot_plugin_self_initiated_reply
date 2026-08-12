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

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const pageDir = join(root, "pages", "主动回复设置");

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
  const [html, app, css] = await Promise.all([
    readFile(join(pageDir, "index.html"), "utf8"),
    readFile(join(pageDir, "app.js"), "utf8"),
    readFile(join(pageDir, "style.css"), "utf8"),
  ]);
  assert.match(html, /<main\b[^>]*class="layout"/);
  assert.match(html, /id="whitelistInput"[^>]*aria-describedby="whitelistError"/);
  assert.match(app, /setAttribute\("aria-describedby",/);
  assert.match(app, /setAttribute\("aria-current", "location"\)/);
  assert.match(app, /requestPluginApi\(/);
  assert.match(app, /createProviderControl\(/);
  assert.match(
    await readFile(join(pageDir, "providers.mjs"), "utf8"),
    /providerNeedsManualInput\(next, getOptions\(\), isListAvailable\(\)\)/
  );
  assert.match(
    app,
    /event\.key !== "Escape" \|\| !media\.matches \|\| els\.moreActionsMenu\.hidden/
  );
  assert.match(html, /id="moreActionsBtn"[^>]*aria-controls="moreActionsMenu"[^>]*aria-expanded="false"/);
  assert.match(html, /id="judgeProviderInput"[^>]*hidden/);
  assert.match(html, /超时则本次不主动回应/);
  assert.doesNotMatch(html, /超时按默认放行/);
  assert.match(app, /isSuccessfulConfigPayload\(/);
  assert.match(app, /btn\.disabled = loading \|\| !configLoaded/);
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
  assert.match(workflow, /^  frontend:\n/m);
  assert.match(workflow, /actions\/setup-node@v4/);
  assert.match(workflow, /node --check "pages\/主动回复设置\/app\.js"/);
  assert.match(workflow, /node --test tests\/frontend_contract\.test\.mjs/);
});
