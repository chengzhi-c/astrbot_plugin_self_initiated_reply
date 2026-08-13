import { test, expect } from "@playwright/test";
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, resolve, sep } from "node:path";

const ROOT = resolve(import.meta.dirname, "..");
const PAGE_PATH = "/pages/%E4%B8%BB%E5%8A%A8%E5%9B%9E%E5%A4%8D%E8%AE%BE%E7%BD%AE/index.html";
const MIME = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".png": "image/png",
};

let server;
let baseUrl;
let activeScenario = "";

function configPayload(overrides = {}) {
  return {
    ok: true,
    enabled: true,
    runtime_enabled: true,
    whitelist_sessions: ["123456"],
    decision_model_enabled: true,
    decision_prompt_template: "请根据 {latest_message} 判断是否回复",
    decision_prompt_default: "请根据 {latest_message} 判断是否回复",
    ...overrides,
  };
}

function apiScenario(request) {
  return activeScenario;
}

function respondJson(response, value, status = 200) {
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(value));
}

async function serveStatic(request, response) {
  const url = new URL(request.url, baseUrl);
  if (url.pathname.endsWith("/index.html")) {
    activeScenario = url.searchParams.get("scenario") || "";
  }
  if (url.pathname === "/api/plugin/page/bridge-sdk.js") {
    response.writeHead(200, { "Content-Type": MIME[".js"] });
    response.end("");
    return;
  }
  if (url.pathname.startsWith("/api/plug/")) {
    const scenario = apiScenario(request);
    const endpoint = url.pathname.split("/").slice(4).join("/");
    if (endpoint === "config" && scenario === "fetch-pending") return;
    if (endpoint === "config" && scenario === "bad-json") {
      response.writeHead(200, { "Content-Type": "text/plain; charset=utf-8" });
      response.end("not-json");
      return;
    }
    if (endpoint === "providers") {
      respondJson(response, { ok: true, providers: [] });
      return;
    }
    if (endpoint === "unified/overview") {
      respondJson(response, { ok: true, self_reply: { whitelist_count: 0 } });
      return;
    }
    if (endpoint === "ui/theme") {
      respondJson(response, { ok: true, theme: "light" });
      return;
    }
    respondJson(response, endpoint === "config" ? configPayload() : { ok: true });
    return;
  }

  const decoded = decodeURIComponent(url.pathname === "/" ? PAGE_PATH : url.pathname);
  const filePath = resolve(ROOT, `.${decoded}`);
  if (filePath !== ROOT && !filePath.startsWith(`${ROOT}${sep}`)) {
    response.writeHead(403).end();
    return;
  }
  try {
    const fileStat = await stat(filePath);
    if (!fileStat.isFile()) throw new Error("not a file");
    response.writeHead(200, { "Content-Type": MIME[extname(filePath)] || "application/octet-stream" });
    createReadStream(filePath).pipe(response);
  } catch {
    response.writeHead(404).end();
  }
}

async function installBridge(page, options = {}) {
  await page.addInitScript(
    ({ config, providersFail, saveMode, theme, overviewFail }) => {
      const state = { saveMode, saveAttempts: 0, config };
      window.__bridgeCalls = [];
      window.__bridgeState = state;
      window.AstrBotPluginPage = {
        ready: async () => true,
        apiGet: async (endpoint) => {
          window.__bridgeCalls.push({ method: "GET", endpoint });
          if (endpoint === "providers") {
            if (providersFail) throw new Error("provider list unavailable");
            return { ok: true, providers: [{ id: "provider-a", label: "Provider A" }] };
          }
          if (endpoint === "config") return state.config;
          if (endpoint === "unified/overview") {
            if (overviewFail) throw new Error("overview unavailable");
            return { ok: true, self_reply: { whitelist_count: state.config.whitelist_sessions.length } };
          }
          if (endpoint === "ui/theme") return { ok: true, theme };
          return { ok: true };
        },
        apiPost: async (endpoint, body) => {
          window.__bridgeCalls.push({ method: "POST", endpoint, body });
          if (endpoint === "config") {
            state.saveAttempts += 1;
            if (state.saveMode === "pending" && state.saveAttempts === 1) {
              return new Promise(() => {});
            }
            if (state.saveMode === "fail-once" && state.saveAttempts === 1) {
              return { ok: false, error: "write failed" };
            }
            state.config = { ...state.config, ...body, runtime_enabled: true, ok: true };
            return { ok: true };
          }
          return { ok: true, theme: body?.theme || "auto", removed: 0 };
        },
      };
    },
    {
      config: configPayload(options.config),
      providersFail: Boolean(options.providersFail),
      saveMode: options.saveMode || "success",
      theme: options.theme || "light",
      overviewFail: Boolean(options.overviewFail),
    }
  );
}

