import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
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
  assert.match(app, /providerNeedsManualInput\(next, providerOptions, providerListAvailable\)/);
  assert.match(
    app,
    /event\.key !== "Escape" \|\| !media\.matches \|\| els\.moreActionsMenu\.hidden/
  );
  assert.match(html, /id="moreActionsBtn"[^>]*aria-controls="moreActionsMenu"[^>]*aria-expanded="false"/);
  assert.match(html, /id="judgeProviderInput"[^>]*hidden/);
  assert.match(css, /@media \(max-width: 360px\)/);
  assert.match(css, /more-actions-menu\[hidden\]/);
});

test("CI runs the dependency-free frontend gate", async () => {
  const workflow = await readFile(join(root, ".github", "workflows", "ci.yml"), "utf8");
  assert.match(workflow, /^  frontend:\n/m);
  assert.match(workflow, /actions\/setup-node@v4/);
  assert.match(workflow, /node --check "pages\/主动回复设置\/app\.js"/);
  assert.match(workflow, /node --test tests\/frontend_contract\.test\.mjs/);
});
