import { test, expect } from "@playwright/test";
import { FETCH_TIMEOUT_MS } from "../pages/主动回复设置/frontend-core.mjs";
import { configPayload } from "./fixtures/config-payload.mjs";
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
    ({ config, providersFail, saveMode, theme, dim, bold, refreshConfigPending }) => {
      const state = {
        saveMode,
        saveAttempts: 0,
        config,
        configCalls: 0,
        refreshConfigPending,
      };
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
          if (endpoint === "config") {
            state.configCalls += 1;
            if (state.refreshConfigPending && state.configCalls > 1) {
              return new Promise((resolve) => {
                window.__resolveRefreshConfig = () => resolve(state.config);
              });
            }
            return state.config;
          }
          if (endpoint === "ui/theme") return { ok: true, theme, dim, bold };
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
            state.config = {
              ...state.config,
              ...body,
              runtime_enabled: true,
              ok: true,
              config_revision: `sha256:${"c".repeat(64)}`,
            };
            return {
              ok: true,
              ...state.config,
              adjusted_fields: state.saveMode === "adjusted" ? ["whitelist_sessions"] : [],
            };
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
      dim: Boolean(options.dim),
      bold: Boolean(options.bold),
      refreshConfigPending: Boolean(options.refreshConfigPending),
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
  await page.evaluate((timeoutMs) => {
    const nativeSetTimeout = window.setTimeout.bind(window);
    window.setTimeout = (callback, delay, ...args) =>
      nativeSetTimeout(callback, delay === timeoutMs ? 30 : delay, ...args);
  }, FETCH_TIMEOUT_MS);
  await page.locator("#messageDelayInput").fill("75");
  await page.locator("#saveTopBtn").click();
  await expect(page.locator("#toast")).toContainText("保存状态未知");
  await expect(page.locator("#configForm")).not.toHaveAttribute("inert", "");
  await expect(page.locator("#saveTopBtn")).toBeDisabled();

  await page.waitForTimeout(80);
  const posts = await page.evaluate(() =>
    window.__bridgeCalls.filter((call) => call.method === "POST" && call.endpoint === "config")
  );
  expect(posts).toHaveLength(1);
  expect(errors).toEqual([]);
});

test("every checkbox renders the backend bool value", async ({ page }) => {
  // 取代已删除的 data-config-default 机制：那个属性只在"配置缺键"时才与
  // Boolean() 不同，而缺键会被 requiredKeys 校验提前拦死，故它永不生效。
  // 真正要守的是渲染本身——后端 false 却显示勾选，用户会以为功能已开。
  const overrides = {
    enabled: true,
    enabled_private_sessions: false,
    abandon_stale_on_new_message: true,
    decision_model_enabled: false,
    proactive_inherit_tools: true,
    vision_judge_enabled: false,
    vision_main_enabled: true,
    vision_skip_stickers: false,
  };
  await installBridge(page, { config: overrides });
  await openPage(page);
  for (const [id, key] of Object.entries({
    enabledInput: "enabled",
    enabledPrivateSessionsInput: "enabled_private_sessions",
    abandonStaleOnNewMessageInput: "abandon_stale_on_new_message",
    decisionModelInput: "decision_model_enabled",
    proactiveInheritToolsInput: "proactive_inherit_tools",
    visionJudgeEnabledInput: "vision_judge_enabled",
    visionMainEnabledInput: "vision_main_enabled",
    visionSkipStickersInput: "vision_skip_stickers",
  })) {
    // 显式断言双向：只测 true 会让"恒为勾选"的缺陷溜过。
    if (overrides[key]) {
      await expect(page.locator(`#${id}`), `${key} 应为勾选`).toBeChecked();
    } else {
      await expect(page.locator(`#${id}`), `${key} 应为未勾选`).not.toBeChecked();
    }
  }
});

test("late refresh config does not overwrite a dirty form", async ({ page }) => {
  await installBridge(page, { refreshConfigPending: true });
  const errors = await openPage(page);
  await page.locator("#refreshBtn").click();
  await expect.poll(() => page.evaluate(() => typeof window.__resolveRefreshConfig)).toBe("function");
  await page.locator("#messageDelayInput").fill("75");
  await page.evaluate(() => window.__resolveRefreshConfig());
  await page.waitForTimeout(80);
  await expect(page.locator("#messageDelayInput")).toHaveValue("75");
  await expect(page.locator("#navSaveState")).toContainText("有未保存改动");
  expect(errors).toEqual([]);
});