async function openPage(page, query = "") {
  const errors = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });
  await page.goto(`${baseUrl}${PAGE_PATH}${query}`);
  await expect(page.locator("#boot")).toHaveClass(/is-hidden/);
  return errors;
}

async function expectNoHorizontalOverflow(page) {
  const widths = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(widths.scroll).toBeLessThanOrEqual(widths.client);
}

test.beforeAll(async () => {
  server = createServer((request, response) => {
    serveStatic(request, response).catch(() => response.writeHead(500).end());
  });
  await new Promise((resolveListen) => server.listen(0, "127.0.0.1", resolveListen));
  const address = server.address();
  baseUrl = `http://127.0.0.1:${address.port}`;
});

test.afterAll(async () => {
  await new Promise((resolveClose, reject) =>
    server.close((error) => (error ? reject(error) : resolveClose()))
  );
});

for (const variant of [
  { name: "desktop light", viewport: { width: 1440, height: 1000 }, theme: "light" },
  { name: "desktop dark", viewport: { width: 1440, height: 1000 }, theme: "dark" },
  { name: "mobile light", viewport: { width: 360, height: 800 }, theme: "light" },
  { name: "mobile dark", viewport: { width: 360, height: 800 }, theme: "dark" },
]) {
  test(`${variant.name} loads without overflow or fixed-bar overlap`, async ({ page }) => {
    await page.setViewportSize(variant.viewport);
    await installBridge(page, { theme: variant.theme });
    const errors = await openPage(page);
    await expect(page.locator("html")).toHaveAttribute("data-theme", variant.theme);
    await expect(page.locator("#configForm")).not.toHaveAttribute("inert", "");
    await expectNoHorizontalOverflow(page);
    if (variant.viewport.width <= 720) {
      const bars = await page.evaluate(() => {
        const tabs = document.querySelector("#mobileTabbar").getBoundingClientRect();
        const save = document.querySelector("#mobileSaveBar").getBoundingClientRect();
        return { tabsBottom: tabs.bottom, saveTop: save.top };
      });
      expect(bars.tabsBottom).toBeLessThanOrEqual(bars.saveTop + 1);
    }
    expect(errors).toEqual([]);
  });
}

test("pending save restores the form and a second save succeeds", async ({ page }) => {
  await installBridge(page, { saveMode: "pending" });
  const errors = await openPage(page);
  await page.evaluate(() => {
    const nativeSetTimeout = window.setTimeout.bind(window);
    window.setTimeout = (callback, delay, ...args) =>
      nativeSetTimeout(callback, delay === 15000 ? 30 : delay, ...args);
  });
  await page.locator("#messageDelayInput").fill("75");
  await page.locator("#saveTopBtn").click();
  await expect(page.locator("#configSaveState")).toHaveText("保存失败");
  await expect(page.locator("#configForm")).not.toHaveAttribute("inert", "");
  await expect(page.locator("#saveTopBtn")).toBeEnabled();

  await page.locator("#saveTopBtn").click();
  await expect(page.locator("#configSaveState")).toHaveText("已保存");
  const posts = await page.evaluate(() =>
    window.__bridgeCalls.filter((call) => call.method === "POST" && call.endpoint === "config")
  );
  expect(posts).toHaveLength(2);
  expect(posts[1].body.message_delay_sec).toBe(75);
  expect(errors).toEqual([]);
});

test("overview failure does not block a successful config load", async ({ page }) => {
  await installBridge(page, { overviewFail: true });
  const errors = await openPage(page);
  await expect(page.locator("#configForm")).toBeVisible();
  await expect(page.locator("#configForm")).not.toHaveAttribute("inert", "");
  await expect(page.locator("#enabledInput")).toBeChecked();
  await expect(page.locator("#boot")).toHaveClass(/is-hidden/);
  expect(errors).toEqual([]);
});

