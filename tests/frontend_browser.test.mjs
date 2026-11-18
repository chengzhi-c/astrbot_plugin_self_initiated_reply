import { test, expect } from "@playwright/test";
import { FETCH_TIMEOUT_MS } from "../pages/主动回复设置/frontend-core.mjs";
import { configPayload } from "./fixtures/config-payload.mjs";
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, resolve, sep } from "node:path";

const ROOT = resolve(import.meta.dirname, "..");
const PAGE_PATH = "/pages/%E4%B8%BB%E5%8A%A8%E5%9B%9E%E5%A4%8D%E8%AE%BE%E7%BD%AE/index.html";
// 首屏就绪等待预算，与 app.js 的 BOOT_TIMEOUT_MS（12s）同量级：页面自身对首屏
// 给的是 12s 看门狗 + 每次抓取 15s 硬上限，测试用 5s（expect 默认）会在负载高时
// 偶发假红。这里只放宽"等页面启动完成"的窗口，不放宽任何断言。
const BOOT_WAIT_MS = 15_000;
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
  ({ config, providers, providersFail, saveMode, theme, dim, bold, refreshConfigPending, themePending, themePostPending, cleanupRemoved, cleanupFail, readyFails }) => {
      const state = {
        saveMode,
        saveAttempts: 0,
        config,
        configCalls: 0,
        providers,
        providersFail,
        refreshConfigPending,
        themePending,
        themePostAttempts: 0,
        themePostPending,
        cleanupRemoved,
        cleanupFail,
        readyFails,
      };
      window.__bridgeCalls = [];
      window.__appliedThemeWrites = [];
      window.__bridgeState = state;
      window.AstrBotPluginPage = {
        ready: async () => {
          if (state.readyFails) throw new Error("bridge handshake rejected");
          return true;
        },
        apiGet: async (endpoint) => {
          window.__bridgeCalls.push({ method: "GET", endpoint });
          // 握手已失败时 bridge 不可用：任何调用都是误用，必须炸出来而不是
          // 静默返回成功值，否则「不回退 fetch」的缺陷在用例里不可见。
          if (state.readyFails) throw new Error("bridge used after handshake failure");
          if (endpoint === "providers") {
            if (state.providersFail) throw new Error("provider list unavailable");
            // state.providers 让用例在刷新间更换列表内容：真实宿主里 Provider
            // 会增减，而 render() 对「当前值不在新列表里」有归零路径。
            return { ok: true, providers: state.providers || [{ id: "provider-a", label: "Provider A" }] };
          }
          if (endpoint === "config") {
            state.configCalls += 1;
            if (state.refreshConfigPending && state.configCalls > 1) {
              // 冻结请求发出那一刻的快照：后端与前端是两个进程，迟到 GET 读出
              // 的可能是保存之前的旧值，正是迟到响应覆盖已保存编辑的场景。
              const snapshot = { ...state.config };
              return new Promise((resolve) => {
                window.__resolveRefreshConfig = () => resolve(snapshot);
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
          if (state.readyFails) throw new Error("bridge used after handshake failure");
          if (endpoint === "config") {
            state.saveAttempts += 1;
            if (state.saveMode === "pending" && state.saveAttempts === 1) {
              return new Promise(() => {});
            }
            if (state.saveMode === "fail-once" && state.saveAttempts === 1) {
              return { ok: false, error: "write failed" };
            }
            if (state.saveMode === "stale") {
              // CAS 失配：后端返回 STALE_WRITE 与新 revision，前端必须要求刷新。
              return {
                ok: false,
                error: "配置已被其他请求修改",
                error_code: "STALE_WRITE",
                config_revision: `sha256:${"d".repeat(64)}`,
              };
            }
            if (state.saveMode === "field-error") {
              // 模拟后端 _strict_int 的字段级拒绝：error 文案以「键名 + 空格」
              // 前缀自带定位，驱动 saveConfig 的 numberField 标红路径。
              return { ok: false, error: "cooldown_sec 必须是整数" };
            }
            // 响应形状必须与真实后端一致：POST /config 返回
            // { ok, config, config_revision, runtime_enabled, adjusted_fields }。
            // config 是 Settings.to_config_dict() 的输出，**不含**面板视图键
            // decision_prompt_default（只存在于 GET，见 webapi._api_get_config）。
            // 桩比真实后端"更完整"会让「恢复默认提示词」这类缺陷在所有用例中
            // 不可见，所以这里显式剔除，让 POST 后的 config 与磁盘内容同形。
            const { base_revision: _ignoredRevision, ...persisted } = body;
            state.config = {
              ...state.config,
              ...persisted,
              runtime_enabled: true,
              ok: true,
              config_revision: `sha256:${"c".repeat(64)}`,
            };
            delete state.config.decision_prompt_default;
            return {
              ok: true,
              config: state.config,
              config_revision: state.config.config_revision,
              runtime_enabled: true,
              adjusted_fields: state.saveMode === "adjusted" ? ["whitelist_sessions"] : [],
            };
          }
          if (endpoint === "ui/theme") {
            state.themePostAttempts += 1;
            // 落盘时刻即响应返回时刻：两笔写入乱序到达靠这个顺序复现。
            const land = () => window.__appliedThemeWrites.push(body || {});
            const response = { ok: true, theme: body?.theme || "auto", removed: 0 };
            if (state.themePostPending && state.themePostAttempts === 1) {
              return new Promise((resolve) => {
                window.__resolveThemePost = () => {
                  land();
                  resolve(response);
                };
              });
            }
            land();
            return response;
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
      providers: options.providers,
      providersFail: Boolean(options.providersFail),
      saveMode: options.saveMode || "success",
      theme: options.theme || "light",
      dim: Boolean(options.dim),
      bold: Boolean(options.bold),
      refreshConfigPending: Boolean(options.refreshConfigPending),
      themePending: Boolean(options.themePending),
      themePostPending: Boolean(options.themePostPending),
      cleanupRemoved: options.cleanupRemoved ?? 0,
      cleanupFail: Boolean(options.cleanupFail),
      readyFails: Boolean(options.readyFails),
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
  // 首屏等待采用页面启动预算；控件读值和交互仍使用各自的断言预算。
  await expect(page.locator("#boot")).toHaveClass(/is-hidden/, {
    timeout: BOOT_WAIT_MS,
  });
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
  // 760 落在 1024 与 720 两个断点之间：侧栏已收窄、移动端布局还没接管。
  // 这一段既不属于 desktop 档也不属于 mobile 档，只靠两端各测一档会整体漏掉。
  { name: "narrow tablet light", viewport: { width: 760, height: 900 }, theme: "light" },
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

test("an unknown save state remains recoverable without replaying the write", async ({ page }) => {
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
  // 四个保存入口都被禁用：焦点必须落到下一步唯一可做的刷新按钮，而不是 body。
  await expect(page.locator("#refreshBtn")).toBeFocused();
  // 超时时并不知道写没写进去：侧栏不能断言「保存失败」。
  await expect(page.locator("#navSaveState")).toHaveText("状态未知");
  // unknown 借用 error 的红样式：没有这一档就只剩默认色。
  await expect(page.locator("#configSaveState")).toHaveClass(/is-error/);
  // 顶栏保存入口持续红边；改字也不能把「状态未知」盖成「有未保存改动」。
  await expect(page.locator("#saveTopBtn")).toHaveClass(/is-failed/);
  await page.locator("#messageDelayInput").fill("80");
  await expect(page.locator("#configSaveState")).toHaveText("保存状态未知");
  await expect(page.locator("#navSaveState")).toHaveText("状态未知");
  await expect(page.locator("#configForm")).not.toHaveAttribute("inert", "");
  await expect(page.locator("#saveTopBtn")).toBeDisabled();

  // 刷新成功要同时清掉失败外环与侧栏「状态未知」，否则会一直停在错误态。
  await page.locator("#refreshBtn").click();
  await page.locator("#refreshBtn").click();
  await expect(page.locator("#configSaveState")).toHaveText("");
  await expect(page.locator("#navSaveState")).not.toHaveText("状态未知");
  await expect(page.locator("#saveTopBtn")).not.toHaveClass(/is-failed/);

  await page.waitForTimeout(80);
  const posts = await page.evaluate(() =>
    window.__bridgeCalls.filter((call) => call.method === "POST" && call.endpoint === "config")
  );
  expect(posts).toHaveLength(1);
  expect(errors).toEqual([]);
});

test("unknown save state focuses a reachable refresh affordance on narrow screens", async ({ page }) => {
  // 窄屏刷新按钮收在「更多操作」菜单里（菜单 hidden 时 focus() 是空操作），
  // 回焦必须落到菜单触发器上，否则焦点仍掉到 body。
  await page.setViewportSize({ width: 390, height: 900 });
  await installBridge(page, { saveMode: "pending" });
  const errors = await openPage(page);
  await page.evaluate((timeoutMs) => {
    const nativeSetTimeout = window.setTimeout.bind(window);
    window.setTimeout = (callback, delay, ...args) =>
      nativeSetTimeout(callback, delay === timeoutMs ? 30 : delay, ...args);
  }, FETCH_TIMEOUT_MS);
  await page.locator("#saveTopBtn").click();
  await expect(page.locator("#toast")).toContainText("保存状态未知");
  await expect(page.locator("#moreActionsBtn")).toBeFocused();
  expect(errors).toEqual([]);
});

test("every checkbox renders the backend bool value", async ({ page }) => {
  // 要守的是渲染本身：后端 false 却显示勾选，用户会以为功能已开。
  const overrides = {
    enabled: true,
    enabled_private_sessions: false,
    abandon_stale_on_new_message: true,
    skip_after_direct_call: false,
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
    skipAfterDirectCallInput: "skip_after_direct_call",
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

test("a late refresh does not overwrite an edit that was saved meanwhile", async ({ page }) => {
  // 保存完成后仍须拒绝迟到的旧快照，避免配置值和 revision 一起回退。
  await installBridge(page, { refreshConfigPending: true });
  const errors = await openPage(page);

  // 先编辑再刷新：刷新开始时表单已脏（loadStartedDirty=true）。
  await page.locator("#messageDelayInput").fill("75");
  await page.locator("#refreshBtn").click();
  // 脏表单需二次点击确认；确认后请求才真正发出并挂起。
  await page.locator("#refreshBtn").click();
  await expect
    .poll(() => page.evaluate(() => typeof window.__resolveRefreshConfig))
    .toBe("function");

  await page.locator("#saveTopBtn").click();
  await expect(page.locator("#navSaveState")).toHaveText("已保存");
  const savedRevision = await page.evaluate(
    () => window.__bridgeState.config.config_revision,
  );

  // 迟到刷新此刻才到达：它携带的快照早于上面的保存（服务端冻结在请求发出时）。
  await page.evaluate(() => window.__resolveRefreshConfig());
  await page.waitForTimeout(120);

  await expect(page.locator("#messageDelayInput")).toHaveValue("75");
  // 保存已完成后响应才被协调器作废：内容已是最新且已落盘，此刻再说"有未保存
  // 改动"是假话（用户刚刚保存过），只会逼他重复保存。toast 必须是中性的那句。
  await expect(page.locator("#toast")).toHaveText("响应未应用，已保留当前内容");
  const revisionAfter = await page.evaluate(
    () => window.__bridgeState.config.config_revision,
  );
  expect(revisionAfter).toBe(savedRevision);
  expect(errors).toEqual([]);
});

test("a stale write asks for a refresh and focuses the refresh affordance", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { saveMode: "stale" });
  const errors = await openPage(page);
  await page.locator("#messageDelayInput").fill("75");
  await page.locator("#saveTopBtn").click();
  await expect(page.locator("#toast")).toContainText("刷新配置后重试");
  await expect(page.locator("#configSaveState")).toHaveText("保存状态未知");
  await expect(page.locator("#configSaveState")).toHaveClass(/is-error/);
  await expect(page.locator("#navSaveState")).toHaveText("状态未知");
  await expect(page.locator("#refreshBtn")).toBeFocused();
  expect(errors).toEqual([]);
});

test("saving returns keyboard focus to the triggering button", async ({ page }) => {
  // 保存期间按钮 disabled，浏览器把焦点丢给 body；恢复 enabled 后不回填，键盘用户
  // 要重新 Tab 一遍才能继续。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page);
  const errors = await openPage(page);
  await page.locator("#saveTopBtn").focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#navSaveState")).toHaveText("已保存");
  await expect(page.locator("#saveTopBtn")).toBeFocused();
  expect(errors).toEqual([]);
});

test("a late refresh on a form edited after the save names the pending edits", async ({ page }) => {
  // 保存后继续编辑时，迟到响应须保留新编辑并提示先保存再刷新。
  await installBridge(page, { refreshConfigPending: true });
  const errors = await openPage(page);
  await page.locator("#messageDelayInput").fill("75");
  await page.locator("#refreshBtn").click();
  // 脏表单需二次点击确认；确认后请求才真正发出并挂起。
  await page.locator("#refreshBtn").click();
  await expect
    .poll(() => page.evaluate(() => typeof window.__resolveRefreshConfig))
    .toBe("function");

  await page.locator("#saveTopBtn").click();
  await expect(page.locator("#navSaveState")).toHaveText("已保存");
  // 保存之后的新编辑：isDirty 重新变真，editEpoch 也已推进。
  await page.locator("#messageDelayInput").fill("80");
  await page.evaluate(() => window.__resolveRefreshConfig());

  await expect(page.locator("#toast")).toHaveText(
    "检测到未保存改动，已保留当前内容，请保存后再刷新",
  );
  await expect(page.locator("#messageDelayInput")).toHaveValue("80");
  await expect(page.locator("#navSaveState")).toContainText("有未保存改动");
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
  await expect(page.locator("#providerListState")).toContainText("判断与识图模型可手动填写");
  expect(errors).toEqual([]);
});

test("a provider list failure keeps the provider already chosen in the form", async ({ page }) => {
  // 刷新时 providers 失败：catch 分支先调 render()，而 render() 是
  // `innerHTML = ""` 后 `select.value = current`，列表为空时写回即归零。
  // 已选 Provider 就此丢失，之后（配置响应到达前）的保存会把空值提交回服务端，
  // 而用户看到的只是「已保存」。取值必须发生在 render() 之前，取到后写进手动
  // 输入框：列表不可用时这就是该控件唯一的展示与提交来源。
  await installBridge(page, {
    config: { judge_provider_id: "provider-a", vision_provider_id: "provider-b" },
    refreshConfigPending: true,
  });
  const errors = await openPage(page);
  await expect(page.locator("#judgeProviderSelect")).toHaveValue("provider-a");

  // 制造一次真正走 catch 分支的刷新：providers 失败，config 响应挂起。
  await page.locator("#messageDelayInput").fill("75");
  await page.evaluate(() => {
    window.__bridgeState.providersFail = true;
  });
  await page.locator("#refreshBtn").click();
  await page.locator("#refreshBtn").click();
  await expect(page.locator("#providerListState")).toContainText("判断与识图模型可手动填写");

  for (const [inputSelector, expected] of [
    ["#judgeProviderInput", "provider-a"],
    ["#visionProviderInput", "provider-b"],
  ]) {
    await expect(page.locator(inputSelector)).toHaveValue(expected);
  }

  // 配置响应仍在途（15s 窗口内），此刻保存必须带上原值而不是空串。
  await page.locator("#saveTopBtn").click();
  const body = await page.evaluate(() => {
    const posts = window.__bridgeCalls.filter(
      (call) => call.method === "POST" && call.endpoint === "config",
    );
    return posts[posts.length - 1]?.body || null;
  });
  expect(body).not.toBeNull();
  expect(body.judge_provider_id).toBe("provider-a");
  expect(body.vision_provider_id).toBe("provider-b");
  expect(errors).toEqual([]);
});

test("a shrunk provider list keeps the provider already chosen in the form", async ({ page }) => {
  // 重建 Provider 列表时保留用户选择，避免列表缺少当前值就把空串保存回服务端。
  await installBridge(page, {
    config: { judge_provider_id: "provider-a", vision_provider_id: "provider-b" },
    refreshConfigPending: true,
  });
  const errors = await openPage(page);
  await expect(page.locator("#judgeProviderSelect")).toHaveValue("provider-a");

  // 刷新：Provider 列表缩小（不再含 provider-a），config 响应挂起。
  await page.locator("#messageDelayInput").fill("75");
  await page.evaluate(() => {
    window.__bridgeState.providers = [{ id: "provider-z", label: "Provider Z" }];
  });
  await page.locator("#refreshBtn").click();
  await page.locator("#refreshBtn").click();
  await expect(page.locator("#providerListState")).toHaveText("");

  // 值不在列表里：控件落手动态，值写进输入框（此刻它是唯一展示与提交来源）。
  await expect(page.locator("#judgeProviderInput")).toHaveValue("provider-a");
  await expect(page.locator("#visionProviderInput")).toHaveValue("provider-b");

  await page.locator("#saveTopBtn").click();
  const body = await page.evaluate(() => {
    const posts = window.__bridgeCalls.filter(
      (call) => call.method === "POST" && call.endpoint === "config",
    );
    return posts[posts.length - 1]?.body || null;
  });
  expect(body).not.toBeNull();
  expect(body.judge_provider_id).toBe("provider-a");
  expect(body.vision_provider_id).toBe("provider-b");
  expect(errors).toEqual([]);
});

test("a rejected bridge handshake falls back to fetch instead of using the bridge", async ({
  page,
}) => {
  // 握手失败意味着 bridge 不可信，requestPluginApi 靠 getBridge() 的返回值决定
  // 走 bridge 还是走 fetch。返回了对象就等于宣称握手成功，fetch 兜底永不触发，
  // 而桩的 apiGet/apiPost 在这种情况下会抛，整页配置随之加载失败。
  await installBridge(page, { readyFails: true });
  const errors = await openPage(page);
  await expect(page.locator("#selfStatus")).toHaveText("启用");
  await expect(page.locator("#configForm")).not.toHaveAttribute("inert", "");
  expect(await page.evaluate(() => window.__bridgeCalls)).toEqual([]);
  expect(errors).toEqual([]);
});

test("theme clicks never submit untouched dim/bold preferences", async ({ page }) => {
  // 主题点击只提交用户操作过的字段，保留服务端的压暗和粗体偏好。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { themePending: true, theme: "dark", dim: true, bold: true });
  const errors = await openPage(page);
  await expect.poll(() => page.evaluate(() => typeof window.__resolveTheme)).toBe("function");

  await page.locator("#themeToggle").click();
  await expect
    .poll(() =>
      page.evaluate(() =>
        window.__bridgeCalls.filter(
          (call) => call.method === "POST" && call.endpoint === "ui/theme",
        ).length,
      ),
    )
    .toBe(1);
  const themeOnly = await page.evaluate(() =>
    window.__bridgeCalls
      .filter((call) => call.method === "POST" && call.endpoint === "ui/theme")
      .map((call) => call.body),
  );
  expect(themeOnly).toHaveLength(1);
  expect(themeOnly[0].theme).toBe("light");
  expect("dim" in themeOnly[0]).toBe(false);
  expect("bold" in themeOnly[0]).toBe(false);

  // 对照：用户真的点过压暗后，该次请求必须带上 dim，否则本地状态与服务端
  // prefs 分叉（刷新后用户的点击丢失）。未动过的 bold 仍不得出现：服务端的
  // bold=true 已经落地，提交它等于替用户表态，取值一错就抹掉他的选择。
  await page.evaluate(() => window.__resolveTheme());
  await expect(page.locator("html")).toHaveClass(/dimmed/); // 服务端 dim=true 已落地
  await page.locator("#dimBtn").click();
  await expect(page.locator("html")).not.toHaveClass(/dimmed/);
  const afterDim = await page.evaluate(() => ({
    body: window.__bridgeCalls
      .filter((call) => call.method === "POST" && call.endpoint === "ui/theme")
      .map((call) => call.body)
      .at(-1),
  }));
  expect(afterDim.body).toEqual({ dim: false });
  expect(errors).toEqual([]);
});

test("topbar height token follows the measured topbar height across breakpoints", async ({ page }) => {
  // 侧栏让位与锚点偏移使用实际顶栏高度；断点和换行变化后必须重新测量。
  await page.setViewportSize({ width: 900, height: 800 });
  await installBridge(page);
  const errors = await openPage(page);
  const probe = () =>
    page.evaluate(() => ({
      height: document.querySelector(".topbar").getBoundingClientRect().height,
      token: getComputedStyle(document.documentElement)
        .getPropertyValue("--topbar-h")
        .trim(),
    }));

  for (const width of [900, 600, 1440, 400]) {
    await page.setViewportSize({ width, height: 800 });
    await page.waitForTimeout(120);
    const { height, token } = await probe();
    expect(
      token,
      `${width}px 视口：令牌 ${token} 与实测顶栏高度 ${height}px 不符`,
    ).toBe(`${Math.round(height)}px`);
  }

  // 写回必须停在一处：观测 → 写变量 → 布局 → 观测 若形成正反馈会自激。
  // 值不收敛时（例如写回未取整的小数）每次测量都会产生新的 style 变更。
  const after = await page.evaluate(async () => {
    const seen = [];
    const root = document.documentElement;
    const observer = new MutationObserver(() => {
      seen.push(root.style.getPropertyValue("--topbar-h"));
    });
    observer.observe(root, { attributes: true, attributeFilter: ["style"] });
    await new Promise((resolve) => setTimeout(resolve, 400));
    observer.disconnect();
    return seen;
  });
  expect(after, `写回未收敛，仍在重复写入：${after.join(",")}`).toEqual([]);

  // 修好的可见症状：滚动后侧栏不再钻到顶栏底下。
  await page.setViewportSize({ width: 900, height: 800 });
  await page.waitForTimeout(120);
  await page.evaluate(() => window.scrollTo(0, 1200));
  await page.waitForTimeout(120);
  const boxes = await page.evaluate(() => ({
    navTop: document.querySelector(".sidenav").getBoundingClientRect().top,
    barBottom: document.querySelector(".topbar").getBoundingClientRect().bottom,
  }));
  expect(boxes.navTop).toBeGreaterThanOrEqual(boxes.barBottom - 0.5);
  expect(errors).toEqual([]);
});

test("anchor jumps keep the section title clear of the wrapped topbar", async ({ page }) => {
  // 窄屏顶栏换行后，锚点标题仍须避开实际顶栏。
  await page.setViewportSize({ width: 600, height: 800 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await installBridge(page);
  const errors = await openPage(page);
  await page.locator('.mtab[data-target="sec-scope"]').click();
  await page.waitForTimeout(200);
  const boxes = await page.evaluate(() => ({
    titleTop: document.querySelector("#sec-scope .panel-head h2").getBoundingClientRect().top,
    barBottom: document.querySelector(".topbar").getBoundingClientRect().bottom,
  }));
  expect(boxes.titleTop).toBeGreaterThanOrEqual(boxes.barBottom);
  expect(errors).toEqual([]);
});

test("mobile save state keeps three distinct colours", async ({ page }) => {
  // ≤720px 的 `.mobile-savebar .mobile-save-state { color: var(--muted) }`
  // 以同特异性且更靠后的位置覆盖了三态配色：成功/失败/待保存颜色完全一致，
  // 移动端用户看不出保存结果。断言取实际计算色（并比对该状态的令牌），
  // 而不是只查源码里有那条规则。
  await page.setViewportSize({ width: 360, height: 800 });
  await installBridge(page);
  const errors = await openPage(page);
  const states = await page.evaluate(() => {
    // 每个状态用一个新建元素读：`.save-state` 带 color 过渡，在既有元素上改类
    // 会读到过渡中间值。而状态由 setSaveState/applyConfigPayload 直接设置，
    // 那时元素已带正确的类，故这里复现的是「稳态颜色」。
    const bar = document.querySelector("#mobileSaveBar");
    const read = (className) => {
      const el = document.createElement("span");
      el.className = `save-state mobile-save-state${className ? ` ${className}` : ""}`;
      bar.appendChild(el);
      const color = getComputedStyle(el).color;
      el.remove();
      return color;
    };
    const probe = (variable) => {
      const el = document.createElement("div");
      el.style.setProperty("color", `var(${variable})`);
      bar.appendChild(el);
      const value = getComputedStyle(el).color;
      el.remove();
      return value;
    };
    return {
      neutral: read(""),
      ok: read("is-ok"),
      error: read("is-error"),
      pending: read("is-pending"),
      tokens: { ok: probe("--ok"), danger: probe("--danger"), accent: probe("--accent-text") },
    };
  });
  expect(states.ok, "成功态未用 --ok").toBe(states.tokens.ok);
  expect(states.error, "失败态未用 --danger").toBe(states.tokens.danger);
  expect(states.pending, "待保存态未用 --accent-text").toBe(states.tokens.accent);
  expect(states.neutral, "中性态未用 --muted").not.toBe(states.ok);

  // 端到端对照：保存成功后移动端状态文字必须真的变绿，而不是只在孤立元素上成立。
  await page.locator("#decisionPromptInput").scrollIntoViewIfNeeded();
  await page.locator("#decisionPromptInput").fill("移动端配色");
  await page.locator("#saveMobileBtn").click();
  await expect(page.locator("#mobileSaveState")).toHaveText("已保存");
  await expect
    .poll(() => page.evaluate(() => getComputedStyle(document.querySelector("#mobileSaveState")).color))
    .toBe(states.tokens.ok);
  expect(errors).toEqual([]);
});

test("a switched-off readout stops its pulse animation", async ({ page }) => {
  // `.master.is-on .stat-dot` 给的脉冲动画会命中卡内所有读数点，包括那个已关闭
  // 的「判断模型」。is-off 只改颜色不重置动画，于是一个关掉的功能继续在脉冲。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { config: { decision_model_enabled: false, enabled: true } });
  const errors = await openPage(page);
  await expect(page.locator("#decisionModelStat")).toHaveClass(/is-off/);
  await expect(page.locator("#selfStat")).toHaveClass(/is-on/);
  await expect
    .poll(() =>
      page.evaluate(
        () => getComputedStyle(document.querySelector("#decisionModelStat .stat-dot")).animationName,
      ),
    )
    .toBe("none");

  // 对照：开着的那份必须仍有动画，否则这条断言会被"全局关掉动画"满足。
  await page.evaluate(() =>
    document.querySelector("#decisionModelStat").className = "readout is-on",
  );
  await expect
    .poll(() =>
      page.evaluate(
        () => getComputedStyle(document.querySelector("#decisionModelStat .stat-dot")).animationName,
      ),
    )
    .toBe("stat-pulse");
  expect(errors).toEqual([]);
});

test("switching the master switch moves both the status text and the state class", async ({ page }) => {
  // enabledInput 的 change 必须同时改文案与状态类。只改文案时，关掉总开关后
  // `.master.is-off` 不生效（stat-dot 继续脉冲、卡片背景不变），视觉仍显示为启用，
  // 直到保存后 applyConfigPayload 才自愈。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { config: { enabled: true } });
  const errors = await openPage(page);
  await expect(page.locator("#selfStat")).toHaveClass(/is-on/);
  await expect(page.locator("#selfStatus")).toHaveText("启用");

  // 总开关是 label 包裹的自定义控件，视觉层 .master-track 覆盖在 input 上，
  // 直接点 input 会被它拦截；点 label 文本与真实用户操作一致。
  await page.locator("label.master-switch").click();
  await expect(page.locator("#selfStatus")).toHaveText("关闭（未保存）");
  await expect(page.locator("#selfStat")).toHaveClass(/is-off/);

  await page.locator("label.master-switch").click();
  await expect(page.locator("#selfStatus")).toHaveText("启用（未保存）");
  await expect(page.locator("#selfStat")).toHaveClass(/is-on/);
  expect(errors).toEqual([]);
});

test("the master switch readout stays paused while the runtime is off", async ({ page }) => {
  // /selfreply off 只翻 runtime_enabled（enabled 仍是 true）：此时拨动总开关，
  // 文案与状态类必须仍按运行态显示「已暂停」。只按复选框显示时，暂停期间
  // 拨动开关会把状态灯重新点亮、文案写成「启用（未保存）」，直到保存或刷新
  // 才自愈——视觉上插件是开着的，实际仍暂停。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { config: { enabled: true, runtime_enabled: false } });
  const errors = await openPage(page);
  await expect(page.locator("#selfStat")).toHaveClass(/is-off/);
  await expect(page.locator("#selfStatus")).toHaveText("已暂停（/off）");

  // 关掉再打开：运行态仍是暂停，读报不得回染成「启用」。
  await page.locator("label.master-switch").click();
  await expect(page.locator("#selfStatus")).toHaveText("关闭（未保存）");
  await expect(page.locator("#selfStat")).toHaveClass(/is-off/);

  await page.locator("label.master-switch").click();
  await expect(page.locator("#selfStatus")).toHaveText("已暂停（未保存）");
  await expect(page.locator("#selfStat")).toHaveClass(/is-off/);
  expect(errors).toEqual([]);
});

test("the master switch readout stays off while the persisted switch is off", async ({ page }) => {
  // 面板里关掉总开关并保存：持久 enabled=false，而运行态没被 /selfreply off
  // 翻过（runtime_enabled 仍是 true）。applyConfigPayload 此前只按 runtimeOn
  // 定 #selfStat 的状态类，漏了 config.enabled——复选框未勾、文字写「关闭」，
  // 而 .master.is-on 仍命中 stat-dot 脉冲、卡片高亮与强调色，视觉上显示为启用。
  // app.js 的 change 处理器用的是 on && runtimeOn，两条路径对同一个量给出
  // 相反的类；加载配置、保存成功、刷新三条路径都走 applyConfigPayload。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { config: { enabled: false, runtime_enabled: true } });
  const errors = await openPage(page);
  await expect(page.locator("#selfStat")).toHaveClass(/is-off/);
  await expect(page.locator("#selfStatus")).toHaveText("关闭");

  // 拨到开（未保存）再拨回：change 处理器与 applyConfigPayload 必须给同一个类
  await page.locator("label.master-switch").click();
  await expect(page.locator("#selfStatus")).toHaveText("启用（未保存）");
  await expect(page.locator("#selfStat")).toHaveClass(/is-on/);

  await page.locator("label.master-switch").click();
  await expect(page.locator("#selfStatus")).toHaveText("关闭（未保存）");
  await expect(page.locator("#selfStat")).toHaveClass(/is-off/);
  expect(errors).toEqual([]);
});

test("dark theme keyboard focus stays visible on the toggle, the text action and summaries", async ({ page }) => {
  // 深色下三处元素沿用浏览器默认 outline（深色底上对比度约 1.03，等于看不见）。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { theme: "dark" });
  const errors = await openPage(page);
  const inspect = await page.evaluate(() => {
    const read = (selector) => {
      const el = document.querySelector(selector);
      el.focus();
      const style = getComputedStyle(el);
      return {
        focused: document.activeElement === el,
        outline: `${style.outlineStyle} ${style.outlineWidth} ${style.outlineColor}`,
        boxShadow: style.boxShadow,
        focusColor: getComputedStyle(document.documentElement)
          .getPropertyValue("--focus-color")
          .trim(),
      };
    };
    const wrap = (selector) => {
      const preview = document.createElement("div");
      preview.style.color = `var(--focus-color)`;
      document.body.appendChild(preview);
      const focusColor = getComputedStyle(preview).color;
      preview.remove();
      return { ...read(selector), focusColor };
    };
    return {
      themeToggle: wrap("#themeToggle"),
      formatBtn: wrap("#formatWhitelistBtn"),
      runtimeSummary: wrap("#sec-runtime > summary"),
      promptHelpSummary: wrap(".prompt-help > summary"),
    };
  });
  const rgb = (value) => value.match(/rgba?\(([^)]+)\)/)?.[1];
  for (const [name, state] of Object.entries(inspect)) {
    expect(state.focused, `${name} 未能聚焦`).toBe(true);
    const visible =
      (state.outline.startsWith("solid") && rgb(state.outline) === rgb(state.focusColor)) ||
      state.boxShadow !== "none";
    expect(
      visible,
      `${name} 在深色下的焦点环不可见：outline=${state.outline} focus=${state.focusColor} shadow=${state.boxShadow}`,
    ).toBe(true);
  }
  expect(errors).toEqual([]);
});

test("provider and mention controls expose the labels screen readers should announce", async ({ page }) => {
  // 可访问名必须是字段标题本身，而不是把 field-hint 一并算进去
  // （读屏会念出「@ 概率 判断模型未表态时按此概率引用」这种长串）。
  // 有描述的字段在这里直接读可访问名；无描述的 Provider 手动输入靠 aria-label。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page);
  const errors = await openPage(page);
  const names = await page.evaluate(() => {
    const byLabel = (id) => {
      const control = document.getElementById(id);
      const label = document.getElementById(`${id}Label`);
      return {
        labelText: label?.textContent?.replace(/\s+/g, " ").trim() || null,
        labelledby: control?.getAttribute("aria-labelledby") || null,
        describedby: control?.getAttribute("aria-describedby") || null,
      };
    };
    return {
      decisionTemp: byLabel("decisionTempInput"),
      mentionMode: byLabel("mentionModeInput"),
    };
  });
  expect(names.decisionTemp.labelledby).toBe("decisionTempInputLabel");
  expect(names.decisionTemp.labelText).toBe("判断温度");
  await expect(page.locator("#decisionTempInput")).toHaveAccessibleName("判断温度");
  expect(names.mentionMode.labelledby).toBe("mentionModeInputLabel");
  expect(names.mentionMode.describedby).toBe("mentionModeHint");
  await expect(page.locator("#mentionModeInput")).toHaveAccessibleName("主动回复 @ 对方");
  await page.locator("#providerManualBtn").click();
  await expect(page.locator("#judgeProviderInput")).toBeVisible();
  await expect(page.locator("#judgeProviderInput")).toHaveAccessibleName("手动填写判断模型 Provider ID");
  await expect(page.locator("#judgeProviderInput")).toHaveAccessibleDescription(
    "留空表示使用当前会话默认模型",
  );
  expect(errors).toEqual([]);
});

test("the master switch name is its static label, not the live status text", async ({ page }) => {
  // #selfStatus 是 aria-live 状态文本。它落在包裹 <label> 内时会被算进开关的
  // 可访问名，于是开关名随状态漂移（"主动回复 启用" / "主动回复 关闭"），
  // 读屏在 live 播报之外还会因名字变化再播一次。名字必须钉在静态标题上。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page);
  const errors = await openPage(page);

  await expect(page.locator("#enabledInput")).toHaveAccessibleName(/^主动回复$/);
  await expect(page.locator("#enabledInput")).toHaveAttribute(
    "aria-labelledby",
    "enabledInputLabel"
  );
  // 状态文本本身照旧可读可播报，不能为了改名把它藏掉。
  await expect(page.locator("#selfStatus")).toHaveText("启用");
  await expect(page.locator("#selfStatus")).toHaveAttribute("aria-live", "polite");
  expect(errors).toEqual([]);
});

test("dim and bold switches expose their pressed state", async ({ page }) => {
  // #dimBtn/#boldBtn 是切换开关，此前只有 .active 类，读屏无从得知当前状态。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { dim: false, bold: true });
  const errors = await openPage(page);
  await expect(page.locator("#dimBtn")).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator("#boldBtn")).toHaveAttribute("aria-pressed", "true");

  await page.locator("#dimBtn").click();
  await expect(page.locator("#dimBtn")).toHaveAttribute("aria-pressed", "true");
  await page.locator("#boldBtn").click();
  await expect(page.locator("#boldBtn")).toHaveAttribute("aria-pressed", "false");
  // 与视觉状态同源：类与 aria 不得各说一套。
  const synced = await page.evaluate(() => ({
    dimClass: document.getElementById("dimBtn").classList.contains("active"),
    dimAria: document.getElementById("dimBtn").getAttribute("aria-pressed"),
    boldClass: document.getElementById("boldBtn").classList.contains("active"),
    boldAria: document.getElementById("boldBtn").getAttribute("aria-pressed"),
  }));
  expect(synced.dimAria).toBe(String(synced.dimClass));
  expect(synced.boldAria).toBe(String(synced.boldClass));
  expect(errors).toEqual([]);
});

test("the whitelist text action keeps a usable touch target", async ({ page }) => {
  // #formatWhitelistBtn 实测 73×23px，低于 24px 的最小触控尺寸。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page);
  const errors = await openPage(page);
  const desktop = await page.locator("#formatWhitelistBtn").boundingBox();
  expect(desktop.height, `桌面高度 ${desktop.height}px`).toBeGreaterThanOrEqual(24);

  await page.setViewportSize({ width: 360, height: 800 });
  const narrow = await page.locator("#formatWhitelistBtn").boundingBox();
  expect(narrow.height, `窄屏高度 ${narrow.height}px`).toBeGreaterThanOrEqual(44);
  expect(errors).toEqual([]);
});

test("folding panels keep their titles in the document outline", async ({ page }) => {
  // 折叠分区标题原为 span，文档大纲里缺这两项，读屏的标题跳转找不到它们。
  await installBridge(page);
  const errors = await openPage(page);
  const outline = await page.evaluate(() =>
    Array.from(document.querySelectorAll("h1, h2")).map((el) => ({
      tag: el.tagName,
      text: el.textContent.replace(/\s+/g, " ").trim(),
    })),
  );
  const titles = outline.map((item) => item.text);
  expect(titles).toContain("运行边界");
  expect(titles).toContain("图片识别");
  expect(outline.every((item) => item.tag === "H2" || item.tag === "H1")).toBe(true);
  expect(errors).toEqual([]);
});

test("vision provider fields lay out on one row like the judge field", async ({ page }) => {
  // 包裹层补上后，select 与实际接到的按钮必须同排；
  // 手动输入框在包裹层外，切到手动模式时按钮不能跟着消失。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { config: { vision_main_enabled: true } });
  const errors = await openPage(page);
  await page.locator("#visionProviderSelect").evaluate((el) => {
    const details = el.closest("details");
    if (details) details.open = true;
  });
  const rows = await page.evaluate(() => {
    const field = document.querySelector('[data-config-key="vision_provider_id"]');
    const select = document.getElementById("visionProviderSelect");
    const button = document.getElementById("visionProviderManualBtn");
    const wrap = select.parentElement;
    return {
      sameWrapper: button.parentElement === wrap,
      wrapperClass: wrap.className,
      gridColumns: getComputedStyle(wrap).gridTemplateColumns.split(" ").length,
      selectBottom: select.getBoundingClientRect().bottom,
      buttonTop: button.getBoundingClientRect().top,
      inputHidden: document.getElementById("visionProviderInput").hidden,
      fieldTop: field.getBoundingClientRect().top,
    };
  });
  expect(rows.sameWrapper, "select 与按钮不在同一个 .provider-control 里").toBe(true);
  expect(rows.wrapperClass).toContain("provider-control");
  expect(rows.gridColumns, "包裹层不是两列横排").toBe(2);
  expect(rows.buttonTop).toBeLessThan(rows.selectBottom);

  await page.locator("#visionProviderManualBtn").click();
  await expect(page.locator("#visionProviderInput")).toBeVisible();
  await expect(page.locator("#visionProviderManualBtn")).toBeVisible();
  await expect(page.locator("#visionProviderManualBtn")).toHaveAttribute(
    "aria-expanded",
    "true",
  );

  // 手动输入时 Provider 控件使用单行布局，避免按钮被拉成整行。
  await page.locator("#visionJudgeProviderManualBtn").click();
  const manual = await page.evaluate(() => {
    const measure = (fieldId, buttonId) => {
      const field = document.getElementById(fieldId);
      const button = document.getElementById(buttonId);
      return {
        hasManualClass: field.classList.contains("manual"),
        columns: getComputedStyle(field.querySelector(".provider-control"))
          .gridTemplateColumns,
        buttonWidth: Math.round(button.getBoundingClientRect().width),
      };
    };
    return {
      vision: measure("visionProviderField", "visionProviderManualBtn"),
      visionJudge: measure(
        "visionJudgeProviderField",
        "visionJudgeProviderManualBtn",
      ),
    };
  });
  for (const [label, state] of Object.entries(manual)) {
    expect(state.hasManualClass, `${label} 容器未挂 manual 类`).toBe(true);
    expect(
      state.columns.trim().split(/\s+/).length,
      `${label} 手动态仍是两列：${state.columns}`,
    ).toBe(1);
    expect(
      state.buttonWidth,
      `${label} 手动按钮被拉成整行宽：${state.buttonWidth}px`,
    ).toBeLessThan(100);
  }

  // 切回列表态恢复两列，避免手动布局规则残留。
  await page.locator("#visionProviderManualBtn").click();
  await expect(page.locator("#visionProviderManualBtn")).toHaveAttribute(
    "aria-expanded",
    "false",
  );
  const restored = await page.evaluate(() => {
    const field = document.getElementById("visionProviderField");
    return {
      hasManualClass: field.classList.contains("manual"),
      columns: getComputedStyle(field.querySelector(".provider-control"))
        .gridTemplateColumns,
    };
  });
  expect(restored.hasManualClass).toBe(false);
  expect(restored.columns.trim().split(/\s+/).length).toBe(2);

  expect(errors).toEqual([]);
});

test("closing the compact menu returns focus to its trigger", async ({ page }) => {
  // 点菜单项后菜单 hidden，焦点随之掉到 body。键盘用户的下一次 Tab 从文档
  // 开头重新开始（顶栏之前的 skip-link 又跑一遍），等于被踢出上下文。
  await page.setViewportSize({ width: 360, height: 800 });
  await installBridge(page);
  const errors = await openPage(page);
  await page.locator("#moreActionsBtn").click();
  await expect(page.locator("#moreActionsBtn")).toHaveAttribute("aria-expanded", "true");
  await page.locator("#dimBtn").click();
  await expect(page.locator("#moreActionsMenu")).toBeHidden();
  await expect(page.locator("#moreActionsBtn")).toBeFocused();
  // 下一次 Tab 必须落在顶栏内的下一项，而不是文档开头。
  await page.keyboard.press("Tab");
  const focused = await page.evaluate(() => document.activeElement?.id || document.activeElement?.tagName);
  expect(["saveTopBtn", "refreshBtn", "themeToggle", "moreActionsBtn"]).toContain(focused);

  // 对照：焦点不在菜单内时（点页面别处关闭菜单）不得把焦点拽到触发器，
  // 否则用户的下一次 Tab 从顶栏重新开始，等于被踢出上下文。
  await page.locator("#moreActionsBtn").click();
  await expect(page.locator("#moreActionsMenu")).toBeVisible();
  await page.mouse.click(20, 400);
  await expect(page.locator("#moreActionsMenu")).toBeHidden();
  expect(await page.evaluate(() => document.activeElement?.id)).not.toBe(
    "moreActionsBtn",
  );
  expect(errors).toEqual([]);
});

test("arming the compact refresh confirm keeps the menu open for the second click", async ({
  page,
}) => {
  // #refreshBtn 在 #moreActionsMenu 内，而 chrome.mjs 给菜单里每个 button 都挂了
  // closeMenu。窄屏下脏表单的第一次点击只负责「武装」，提示语要求再点一次，
  // 但紧随其后的 closeMenu 会把菜单隐藏，第二下点不到（要先重新展开菜单），
  // 提示与可达行为矛盾。断言菜单仍可见、按钮带 is-armed，且第二下真的发出刷新。
  // 视口与其他 compact 用例同取 360，不去钉 MORE_ACTIONS_MEDIA 的断点值。
  await page.setViewportSize({ width: 360, height: 800 });
  await installBridge(page);
  const errors = await openPage(page);
  await page.locator("#messageDelayInput").fill("75");
  await page.locator("#moreActionsBtn").click();
  await expect(page.locator("#moreActionsMenu")).toBeVisible();

  await page.locator("#refreshBtn").click();
  await expect(page.locator("#refreshBtn")).toHaveClass(/is-armed/);
  await expect(page.locator("#moreActionsMenu")).toBeVisible();
  await expect(page.locator("#toast")).toContainText("再点一次");

  const configGets = () =>
    page.evaluate(
      () =>
        window.__bridgeCalls.filter(
          (call) => call.method === "GET" && call.endpoint === "config",
        ).length,
    );
  const before = await configGets();
  await page.locator("#refreshBtn").click();
  await expect.poll(configGets).toBe(before + 1);
  await expect(page.locator("#refreshBtn")).not.toHaveClass(/is-armed/);
  expect(errors).toEqual([]);
});

test("saving reveals the folded panel that holds the offending field", async ({ page }) => {
  // vision 的数值字段在收起的 <details> 里。折叠时 focus() 对不可见元素无效，
  // 用户只看到 "部分数值超出允许范围" 的 toast，找不到是哪个字段。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page);
  const errors = await openPage(page);
  await expect(page.locator("#sec-vision")).not.toHaveAttribute("open", "");
  await page.locator("#visionMaxImagesInput").evaluate((el) => {
    el.value = "9"; // max = 5
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.locator("#saveTopBtn").click();
  await expect(page.locator("#toast")).toContainText("超出允许范围");
  await expect(page.locator("#sec-vision")).toHaveAttribute("open", "");
  await expect(page.locator("#visionMaxImagesInput")).toBeFocused();
  await expect(page.locator("#visionMaxImagesInputError")).toBeVisible();
  expect(errors).toEqual([]);
});

test("a refresh withheld by pending edits says so instead of claiming success", async ({ page }) => {
  // loadConfig 被协调器拦下时返回 false，但 doRefresh 无条件报「已刷新为
  // 最新配置」。用户以为读到了磁盘上的新配置，随后保存又把他的编辑覆盖上去。
  await installBridge(page, { refreshConfigPending: true });
  const errors = await openPage(page);
  await page.locator("#refreshBtn").click();
  await page.locator("#refreshBtn").click();
  await expect
    .poll(() => page.evaluate(() => typeof window.__resolveRefreshConfig))
    .toBe("function");
  // 请求在途时编辑：响应到达后 canApplyLoad 为 false（editEpoch 已推进）。
  await page.locator("#messageDelayInput").fill("80");
  await page.evaluate(() => window.__resolveRefreshConfig());
  await expect(page.locator("#toast")).toContainText("已保留当前内容");
  await expect(page.locator("#toast")).not.toContainText("已刷新为最新配置");
  await expect(page.locator("#messageDelayInput")).toHaveValue("80");

  // 对照：干净的刷新仍必须报成功，否则这条会被"永远不报成功"满足。
  await page.locator("#saveTopBtn").click();
  await expect(page.locator("#navSaveState")).toHaveText("已保存");
  await page.evaluate(() => {
    window.__bridgeState.refreshConfigPending = false;
  });
  await page.locator("#refreshBtn").click();
  await expect(page.locator("#toast")).toHaveText("已刷新为最新配置");
  expect(errors).toEqual([]);
});

test("a refresh finishing under an in-flight save leaves the refresh held", async ({ page }) => {
  // 刷新尾段会经 applyConfigPayload → setSaving(false) 回到这套按钮的唯一判据上；
  // 那时 doRefresh 的 finally 若直写 disabled = false，就会在保存还在途时把刷新
  // 放回可用，用户的下一次点击被 doRefresh 的 savingConfig 守卫静默丢弃。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { refreshConfigPending: true, saveMode: "pending" });
  const errors = await openPage(page);
  await page.locator("#refreshBtn").click();
  await expect
    .poll(() => page.evaluate(() => typeof window.__resolveRefreshConfig))
    .toBe("function");
  // 刷新在途时发起保存：保存 POST 挂起，savingConfig 一直保持 true。
  await page.locator("#saveTopBtn").click();
  await expect(page.locator("#saveTopBtn")).toBeDisabled();
  await page.evaluate(() => window.__resolveRefreshConfig());
  await expect(page.locator("#toast")).toHaveText("已刷新为最新配置");
  await expect(page.locator("#refreshBtn")).toBeDisabled();
  expect(errors).toEqual([]);
});

test("switching a provider input mode alone does not mark the form dirty", async ({ page }) => {
  // 只是换输入方式（列表 ↔ 手动）不改配置值，却留下假的未保存标记，
  // 用户被迫为一次无改动的点击保存一次。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { config: { judge_provider_id: "provider-a" } });
  const errors = await openPage(page);
  await expect(page.locator("#navSaveState")).toHaveText("已同步");

  await page.locator("#providerManualBtn").click();
  await expect(page.locator("#judgeProviderInput")).toBeVisible();
  await expect(page.locator("#judgeProviderInput")).toHaveValue("provider-a");
  await expect(page.locator("#navSaveState")).toHaveText("已同步");

  await page.locator("#providerManualBtn").click();
  await expect(page.locator("#judgeProviderSelect")).toBeVisible();
  await expect(page.locator("#judgeProviderSelect")).toHaveValue("provider-a");
  await expect(page.locator("#navSaveState"), "仅切模式不应标脏").toHaveText("已同步");

  // 对照：真正改了值仍必须标脏，否则这条会被"永不标脏"满足。
  await page.locator("#providerManualBtn").click();
  await page.locator("#judgeProviderInput").fill("provider-b");
  await expect(page.locator("#navSaveState")).toHaveText("有未保存改动");
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
  const errors = await openPage(page);
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
  const errors = await openPage(page);
  await expect.poll(() => page.evaluate(() => typeof window.__resolveTheme)).toBe("function");
  await page.locator("#themeToggle").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.evaluate(() => window.__resolveTheme());
  await page.waitForTimeout(80);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  expect(await page.evaluate(() => localStorage.getItem("selfreply-theme"))).toBe("light");
  expect(errors).toEqual([]);
});

test("two quick theme clicks land in click order, not arrival order", async ({ page }) => {
  // 模拟首笔主题写入晚于第二笔，最终状态仍须符合最后一次点击。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { theme: "light", themePostPending: true });
  const errors = await openPage(page);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

  await page.locator("#themeToggle").click();
  await expect
    .poll(() => page.evaluate(() => typeof window.__resolveThemePost))
    .toBe("function");
  // 首笔仍在途时再点一次：light → dark → auto，最后一次意图是跟随系统。
  await page.locator("#themeToggle").click();
  expect(await page.evaluate(() => document.documentElement.hasAttribute("data-theme"))).toBe(
    false,
  );

  await page.evaluate(() => window.__resolveThemePost());
  await expect.poll(() => page.evaluate(() => window.__appliedThemeWrites.length)).toBe(2);
  const applied = await page.evaluate(() =>
    window.__appliedThemeWrites.map((write) => write.theme),
  );
  expect(applied.at(-1)).toBe("auto");
  expect(errors).toEqual([]);
});

test("dim and bold clicks never submit the theme field", async ({ page }) => {
  // 压暗和粗体只提交自身偏好，避免用未加载的默认主题覆盖服务端选择。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { themePending: true, theme: "dark", dim: false, bold: false });
  const errors = await openPage(page);
  await expect.poll(() => page.evaluate(() => typeof window.__resolveTheme)).toBe("function");
  await page.locator("#dimBtn").click();
  await expect(page.locator("html")).toHaveClass(/dimmed/);
  // GET ui/theme 仍在途：此刻 currentTheme() 为 auto，正是缺陷触发窗口。
  expect(
    await page.evaluate(() => document.documentElement.hasAttribute("data-theme")),
  ).toBe(false);
  await page.evaluate(() => window.__resolveTheme());
  await page.waitForTimeout(80);

  const themePosts = await page.evaluate(() =>
    window.__bridgeCalls
      .filter((call) => call.method === "POST" && call.endpoint === "ui/theme")
      .map((call) => call.body),
  );
  expect(themePosts.length).toBeGreaterThan(0);
  for (const body of themePosts) {
    expect(body.dim).toBe(true);
    expect("theme" in body).toBe(false);
  }
  // 服务端迟到的 dark 仍应生效（用户没点过主题，不得被当成 auto 覆盖）。
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect(errors).toEqual([]);
});

test("a dim click during a pending theme GET keeps the untouched sibling from the server", async ({ page }) => {
  // GET 在途时保留用户点击；尚未触碰的兄弟字段继续采用服务端值。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { themePending: true, theme: "dark", dim: true, bold: true });
  const errors = await openPage(page);
  await expect.poll(() => page.evaluate(() => typeof window.__resolveTheme)).toBe("function");
  await page.locator("#dimBtn").click();
  await expect(page.locator("html")).toHaveClass(/dimmed/);
  // 点压暗只提交 dim：未动过的 bold 不得出现在请求体里（服务端保持原值）。
  const afterDim = await page.evaluate(() =>
    window.__bridgeCalls
      .filter((call) => call.method === "POST" && call.endpoint === "ui/theme")
      .map((call) => call.body),
  );
  expect(afterDim.length).toBeGreaterThan(0);
  for (const body of afterDim) expect(body).toEqual({ dim: true });

  await page.evaluate(() => window.__resolveTheme());
  await page.waitForTimeout(80);
  // 服务端的 bold=true 必须落地：用户只动过压暗，粗体不是他的选择。
  await expect(page.locator("html")).toHaveClass(/bold-text/);
  await expect(page.locator("#boldBtn")).toHaveClass(/active/);

  // 再点一次粗体：dim 与 bold 现在都被用户动过，两个键都要提交，取值等于
  // 当前渲染态（dim 被那一下点亮为 true，服务端的 bold=true 已落地后点成 false）。
  await page.locator("#boldBtn").click();
  const bodies = await page.evaluate(() =>
    window.__bridgeCalls
      .filter((call) => call.method === "POST" && call.endpoint === "ui/theme")
      .map((call) => call.body),
  );
  expect(bodies[bodies.length - 1]).toEqual({ dim: true, bold: false });
  expect(errors).toEqual([]);
});

test("the theme toggle announces the restored theme on a fresh load", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { theme: "auto" });
  const errors = await openPage(page);
  // 首屏 restoreTheme 的同值短路会跳过 applyTheme，按钮就只剩 index.html 里的
  // 静态 aria-label，读屏听不到当前档位（点过一次才出现「当前：」）。
  await expect(page.locator("#themeToggle")).toHaveAttribute(
    "aria-label",
    "切换主题，当前：跟随系统",
  );
  expect(errors).toEqual([]);
});

test("the theme toggle names each of the three theme states in the label", async ({ page }) => {
  // 可见标签和 aria-label 共用主题名称，首帧缓存还原也保持一致。
  await page.setViewportSize({ width: 1440, height: 1000 });
  const errors = await openPage(await installThemeCyclePage(page));
  const label = page.locator("#themeToggle .theme-label");
  const cases = [
    { theme: "auto", text: "跟随系统", aria: "切换主题，当前：跟随系统" },
    { theme: "light", text: "慈爱之惠", aria: "切换主题，当前：浅色 · 慈爱之惠" },
    { theme: "dark", text: "审判之司", aria: "切换主题，当前：深色 · 审判之司" },
  ];
  for (const [index, expectation] of cases.entries()) {
    if (index > 0) await page.locator("#themeToggle").click();
    await expect(page.locator("#themeToggle .theme-label")).toHaveText(expectation.text);
    await expect(page.locator("#themeToggle")).toHaveAttribute(
      "aria-label",
      expectation.aria,
    );
    // data-theme 与标签同源：属性正确而标签滞后说明两条路径分叉了。
    const attribute = await page.evaluate(() =>
      document.documentElement.getAttribute("data-theme"),
    );
    expect(attribute).toBe(expectation.theme === "auto" ? null : expectation.theme);
  }
  expect(errors).toEqual([]);
});

// 安装一个由 localStorage 决定首帧主题的桥：首帧 HTML 只还原 localStorage，
// 剩余档位由 applyTheme 写入，正好覆盖冷启动与点击两条路径。
async function installThemeCyclePage(page) {
  await installBridge(page, { theme: "auto", dim: false, bold: false });
  await page.addInitScript(() => localStorage.setItem("selfreply-theme", "auto"));
  return page;
}

test("the theme toggle still submits the theme field", async ({ page }) => {
  // 与上一条互为对照：主题按钮是唯一该提交 theme 的入口，删掉字段即回归。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { theme: "dark", dim: false, bold: false });
  const errors = await openPage(page);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.locator("#themeToggle").click();
  // THEME_CYCLE = auto/light/dark：dark 的下一档是 auto（属性被移除）。
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", /.*/);
  const themePosts = await page.evaluate(() =>
    window.__bridgeCalls
      .filter((call) => call.method === "POST" && call.endpoint === "ui/theme")
      .map((call) => call.body),
  );
  expect(themePosts.at(-1).theme).toBe("auto");
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

  // 菜单动作保持横向单行。宽高比例避免把断言绑定到具体像素。
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
  await expect(page.locator("#moreActionsBtn")).toBeHidden();
  await expect(page.locator("#moreActionsBtn")).not.toHaveAttribute("aria-expanded");
  await expect(page.locator("#moreActionsMenu")).toBeVisible();
  expect(errors).toEqual([]);
});

test("current sidenav link keeps its focus ring below the desktop breakpoint", async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 1000 });
  await installBridge(page);
  const errors = await openPage(page);
  const link = page.locator(".sidenav-link.is-current");
  await link.focus();
  await expect(link).toHaveCSS("box-shadow", /rgb\(54,\s*65,\s*109\)/);
  await page.locator("body").click({ position: { x: 8, y: 8 } });
  await expect(link).toHaveCSS("box-shadow", "none");
  expect(errors).toEqual([]);
});

test("module load failure surfaces a refresh hint", async ({ page }) => {
  await page.route("**/app.js", (route) => route.abort());
  await page.clock.install();
  await page.goto(`${baseUrl}${PAGE_PATH}`);
  await page.clock.runFor(8000);
  await expect(page.locator("#boot")).toHaveClass(/is-failed/);
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
  // 摘要也归入 aria-describedby：「存在 N 处重复」与超上限告警只存在于那个 span，
  // 不引用它的话读屏用户拿不到这两句。
  await expect(page.locator("#whitelistInput")).toHaveAttribute(
    "aria-describedby",
    "whitelistError whitelistSummary",
  );
  await expect(page.locator("#whitelistInput")).toBeFocused();
  expect(errors).toEqual([]);
});

test("invalid whitelist never steals focus on input or blur", async ({ page }) => {
  // 输入和失焦校验保持用户焦点，只有保存失败才定位非法白名单。
  await installBridge(page);
  const errors = await openPage(page);

  await page.locator("#whitelistInput").fill('bad"quote');
  await expect(page.locator("#whitelistError")).toBeVisible();
  await expect(page.locator("#whitelistInput")).toHaveAttribute("aria-invalid", "true");

  // Tab 必须能离开该字段（此前 activeElement 会被拽回 whitelistInput）。
  // 连按两次：一次让 whitelistInput 失焦并触发 blur 校验，一次继续前移；
  // 若 blur 仍抢焦点，第二次 Tab 后焦点会回到该字段。
  await page.keyboard.press("Tab");
  await expect(page.locator("#whitelistInput")).not.toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.locator("#whitelistInput")).not.toBeFocused();

  // 点页面其他可命中区域同样不得被抢回：用顶部刷新按钮（无 label 包裹、
  // 不依赖滚动命中），点击会把焦点移走并让 whitelistInput 失焦。
  await page.locator("#refreshBtn").click();
  await expect(page.locator("#whitelistInput")).not.toBeFocused();
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

test("a sidenav click moves the hash and the single aria-current without observers", async ({ page }) => {
  // 这条只守点击路径本身：IntersectionObserver 在滚动时也会改 aria-current，
  // 不显式禁用的话「加载后把某个链接点亮」的任一实现都能让断言通过。禁用后
  // 只有点击能改变状态，同时覆盖 hash 同步与唯一性。
  await page.addInitScript(() => {
    delete window.IntersectionObserver;
  });
  await page.setViewportSize({ width: 900, height: 1000 });
  await installBridge(page);
  const errors = await openPage(page);
  const target = page.locator('.sidenav-link[data-target="sec-triggers"]');
  await expect(page.locator('.sidenav-link[data-target="selfStat"]')).toHaveAttribute(
    "aria-current",
    "location",
  );

  await target.click();

  await expect(target).toHaveAttribute("aria-current", "location");
  await expect(page).toHaveURL(/#sec-triggers$/);
  expect(errors).toEqual([]);
  // 唯一性：aria-current 同时落在两个链接上，读屏用户就分不清当前位置。
  const currents = await page.evaluate(() =>
    Array.from(document.querySelectorAll(".sidenav-link"))
      .filter((link) => link.getAttribute("aria-current") === "location")
      .map((link) => link.dataset.target),
  );
  expect(currents).toEqual(["sec-triggers"]);
  // is-current 与 aria-current 同源：只有一处更新会让视觉态与读播报分叉。
  await expect(page.locator(".sidenav-link.is-current")).toHaveCount(1);
});

test("backend field errors paint the offending number control", async ({ page }) => {
  await installBridge(page, { saveMode: "field-error" });
  const errors = await openPage(page);
  await page.evaluate(() =>
    document.querySelector('[data-config-key="cooldown_sec"]').scrollIntoView());

  await page.locator("#cooldownInput").fill("90");
  await page.locator("#saveTopBtn").click();
  // 焦点必须落到出错的输入框，否则用户只能靠 toast 猜是哪个框。
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
  // 移动端脏反馈由 #mobileSaveState 文案承担（容器上的 is-dirty 是空转类，已删）。
  await expect(page.locator("#mobileSaveState")).toHaveText("已同步");

  await page.locator("#decisionPromptInput").scrollIntoViewIfNeeded();
  await page.locator("#decisionPromptInput").fill("移动端改的提示词");
  await expect(page.locator("#mobileSaveState")).toHaveText("有未保存改动");

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
  // 保存成功后脏提示必须被覆盖，否则用户看到"已保存"与"有未保存改动"并存。
  await expect(page.locator("#mobileSaveState")).not.toHaveText("有未保存改动");
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

// 对比度实测：把令牌塞进临时元素取计算值，再按 WCAG 相对亮度算比值。light-dark()
// 由 color-scheme 决定，所以每个令牌都要在明暗两种主题下各取一次。tokens 的值写
// null 表示直接取文字色；写另一个令牌名表示该文字色压在它的半透明底上（--warn），
// 先按 alpha 压到不透明底再算。
const measureContrast = (page, tokens, surfaces) =>
  page.evaluate(
    ({ tokens, surfaces }) => {
      const probe = (variable, property) => {
        const el = document.createElement("div");
        el.style.setProperty(property, `var(${variable})`);
        document.body.appendChild(el);
        const value = getComputedStyle(el).getPropertyValue(property);
        el.remove();
        return value.trim();
      };
      const channels = (color) => color.match(/[\d.]+/g).map(Number);
      const luminance = (color) => {
        const [r, g, b] = channels(color).slice(0, 3);
        const channel = (value) => {
          const scaled = value / 255;
          return scaled <= 0.03928 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4;
        };
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
      };
      const composite = (top, bottom) => {
        const source = channels(top);
        const base = channels(bottom).slice(0, 3);
        const alpha = source.length > 3 ? source[3] : 1;
        const mixed = source.slice(0, 3).map((value, index) =>
          Math.round(value * alpha + base[index] * (1 - alpha))
        );
        return `rgb(${mixed.join(", ")})`;
      };
      const rows = [];
      const push = (theme, token, foreground, background) => {
        const first = luminance(foreground);
        const second = luminance(background);
        const [bright, dim] = [first, second].sort((a, b) => b - a);
        rows.push({ theme, token, ratio: (bright + 0.05) / (dim + 0.05) });
      };
      for (const theme of ["light", "dark"]) {
        document.documentElement.setAttribute("data-theme", theme);
        for (const surface of surfaces) {
          const background = probe(surface, "background-color");
          for (const [token, over] of Object.entries(tokens)) {
            push(
              theme,
              token,
              probe(token, "color"),
              over ? composite(probe(over, "background-color"), background) : background,
            );
          }
        }
      }
      return rows;
    },
    { tokens, surfaces },
  );

test("hint tokens clear WCAG AA on both themes and every surface", async ({ page }) => {
  // style.css 的 --faint / --warn 注释各自手算了对比度，但注释里的数字会随令牌
  // 漂移：改浅一档就跌破 AA，而全套用例照绿，小字号提示文字最先不可读。
  // 这里取实际计算值复算，不信任注释。--warn 是武装态刷新按钮的文字色，底是
  // 半透明 --warn-soft，因此按 alpha 压到每种不透明底上再算。
  await openPage(page);
  // --muted 是 13px 正文（.muted），按正文级 AA 4.5:1 一并实测：
  // 它不在 --faint 的 11-12px 手算口径内，遗漏会让调暗一档静默跌破。
  const measured = await measureContrast(
    page,
    { "--faint": null, "--muted": null, "--warn": "--warn-soft" },
    ["--surface", "--surface-2", "--surface-3", "--bg"],
  );

  expect(measured).toHaveLength(24);
  for (const { theme, token, ratio } of measured) {
    // 4.5:1 是正文级 AA（11-12px 提示文字与武装态按钮文字都属于正文级）。
    expect(ratio, `${theme} 主题 ${token} 的对比度`).toBeGreaterThanOrEqual(4.5);
  }
});

test("functional text tokens clear WCAG AA on both themes and base surfaces", async ({ page }) => {
  // 这四组是 11-17px 的文字色：--info 是读数标签（.readout.is-info）、--accent-text
  // 是链接与强调文字、--ok 是保存态（.save-state.is-ok）、--text-soft 是面板正文。
  // 它们不经过 --faint 的手算注释，调浅一档同样跌破正文级 AA 而全套用例照绿。
  await openPage(page);
  const measured = await measureContrast(
    page,
    { "--info": null, "--accent-text": null, "--ok": null, "--text-soft": null },
    ["--surface", "--surface-2"],
  );

  expect(measured).toHaveLength(16);
  for (const { theme, token, ratio } of measured) {
    // 4.5:1 是正文级 AA：这四组令牌落在最小 11.5px 的读数标签与正文上。
    expect(ratio, `${theme} 主题 ${token} 的对比度`).toBeGreaterThanOrEqual(4.5);
  }
});

test("spirit token clears WCAG AA on both themes and card surfaces", async ({ page }) => {
  // 主题图标色使用正文级 AA，核对明暗两侧实际承载图标的卡片底色。
  await openPage(page);
  const measured = await measureContrast(page, { "--spirit": null }, ["--surface", "--surface-2"]);

  expect(measured).toHaveLength(4);
  for (const { theme, token, ratio } of measured) {
    expect(ratio, `${theme} 主题 ${token} 的对比度`).toBeGreaterThanOrEqual(4.5);
  }
});

test("ornament token clears WCAG AA on the surfaces it actually sits on", async ({ page }) => {
  // --ornament 只出现在 sidenav 分组标题与图标，容器底色是 --bg / --surface，故不
  // 断言 --surface-2：该令牌不落在那个底上（暗色侧实测 4.49，差 0.01 就红），硬加
  // 会逼着为过数字改坏设计。
  await openPage(page);
  const measured = await measureContrast(page, { "--ornament": null }, ["--bg", "--surface"]);

  expect(measured).toHaveLength(4);
  for (const { theme, token, ratio } of measured) {
    expect(ratio, `${theme} 主题 ${token} 的对比度`).toBeGreaterThanOrEqual(4.5);
  }
});

test("both colour themes resolve the shadow tokens", async ({ page }) => {
  // 阴影令牌是多段颜色列表，light-dark() 接不了，只能浅深各一份。少一份时该半边
  // 所有 box-shadow 在计算值阶段落为 none，而颜色/对比度类断言看不出来。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { theme: "light" });
  const errors = await openPage(page);
  const read = () =>
    page.evaluate(() => ({
      btn: getComputedStyle(document.querySelector("#saveTopBtn")).boxShadow,
      card: getComputedStyle(document.querySelector(".master")).boxShadow,
      nav: getComputedStyle(document.querySelector(".sidenav")).boxShadow,
    }));
  for (const theme of ["light", "dark"]) {
    if (theme === "dark") {
      await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
    }
    const shadows = await read();
    for (const [name, value] of Object.entries(shadows))
      expect(value, `${theme} 主题下 ${name} 阴影丢失`).not.toBe("none");
  }
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

test("topbar does not self-toggle its stuck state near the scroll threshold", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 520 });
  await installBridge(page);
  const errors = await openPage(page);
  const topbar = page.locator(".topbar");
  await expect(topbar).toBeVisible();

  // 顶栏占位高度变化会触发滚动锚定并反过来改变粘附状态，须保持高度稳定。
  const flipReport = await page.evaluate(async () => {
    const bar = document.querySelector(".topbar");
    // 反复穿过阈值，再停在阈值带内不干预，统计 class 自发放映的次数。
    for (const y of [0, 8, 9, 8, 7, 9, 0, 10, 6, 12, 5, 8, 7, 9]) {
      window.scrollTo(0, y);
      await new Promise((resolve) => requestAnimationFrame(() => resolve()));
    }
    let flips = 0;
    let previous = bar.classList.contains("is-stuck");
    const started = performance.now();
    while (performance.now() - started < 1200) {
      if (bar.classList.contains("is-stuck") !== previous) {
        previous = bar.classList.contains("is-stuck");
        flips += 1;
      }
      await new Promise((resolve) => setTimeout(resolve, 2));
    }
    return { flips, scrollY: window.scrollY };
  });

  expect(flipReport.flips, `topbar 在阈值附近自激翻转 ${flipReport.flips} 次`).toBe(0);

  // 状态反馈本身必须保留，否则这条守卫会让 is-stuck 退化成死类。
  await expect(topbar).toHaveClass(/is-stuck/);
  await expect(topbar).toHaveCSS("border-bottom-color", "rgba(45, 52, 75, 0.14)");
  expect(errors).toEqual([]);
});

test("whitelist count and summary agree on the same deduplicated input", async ({
  page,
}) => {
  // 顶栏读数的标签是「生效会话」，与下方摘要的「共 N 条」是同一事实
  // 的两种呈现：后端把裸群号与其群 UMO 视为同一会话（utils.session_whitelisted）。
  // 计数若用未去重的 parseWhitelist().length，`12347` + 对应 UMO 会让顶栏读 2、
  // 摘要读 1，两条读数都是 aria-live="polite"，读屏连续播报两个互相抵消的数字。
  await installBridge(page);
  const errors = await openPage(page);
  const read = async () => {
    await page.evaluate(() => {
      document
        .getElementById("whitelistInput")
        .dispatchEvent(new Event("input", { bubbles: true }));
    });
    // 摘要和计数在防抖后更新，避免逐键播报。
    await page.waitForTimeout(500);
    return page.evaluate(() => ({
      count: document.getElementById("whitelistCount").textContent,
      summary: document.getElementById("whitelistSummary").textContent,
    }));
  };

  // 折叠：裸群号与它的群 UMO 是同一个会话，计数必须按去重后的算
  await page.locator("#whitelistInput").fill("12347\nqq:GroupMessage:12347");
  const collapsed = await read();
  expect(collapsed.count).toBe("1");
  expect(collapsed.summary).toContain("共 1 条");
  expect(collapsed.summary).toContain("存在 1 处重复");

  // 不同会话保持独立计数。
  await page.locator("#whitelistInput").fill("111\nqq:GroupMessage:222");
  const two = await read();
  expect(two.count).toBe("2");
  expect(two.summary).toContain("共 2 条");

  // 完全重复的同一项同样按去重后计数
  await page.locator("#whitelistInput").fill("12347\n12347");
  const dup = await read();
  expect(dup.count).toBe("1");
  expect(dup.summary).toContain("共 1 条");

  expect(errors).toEqual([]);
});

test("the whitelist live region is written only when its text changes", async ({
  page,
}) => {
  // #whitelistSummary 是 aria-live="polite"，却挂在 input 事件上逐键写。
  // textContent 赋值恒会替换文本节点：逐字相同也照样把整句摘要重新排进播报
  // 队列，读屏用户粘一段 20 条的名单会听到同一句话被每个按键打断一次，
  // 还与 #whitelistError 的 role="alert" 交错。aria-describedby 已在聚焦时
  // 给出这两句，live region 只该播报“变了”的那几次。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page);
  const errors = await openPage(page);

  const watch = () =>
    page.evaluate(() => {
      const summary = document.getElementById("whitelistSummary");
      window.__summaryWrites = [];
      const observer = new MutationObserver(() => {
        window.__summaryWrites.push(summary.textContent);
      });
      observer.observe(summary, {
        childList: true,
        characterData: true,
        subtree: true,
      });
      window.__stopSummaryWatch = () => observer.disconnect();
    });
  const writes = () =>
    page.evaluate(() => {
      window.__stopSummaryWatch();
      return window.__summaryWrites.slice();
    });

  await page.locator("#whitelistInput").fill("");
  await watch();
  await page.locator("#whitelistInput").pressSequentially("1,2,3,4,7", { delay: 0 });
  // 防抖窗口内不得写入：同步实现会在这里写到 5 句（每逗号一句）。
  const during = await page.evaluate(() => window.__summaryWrites.slice());
  expect(during, `防抖窗口内写入了 ${during.length} 次：${during.join(" | ")}`).toEqual([]);
  await page.waitForTimeout(600);
  const seen = await writes();
  // 停顿后只播报一次最终摘要；同一句写两遍同样是重复播报。
  expect(seen, `停顿后写入：${seen.join(" | ")}`).toEqual(["共 5 条（5 个纯群号）"]);
  await expect(page.locator("#whitelistSummary")).toHaveText("共 5 条（5 个纯群号）");
  await expect(page.locator("#whitelistCount")).toHaveText("5");

  // 反向锚：摘要真的变了仍必须写，否则这条会被「恒不写」满足。
  await watch();
  await page.evaluate(async () => {
    const input = document.getElementById("whitelistInput");
    input.value = "only-one";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    // 防抖窗口内不写；等到窗口过去再看写入。
    await new Promise((resolve) => setTimeout(resolve, 550));
  });
  expect(await writes()).toEqual(["共 1 条（1 个群键）"]);
  expect(errors).toEqual([]);
});

test("save-state live regions are not rewritten with identical text", async ({
  page,
}) => {
  // #configSaveState / #mobileSaveState 也是 aria-live="polite"，而 setDirty
  // 挂在每个 input 上：逐字相同也写就是逐键重排同一句播报。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page);
  const errors = await openPage(page);

  await page.evaluate(() => {
    const target = document.getElementById("configSaveState");
    window.__saveWrites = [];
    const observer = new MutationObserver(() => {
      window.__saveWrites.push(target.textContent);
    });
    observer.observe(target, { childList: true, characterData: true, subtree: true });
    window.__stopSaveWatch = () => observer.disconnect();
  });
  await page.locator("#messageDelayInput").pressSequentially("123", { delay: 0 });
  await page.waitForTimeout(100);
  const seen = await page.evaluate(() => {
    window.__stopSaveWatch();
    return window.__saveWrites.slice();
  });
  expect(new Set(seen).size, `保存状态被重复写入：${seen.join(" | ")}`).toBe(seen.length);
  await expect(page.locator("#configSaveState")).toHaveText("有未保存改动");
  expect(errors).toEqual([]);
});

test("a successful refresh clears stale field errors and the failed-save state", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installBridge(page, { saveMode: "fail-once" });
  const errors = await openPage(page);
  const refresh = async () => {
    // 两次点击是刷新入口的确认手势（首次仅待确认）。
    await page.locator("#refreshBtn").click();
    await page.locator("#refreshBtn").click();
  };

  // 越界输入先画出错误；刷新拿到服务端真值后红字与 aria-invalid 必须一并抹掉。
  await page.locator("#messageDelayInput").fill("1");
  await expect(page.locator("#messageDelayInputError")).toBeVisible();
  await refresh();
  await expect(page.locator("#messageDelayInputError")).toBeHidden();
  await expect(page.locator("#messageDelayInput")).not.toHaveAttribute("aria-invalid", "true");
  await expect(page.locator("#messageDelayInput")).not.toHaveValue("1");

  // 失败的保存留下「保存失败」：此后不再编辑（否则状态会先变成脏），
  // 只靠一次成功刷新把它清掉——顶标说「已同步」而正文还写「保存失败」会自相矛盾。
  await page.locator("#messageDelayInput").fill("75");
  await page.locator("#saveTopBtn").click();
  await expect(page.locator("#configSaveState")).toHaveText("保存失败");
  await expect(page.locator("#saveTopBtn")).toHaveClass(/is-failed/);
  await refresh();
  await expect(page.locator("#configSaveState")).toHaveText("");
  await expect(page.locator("#saveTopBtn")).not.toHaveClass(/is-failed/);
  await expect(page.locator("#navSaveState")).toHaveText("已同步");
  expect(errors).toEqual([]);
});

test("fallback queue restores focus to the moved item for repeat activation", async ({ page }) => {
  // 每次改动都整表重渲染，焦点必须跟随移动后的项目，否则第二次操作无法连续完成。
  await installBridge(page, {
    providers: [
      { id: "p1", label: "P1" },
      { id: "p2", label: "P2" },
      { id: "p3", label: "P3" },
    ],
    config: { proactive_fallback_provider_ids: ["p1", "p2", "p3"] },
  });
  await openPage(page);

  const rows = () => page.locator("#proactiveFallbackList .fallback-item span");
  expect(await rows().allTextContents()).toEqual(["1. P1", "2. P2", "3. P3"]);

  // 第二次激活重渲染后重新聚焦的下移按钮，应继续移动同一个项目。
  const downOnFirst = page
    .locator("#proactiveFallbackList .fallback-item")
    .nth(0)
    .getByRole("button", { name: /^下移/ });
  await downOnFirst.click();
  const afterFirst = await rows().allTextContents();
  expect(afterFirst).toEqual(["1. P2", "2. P1", "3. P3"]);
  // 第一次点击后焦点应跟着被移动的那一项，而不是掉到 body。
  const focused = await page.evaluate(() => {
    const node = document.activeElement;
    return node?.getAttribute?.("aria-label") || node?.tagName || "";
  });
  expect(focused).toMatch(/^下移/);

  await page.evaluate(() => {
    const node = document.activeElement;
    node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  expect(await rows().allTextContents()).toEqual(["1. P2", "2. P3", "3. P1"]);
});

test("removing a fallback provider keeps focus on a useful next control", async ({ page }) => {
  await installBridge(page, {
    providers: [
      { id: "p1", label: "P1" },
      { id: "p2", label: "P2" },
      { id: "p3", label: "P3" },
    ],
    config: { proactive_fallback_provider_ids: ["p1", "p2", "p3"] },
  });
  await openPage(page);

  const rows = page.locator("#proactiveFallbackList .fallback-item");
  await rows.nth(1).getByRole("button", { name: /^移除/ }).click();
  await expect(rows.nth(1).getByRole("button", { name: "移除第 2 项 P3" })).toBeFocused();

  await rows.nth(1).getByRole("button", { name: /^移除/ }).click();
  await expect(rows.nth(0).getByRole("button", { name: "移除第 1 项 P1" })).toBeFocused();

  await rows.nth(0).getByRole("button", { name: /^移除/ }).click();
  await expect(page.locator("#proactiveFallbackSelect")).toBeFocused();
});

test("adding the last fallback moves focus off the disabling button", async ({ page }) => {
  // 加满后「添加」被禁用，焦点若留在上面等于掉到 body，键盘用户要从头 Tab。
  await installBridge(page, {
    providers: [
      { id: "p1", label: "P1" },
      { id: "p2", label: "P2" },
    ],
    config: {},
  });
  await openPage(page);

  for (const id of ["p1", "p2"]) {
    await page.locator("#proactiveFallbackSelect").selectOption(id);
    await page.locator("#proactiveFallbackAddBtn").click();
  }

  await expect(page.locator("#proactiveFallbackAddBtn")).toBeDisabled();
  const active = await page.evaluate(() => document.activeElement?.id || "");
  expect(active).toBe("proactiveFallbackSelect");
});

test("generation model panel loads the configured fallback order", async ({
  page,
}) => {
  // 备用链的顺序就是模型尝试顺序，初始加载应保持后端配置顺序。
  await installBridge(page, {
    providers: [
      { id: "provider-a", label: "Provider A" },
      { id: "provider-b", label: "Provider B" },
      { id: "provider-c", label: "Provider C" },
    ],
    config: { proactive_fallback_provider_ids: ["provider-c", "provider-a"] },
  });
  const errors = await openPage(page);

  const names = await page
    .locator("#proactiveFallbackList .fallback-item span")
    .allTextContents();
  expect(names).toEqual(["1. Provider C", "2. Provider A"]);
  expect(errors).toEqual([]);
});

test("generation model panel picks providers from lists and submits every key", async ({
  page,
}) => {
  // 主动回复可以指定自己的模型、备用链与重试次数。模型都只能从列表里选：
  // 让人手填 Provider ID 等于让他记住宿主内部标识，填错时这一轮静默回落。
  await installBridge(page, {
    providers: [
      { id: "provider-a", label: "Provider A" },
      { id: "provider-b", label: "Provider B" },
      { id: "provider-c", label: "Provider C" },
    ],
    config: { proactive_provider_id: "provider-a" },
  });
  const errors = await openPage(page);

  await expect(page.locator("#sec-model")).toBeVisible();
  await expect(page.locator("#proactiveProviderSelect")).toHaveValue("provider-a");
  // 面板里不该再有手填入口。
  await expect(page.locator("#proactiveProviderInput")).toHaveCount(0);

  await page.locator("#proactiveProviderSelect").selectOption("provider-b");

  // 备用链是有序的：加两个，再把后面的挪到前面。
  for (const id of ["provider-c", "provider-a"]) {
    await page.locator("#proactiveFallbackSelect").selectOption(id);
    await page.locator("#proactiveFallbackAddBtn").click();
  }
  await expect(page.locator("#proactiveFallbackList .fallback-item")).toHaveCount(2);
  await page
    .locator("#proactiveFallbackList .fallback-item")
    .nth(1)
    .getByRole("button", { name: /^上移/ })
    .click();

  await page.locator("#proactiveRetriesInput").fill("2");
  await page.locator("#saveTopBtn").click();

  const body = await page.evaluate(() => {
    const posts = window.__bridgeCalls.filter(
      (call) => call.method === "POST" && call.endpoint === "config",
    );
    return posts[posts.length - 1]?.body || null;
  });
  expect(body).not.toBeNull();
  expect(body.proactive_provider_id).toBe("provider-b");
  expect(body.proactive_fallback_provider_ids).toEqual(["provider-a", "provider-c"]);
  expect(body.proactive_request_max_retries).toBe(2);
  expect(errors).toEqual([]);
});
