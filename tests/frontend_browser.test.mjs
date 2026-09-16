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
    ({ config, providersFail, saveMode, theme, dim, bold, refreshConfigPending, themePending, cleanupRemoved, cleanupFail }) => {
      const state = {
        saveMode,
        saveAttempts: 0,
        config,
        configCalls: 0,
        refreshConfigPending,
        themePending,
        cleanupRemoved,
        cleanupFail,
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
          if (endpoint === "ui/theme") {
            if (state.themePending) {
              return new Promise((resolve) => {
                window.__resolveTheme = () => resolve({ ok: true, theme, dim, bold });
              });
            }
            return { ok: true, theme, dim, bold };
          }
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
            if (state.saveMode === "field-error") {
              // 模拟后端 _strict_int 的字段级拒绝：error 文案以「键名 + 空格」
              // 前缀自带定位，驱动 saveConfig 的 numberField 标红路径。
              return { ok: false, error: "cooldown_sec 必须是整数" };
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
          if (endpoint === "image-cache/cleanup") {
            // 失败场景走后端 {"ok": false, "error": ...} 形状：前端靠 ok !== true
            // 抛错，不是靠 HTTP 状态码。
            if (state.cleanupFail) return { ok: false, error: "磁盘只读" };
            return { ok: true, removed: state.cleanupRemoved };
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
      themePending: Boolean(options.themePending),
      cleanupRemoved: options.cleanupRemoved ?? 0,
      cleanupFail: Boolean(options.cleanupFail),
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

test("late theme prefs do not overwrite a dim click already made", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { themePending: true, dim: false, bold: false });
  const errors = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });
  await page.goto(`${baseUrl}${PAGE_PATH}`);
  await expect(page.locator("#boot")).toHaveClass(/is-hidden/);
  await expect.poll(() => page.evaluate(() => typeof window.__resolveTheme)).toBe("function");
  await page.locator("#dimBtn").click();
  await expect(page.locator("html")).toHaveClass(/dimmed/);
  await page.evaluate(() => window.__resolveTheme());
  await page.waitForTimeout(80);
  await expect(page.locator("html")).toHaveClass(/dimmed/);
  await expect(page.locator("#dimBtn")).toHaveClass(/active/);
  expect(errors).toEqual([]);
});

test("late theme prefs do not overwrite a theme click already made", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { themePending: true, theme: "dark", dim: false, bold: false });
  const errors = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });
  await page.goto(`${baseUrl}${PAGE_PATH}`);
  await expect(page.locator("#boot")).toHaveClass(/is-hidden/);
  await expect.poll(() => page.evaluate(() => typeof window.__resolveTheme)).toBe("function");
  await page.locator("#themeToggle").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.evaluate(() => window.__resolveTheme());
  await page.waitForTimeout(80);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  expect(await page.evaluate(() => localStorage.getItem("selfreply-theme"))).toBe("light");
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

  // 菜单项必须是**横向单行项**（占满菜单宽度、文字标签在旁），不是 44px 图标方块。
  //
  // 这条断言来自一次真实的层叠事故：这三个按钮的样式锚曾是 #id（特异性 110），
  // 压过了 `.more-actions-menu .btn`（20），于是窄屏菜单里它们被钉成
  // `width: var(--tap)` + `padding: 0`，文字被挤成竖排（实测 44px 方块 vs
  // 本应 118px 单行项）。ID 改类后特异性降到 10，菜单规则才生效。
  // 断言取"宽 > 高 × 1.5"而非具体像素：尺寸随 --tap 令牌变，而"是不是横向项"
  // 是设计契约。
  for (const id of ["#dimBtn", "#boldBtn", "#refreshBtn"]) {
    const box = await page.locator(id).boundingBox();
    expect(
      box.width,
      `${id} 在窄屏菜单里不是横向菜单项（${box.width}×${box.height}）：` +
        "样式锚的特异性是否又压过了 .more-actions-menu .btn？",
    ).toBeGreaterThan(box.height * 1.5);
  }
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
  // 仅 toBeInViewport 抓不住"焦点没落过去"：落点若无 tabindex="-1"，
  // 浏览器不会聚焦它，键盘用户的后续 Tab 仍从文档开头（顶栏）重新开始，
  // 跳过链接等于只滚动不跳过。这里钉住焦点本身。
  await expect(page.locator("#selfStat")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.locator("#selfStat")).not.toBeFocused();

  await page.locator("#whitelistInput").fill('bad"quote');
  await page.locator("#saveTopBtn").click();
  await expect(page.locator("#whitelistError")).toBeVisible();
  await expect(page.locator("#whitelistInput")).toHaveAttribute("aria-invalid", "true");
  await expect(page.locator("#whitelistInput")).toHaveAttribute("aria-describedby", "whitelistError");
  await expect(page.locator("#whitelistInput")).toBeFocused();
  expect(errors).toEqual([]);
});