test("provider failure enables manual input for all provider controls", async ({ page }) => {
  await installBridge(page, { providersFail: true });
  const errors = await openPage(page);
  for (const selector of [
    "#judgeProviderInput",
    "#visionProviderInput",
    "#visionJudgeProviderInput",
  ]) {
    await page.locator(selector).evaluate((element) => {
      const details = element.closest("details");
      if (details) details.open = true;
    });
    await expect(page.locator(selector)).toBeVisible();
  }
  await expect(page.locator("#providerListState")).toContainText("三个 Provider 均可手动填写");
  expect(errors).toEqual([]);
});

test("prompt preview escapes HTML and theme choice persists", async ({ page }) => {
  await installBridge(page, { theme: "light" });
  const errors = await openPage(page);
  await page.locator("#decisionPromptInput").fill('<img src=x onerror="window.__xss=1"> {latest_message}');
  await expect(page.locator("#promptPreview img")).toHaveCount(0);
  await expect(page.locator("#promptPreview")).toContainText("<img src=x");
  expect(await page.evaluate(() => window.__xss)).toBeUndefined();

  await page.locator("#themeToggle").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect(await page.evaluate(() => localStorage.getItem("selfreply-theme"))).toBe("dark");
  expect(errors).toEqual([]);
});

test("dimming places a visible non-interactive overlay above the page", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page);
  const errors = await openPage(page);
  await expect(page.locator("#dimBtn")).toBeVisible();
  await page.locator("#dimBtn").click();
  await expect(page.locator("html")).toHaveClass(/dimmed/);
  await expect.poll(() => page.evaluate(() =>
    getComputedStyle(document.documentElement, "::after").backgroundColor)).toBe("rgba(0, 0, 0, 0.18)");
  const overlay = await page.evaluate(() => {
    const style = getComputedStyle(document.documentElement, "::after");
    return { pointerEvents: style.pointerEvents, zIndex: style.zIndex };
  });
  expect(overlay.pointerEvents).toBe("none");
  expect(Number(overlay.zIndex)).toBeGreaterThan(90);
  expect(await page.evaluate(() => localStorage.getItem("selfreply-dim"))).toBe("1");
  await page.locator("#dimBtn").click();
  await expect(page.locator("html")).not.toHaveClass(/dimmed/);
  await expect.poll(() => page.evaluate(() =>
    getComputedStyle(document.documentElement, "::after").backgroundColor)).toBe("rgba(0, 0, 0, 0)");
  expect(errors).toEqual([]);
});

test("compact more-actions menu exposes auxiliary controls", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await installBridge(page);
  const errors = await openPage(page);
  await expect(page.locator("#moreActionsMenu")).toBeHidden();
  await expect(page.locator("#moreActionsBtn")).toHaveAttribute("title", "更多操作");
  await page.locator("#moreActionsBtn").click();
  await expect(page.locator("#moreActionsBtn")).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator("#dimBtn")).toBeVisible();
  await expect(page.locator("#boldBtn")).toBeVisible();
  await expect(page.locator("#refreshBtn")).toBeVisible();
  expect(errors).toEqual([]);
});

test("fetch non-JSON and pending responses both leave a recoverable page", async ({ page }) => {
  let errors = await openPage(page, "?scenario=bad-json");
  await expect(page.locator("#toast")).toContainText("响应不是有效 JSON");
  await expect(page.locator("#saveTopBtn")).toBeDisabled();
  expect(errors).toEqual([]);

  await page.addInitScript(() => {
    const nativeSetTimeout = window.setTimeout.bind(window);
    window.setTimeout = (callback, delay, ...args) =>
      nativeSetTimeout(callback, delay === 15000 ? 30 : delay, ...args);
  });
  await page.goto(`${baseUrl}${PAGE_PATH}?scenario=fetch-pending`);
  await expect(page.locator("#boot")).toHaveClass(/is-hidden/);
  await expect(page.locator("#toast")).toContainText("请求超时");
  await expect(page.locator("#saveTopBtn")).toBeDisabled();
  errors = errors.filter(Boolean);
  expect(errors).toEqual([]);
});