test("forced refresh also preserves edits made after the request starts", async ({ page }) => {
  await installBridge(page, { refreshConfigPending: true });
  const errors = await openPage(page);
  await page.locator("#messageDelayInput").fill("75");
  await page.locator("#refreshBtn").click();
  await page.locator("#refreshBtn").click();
  await expect.poll(() => page.evaluate(() => typeof window.__resolveRefreshConfig)).toBe("function");
  await page.locator("#messageDelayInput").fill("80");
  await page.evaluate(() => window.__resolveRefreshConfig());
  await page.waitForTimeout(80);
  await expect(page.locator("#messageDelayInput")).toHaveValue("80");
  expect(errors).toEqual([]);
});

test("adjusted fields are surfaced with a field label", async ({ page }) => {
  await installBridge(page, { saveMode: "adjusted" });
  const errors = await openPage(page);
  await page.locator("#whitelistInput").fill("a\na");
  await page.locator("#saveTopBtn").click();
  await expect(page.locator("#toast")).toContainText("白名单");
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
  await expect.poll(() =>
    page.evaluate(() => window.__bridgeCalls.find((call) => call.method === "POST" && call.endpoint === "ui/theme")?.body)
  ).toMatchObject({ dim: true });
  await page.locator("#dimBtn").click();
  await expect(page.locator("html")).not.toHaveClass(/dimmed/);
  await expect.poll(() => page.evaluate(() =>
    getComputedStyle(document.documentElement, "::after").backgroundColor)).toBe("rgba(0, 0, 0, 0)");
  expect(errors).toEqual([]);
});

test("dimming and bold restore from ui prefs", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { dim: true, bold: true });
  const errors = await openPage(page);
  await expect(page.locator("html")).toHaveClass(/dimmed/);
  await expect(page.locator("html")).toHaveClass(/bold-text/);
  await expect(page.locator("#dimBtn")).toHaveClass(/active/);
  await expect(page.locator("#boldBtn")).toHaveClass(/active/);
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

test("desktop more-actions trigger stays collapsed-attribute free", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page);
  const errors = await openPage(page);
  await expect(page.locator("#moreActionsBtn")).not.toHaveAttribute("aria-expanded");
  await expect(page.locator("#moreActionsMenu")).toBeVisible();
  expect(errors).toEqual([]);
});

test("module load failure surfaces a refresh hint", async ({ page }) => {
  await page.route("**/app.js", (route) => route.abort());
  await page.clock.install();
  await page.goto(`${baseUrl}${PAGE_PATH}`);
  await page.clock.runFor(8000);
  await expect(page.locator("body")).toHaveClass(/is-ready/);
  await expect(page.locator(".boot-text")).toHaveText("脚本加载失败，请刷新");
});

test("fetch non-JSON and pending responses both leave a recoverable page", async ({ page }) => {
  let errors = await openPage(page, "?scenario=bad-json");
  await expect(page.locator("#toast")).toContainText("响应不是有效 JSON");
  await expect(page.locator("#saveTopBtn")).toBeDisabled();
  expect(errors).toEqual([]);

  await page.addInitScript((timeoutMs) => {
    const nativeSetTimeout = window.setTimeout.bind(window);
    window.setTimeout = (callback, delay, ...args) =>
      nativeSetTimeout(callback, delay === timeoutMs ? 30 : delay, ...args);
  }, FETCH_TIMEOUT_MS);
  await page.goto(`${baseUrl}${PAGE_PATH}?scenario=fetch-pending`);
  await expect(page.locator("#boot")).toHaveClass(/is-hidden/);
  await expect(page.locator("#toast")).toContainText("请求超时");
  await expect(page.locator("#saveTopBtn")).toBeDisabled();
  errors = errors.filter(Boolean);
  expect(errors).toEqual([]);
});

test("skip link and invalid whitelist stay keyboard-accessible", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page);
  const errors = await openPage(page);
  await page.keyboard.press("Tab");
  await expect(page.locator(".skip-link")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#selfStat")).toBeInViewport();

  await page.locator("#whitelistInput").fill('bad"quote');
  await page.locator("#saveTopBtn").click();
  await expect(page.locator("#whitelistError")).toBeVisible();
  await expect(page.locator("#whitelistInput")).toHaveAttribute("aria-invalid", "true");
  await expect(page.locator("#whitelistInput")).toHaveAttribute("aria-describedby", "whitelistError");
  await expect(page.locator("#whitelistInput")).toBeFocused();
  expect(errors).toEqual([]);
});