test("integer controls reject fractional input before save", async ({ page }) => {
  await installBridge(page);
  const errors = await openPage(page);
  await page.evaluate(() =>
    document.querySelector('[data-config-key="cooldown_sec"]').scrollIntoView());

  await page.locator("#cooldownInput").fill("90.5");
  await page.locator("#cooldownInput").blur();
  await expect(page.locator("#cooldownInputError")).toBeVisible();
  await expect(page.locator("#cooldownInputError")).toContainText("请输入整数");
  await expect(page.locator("#cooldownInput")).toHaveAttribute("aria-invalid", "true");

  // step=5 只是滑杆增量，不是整除约束（后端 _strict_int 接受任意整数）：
  // 47 必须保持合法，否则前端比后端更严，丧失既有功能。
  await page.locator("#messageDelayInput").fill("47");
  await page.locator("#messageDelayInput").blur();
  await expect(page.locator("#messageDelayInputError")).toBeHidden();

  await page.locator("#cooldownInput").fill("90");
  await expect(page.locator("#cooldownInputError")).toBeHidden();
  await page.locator("#saveTopBtn").click();
  await expect(page.locator("#configSaveState")).toHaveClass(/is-ok/);
  expect(errors).toEqual([]);
});

test("backend field errors paint the offending number control", async ({ page }) => {
  await installBridge(page, { saveMode: "field-error" });
  const errors = await openPage(page);
  await page.evaluate(() =>
    document.querySelector('[data-config-key="cooldown_sec"]').scrollIntoView());

  await page.locator("#cooldownInput").fill("90");
  await page.locator("#saveTopBtn").click();
  // 此前该定位只对白名单生效，其它字段用户只能靠 toast 猜是哪个框。
  await expect(page.locator("#cooldownInputError")).toBeVisible();
  await expect(page.locator("#cooldownInputError")).toContainText("cooldown_sec 必须是整数");
  await expect(page.locator("#cooldownInput")).toHaveAttribute("aria-invalid", "true");
  await expect(page.locator("#cooldownInput")).toBeFocused();
  expect(errors).toEqual([]);
});

test("reset prompt restores the server default and marks the form dirty", async ({ page }) => {
  const custom = "自定义判断提示词 {latest_message}";
  const fallback = "默认判断提示词 {latest_message}";
  await installBridge(page, {
    config: { decision_prompt_template: custom, decision_prompt_default: fallback },
  });
  const errors = await openPage(page);
  await expect(page.locator("#decisionPromptInput")).toHaveValue(custom);

  // 先保存一次清掉脏标记，才能把「点恢复后又变脏」当成新增事实断言。
  await page.locator("#saveTopBtn").click();
  await expect(page.locator("#navSaveState")).toHaveText("已保存");

  await page.locator("#resetPromptBtn").click();
  await expect(page.locator("#decisionPromptInput")).toHaveValue(fallback);
  await expect(page.locator("#promptPreview")).toContainText("默认判断提示词");
  await expect(page.locator("#toast")).toContainText("已恢复默认提示词");
  await expect(page.locator("#navSaveState")).toHaveText("有未保存改动");

  await page.locator("#saveTopBtn").click();
  await expect.poll(() =>
    page.evaluate(() => {
      const posts = window.__bridgeCalls.filter(
        (call) => call.method === "POST" && call.endpoint === "config",
      );
      return posts[posts.length - 1]?.body?.decision_prompt_template;
    }),
  ).toBe(fallback);
  expect(errors).toEqual([]);
});

test("mobile save bar submits the same body as the top save button", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await installBridge(page);
  const errors = await openPage(page);
  await expect(page.locator("#mobileSaveBar")).toBeVisible();
  await expect(page.locator("#mobileSaveBar")).not.toHaveClass(/is-dirty/);

  await page.locator("#decisionPromptInput").scrollIntoViewIfNeeded();
  await page.locator("#decisionPromptInput").fill("移动端改的提示词");
  await expect(page.locator("#mobileSaveBar")).toHaveClass(/is-dirty/);

  await page.locator("#saveMobileBtn").click();
  await expect(page.locator("#mobileSaveState")).toHaveText("已保存");
  const body = await page.evaluate(() => {
    const posts = window.__bridgeCalls.filter(
      (call) => call.method === "POST" && call.endpoint === "config",
    );
    return posts[posts.length - 1]?.body || null;
  });
  expect(body).not.toBeNull();
  expect(body.decision_prompt_template).toBe("移动端改的提示词");
  // 顶部与移动端按钮走同一个表单提交：CAS 基线随行发出，缺它会被后端拒为 STALE_WRITE。
  expect(body.base_revision).toBe(`sha256:${"b".repeat(64)}`);
  await expect(page.locator("#mobileSaveBar")).not.toHaveClass(/is-dirty/);
  expect(errors).toEqual([]);
});

test("image cache cleanup reports the count and surfaces failures", async ({ page }) => {
  await installBridge(page, { cleanupRemoved: 3 });
  const errors = await openPage(page);
  const button = page.locator("#cleanupImageCacheBtn");
  // 按钮在折叠的「识图高级设置」里：先展开再点（与 provider 控件同一手法）。
  await button.evaluate((element) => {
    const details = element.closest("details");
    if (details) details.open = true;
  });
  await button.click();
  await expect(page.locator("#cleanupImageCacheState")).toHaveText("已清理 3 个过期图片");
  await expect(page.locator("#toast")).toContainText("已清理 3 个过期图片");
  await expect(button).toBeEnabled();

  // 失败分支：后端返回 ok:false，必须报错而不是当成「没有需要清理的图片」。
  await page.evaluate(() => {
    window.__bridgeState.cleanupFail = true;
    window.__bridgeState.cleanupRemoved = 0;
  });
  await button.click();
  await expect(page.locator("#cleanupImageCacheState")).toHaveText("清理失败");
  await expect(page.locator("#toast")).toContainText("磁盘只读");
  await expect(button).toBeEnabled();
  expect(errors).toEqual([]);
});

test("mobile tab click moves both the tab and the sidenav current state", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await installBridge(page);
  const errors = await openPage(page);
  const decisionTab = page.locator('.mtab[data-target="sec-decision"]');
  const decisionLink = page.locator('.sidenav-link[data-target="sec-decision"]');

  await decisionTab.click();
  await expect(decisionTab).toHaveClass(/is-current/);
  await expect(decisionTab).toHaveAttribute("aria-current", "location");
  await expect(decisionLink).toHaveClass(/is-current/);
  await expect(decisionLink).toHaveAttribute("aria-current", "location");
  // 单一当前项：旧 tab 必须让位，否则 aria-current 会同时落在两个 tab 上。
  const currentTabs = await page.evaluate(() =>
    Array.from(document.querySelectorAll(".mtab"))
      .filter((tab) => tab.getAttribute("aria-current") === "location")
      .map((tab) => tab.dataset.target),
  );
  expect(currentTabs).toEqual(["sec-decision"]);
  expect(errors).toEqual([]);
});
