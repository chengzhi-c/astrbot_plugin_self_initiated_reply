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
    ({ config, providersFail, saveMode, theme, dim, bold, refreshConfigPending, themePending, cleanupRemoved, cleanupFail, configFail, readyFails }) => {
      const state = {
        saveMode,
        saveAttempts: 0,
        config,
        configCalls: 0,
        providersFail,
        configFail,
        refreshConfigPending,
        themePending,
        cleanupRemoved,
        cleanupFail,
        readyFails,
      };
      window.__bridgeCalls = [];
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
            return { ok: true, providers: [{ id: "provider-a", label: "Provider A" }] };
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
  // 首屏等待预算显式放宽到页面的看门狗量级（app.js BOOT_TIMEOUT_MS = 12s）。
  // 默认 5s（playwright.config 的 expect.timeout）是**断言**预算，不是加载预算：
  // 本套件绝大多数用例共用本 helper，负载高时偶发首屏超过 5s，失败点随机落在当时那条
  // 用例上（实测 12 轮全量里 2 次，分别报在 893 与 1412 两条互不相干的用例上）。
  // 这不放松任何断言，页面真加载失败时看门狗仍会隐藏 boot，随后各用例自己的
  // 读值/toast/errors 断言照旧失败。
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
  // 回归守卫：保存成功后 setDirty(false) 不推进 editEpoch。若刷新开始前表单
  // 已脏（loadStartedDirty=true），迟到响应到达时 isDirty 已回落，守卫会放行，
  // 把已保存的值覆盖回刷新请求发出时的旧快照，configRevision 也一并回退，
  // 下一次保存必撞 STALE_WRITE。
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

test("a late refresh on a form edited after the save names the pending edits", async ({ page }) => {
  // 上一条守"保存后表单已干净"的中性口径，这一条守它的另一半：保存成功后用户
  // 又改了字段。此时迟到响应携带的快照早于本次保存，必须仍被协调器作废，
  // 应用它会把用户保存过的那一版连同他的新编辑一起退回旧值。
  // toast 必须落到脏表单那句：只说"已保留当前内容"也能满足上面那条干净用例，
  // 于是"响应未应用、请保存后再刷新"这句可操作的指引在产品里彻底消失。
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
  await expect(page.locator("#providerListState")).toContainText("三个 Provider 均可手动填写");
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
  await expect(page.locator("#providerListState")).toContainText("三个 Provider 均可手动填写");

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
  // 回归守卫：GET ui/theme 在途时点主题，曾把服务端已存的 dim/bold 一并提交为
  // 当前渲染态（此刻恒为 false）后端语义是「未提交的键保持原值」，但前端每次
  // 都提交两者，于是服务端的压暗/粗体被静默抹掉。未触碰过的键不得出现在请求体里，
  // 与 theme 字段同一条规则。
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

  // 对照：用户真的点过压暗后，该次请求必须带上 dim / bold 两个键，否则本地状态
  // 与服务端 prefs 分叉（刷新后用户的点击丢失）。
  await page.evaluate(() => window.__resolveTheme());
  await expect(page.locator("html")).toHaveClass(/dimmed/); // 服务端 dim=true 已落地
  await page.locator("#dimBtn").click();
  await expect(page.locator("html")).not.toHaveClass(/dimmed/);
  const afterDim = await page.evaluate(() => ({
    body: window.__bridgeCalls
      .filter((call) => call.method === "POST" && call.endpoint === "ui/theme")
      .map((call) => call.body)
      .at(-1),
    dimmed: document.documentElement.classList.contains("dimmed"),
    bold: document.documentElement.classList.contains("bold-text"),
  }));
  // 点过之后两个键都要提交，且取值必须等于当前渲染态（服务端 bold=true 已落地）。
  expect(afterDim.body).toEqual({ dim: afterDim.dimmed, bold: afterDim.bold });
  expect(afterDim.body.dim).toBe(false);
  expect(errors).toEqual([]);
});

test("topbar height token follows the measured topbar height across breakpoints", async ({ page }) => {
  // 静态令牌（88/64/62）与实际顶栏高度一直对不上：1024px 断点内实测 83px、
  // 换行断点实测 115px。令牌被 .sidenav 的 sticky top 与 scroll-margin-top
  // 消费，脱节即侧栏被顶栏盖住、锚点标题被遮。修法是运行时把实测高度写回令牌，
  // 并在断点/换行变化后重测，只写一次的实现在窄屏仍是错的。
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
  // 窄屏顶栏换行成两行（实测 115px）而令牌仍是 62px，scroll-margin-top 只有
  // 78px：锚点跳转后分区标题落在顶栏底下（实测被遮 18px）。
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
  // （读屏会念出「判断温度 越高越发散，默认 0.2」这种长串）。
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
      judgeProvider: document.getElementById("judgeProviderInput")?.getAttribute("aria-describedby"),
    };
  });
  expect(names.decisionTemp.labelledby).toBe("decisionTempInputLabel");
  expect(names.decisionTemp.labelText).toBe("判断温度");
  expect(names.decisionTemp.describedby).toContain("decisionTempHint");
  expect(names.mentionMode.labelledby).toBe("mentionModeInputLabel");
  expect(names.mentionMode.describedby).toBe("mentionModeHint");
  expect(names.judgeProvider).toBe("providerHint");
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

  // 手动态的列宽：三个 Provider 控件必须同源。只让 judge 挂 manual 类时，
  // vision 两个字段靠 .provider-field.manual 收成单列，按钮被拉成整行宽
  // 的历史缺陷实测 373px vs judge 78px。
  // 阈值取 100px 而非精确 78px：字体栈在不同平台有毫米级差异，这里只要量级判据。
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

  // 反向锚：切回列表态必须恢复两列（防「恒单列」式的错误修复）
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
  // closeMenu。窄屏下脏表单的第一次点击只负责「武装」，提示语是「3 秒内再点一次」，
  // 但紧随其后的 closeMenu 会把菜单隐藏，第二下点不到（要先重新展开菜单），
  // 提示与可达行为矛盾。断言菜单仍可见、按钮带 is-armed，且第二下真的发出刷新。
  await page.setViewportSize({ width: 460, height: 800 });
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

test("dim and bold clicks never submit the theme field", async ({ page }) => {
  // 回归守卫：bindDimBoldButtons 曾把 currentTheme() 一并提交，而 data-theme
  // 尚未渲染出服务端主题（GET 在途 / localStorage 不可用）时它恒为 "auto"，
  // 一次压暗就把服务端已存的 dark 静默改成跟随系统。压暗/粗体只改自己那两个字段。
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

test("invalid whitelist never steals focus on input or blur", async ({ page }) => {
  // 回归守卫：validateWhitelist 曾同时挂在 input/blur 上并无条件 focus()，
  // 白名单残留一个非法条目后，鼠标点其他字段会被立刻抢回、键盘 Tab 也逃不出。
  // 交互路径不得抢焦点；保存路径的聚焦由上面那条用例单独钉住。
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

test("faint hint token clears WCAG AA on both themes and both surfaces", async ({ page }) => {
  // style.css 的 --faint 注释手算了四组对比度（浅色 5.11/4.77，深色 5.39/4.97），
  // 但没有任何断言：把令牌改浅（或改暗）一档就跌破 AA，而全套用例照绿，11-12px
  // 小字号提示文字最先不可读。这里取实际计算值复算，不信任注释里的数字。
  await openPage(page);
  const measured = await page.evaluate(() => {
    const probe = (variable, property) => {
      const el = document.createElement("div");
      el.style.setProperty(property, `var(${variable})`);
      document.body.appendChild(el);
      const value = getComputedStyle(el).getPropertyValue(property);
      el.remove();
      return value.trim();
    };
    const luminance = (color) => {
      const [r, g, b] = color.match(/[\d.]+/g).slice(0, 3).map(Number);
      const channel = (value) => {
        const scaled = value / 255;
        return scaled <= 0.03928 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
    };
    const rows = [];
    for (const theme of ["light", "dark"]) {
      document.documentElement.setAttribute("data-theme", theme);
      const faint = luminance(probe("--faint", "color"));
      for (const surface of ["--surface", "--surface-2"]) {
        const background = luminance(probe(surface, "background-color"));
        const [bright, dim] = [faint, background].sort((a, b) => b - a);
        rows.push({ theme, surface, ratio: (bright + 0.05) / (dim + 0.05) });
      }
    }
    return rows;
  });

  expect(measured).toHaveLength(4);
  for (const { theme, surface, ratio } of measured) {
    // 4.5:1 是正文级 AA（11-12px 提示文字属于正文级，不算大字号）。
    expect(ratio, `${theme} 主题 ${surface} 底的 --faint 对比度`).toBeGreaterThanOrEqual(4.5);
  }
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

test("topbar must not change its height when the stuck class toggles", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 520 });
  await installBridge(page);
  const errors = await openPage(page);
  const topbar = page.locator(".topbar");
  await expect(topbar).toBeVisible();

  // is-stuck 曾在粘附时把 padding 从 --sp-7 收到 --sp-5，令占位高度变化约 16px。
  // topbar 是首位 sticky 元素，占位高度变化会触发浏览器滚动锚定补偿、反过来改写
  // scrollY；scrollY 又决定 is-stuck 是否保留，只要高度差超过 sticky 阈值
  // （y > 8）就会自激，表现为页面在接近最上方时疯狂抖动。
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
  // 顶栏读数的标签是「生效会话」，与下方摘要的「已识别 N 个有效会话」是同一事实
  // 的两种呈现：后端把裸群号与其群 UMO 视为同一会话（utils.session_whitelisted）。
  // 计数若用未去重的 parseWhitelist().length，`12347` + 对应 UMO 会让顶栏读 2、
  // 摘要读 1，两条读数都是 aria-live="polite"，读屏连续播报两个互相抵消的数字。
  await installBridge(page);
  const errors = await openPage(page);
  const read = () =>
    page.evaluate(() => {
      const input = document.getElementById("whitelistInput");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      return {
        count: document.getElementById("whitelistCount").textContent,
        summary: document.getElementById("whitelistSummary").textContent,
      };
    });

  // 折叠：裸群号与它的群 UMO 是同一个会话，计数必须按去重后的算
  await page.locator("#whitelistInput").fill("12347\nqq:GroupMessage:12347");
  const collapsed = await read();
  expect(collapsed.count).toBe("1");
  expect(collapsed.summary).toContain("已识别 1 个有效会话");
  expect(collapsed.summary).toContain("存在 1 处重复");

  // 反向锚：两个真正不同的会话不得被折叠成 1（防「恒 1」式的错误修复）
  await page.locator("#whitelistInput").fill("111\nqq:GroupMessage:222");
  const two = await read();
  expect(two.count).toBe("2");
  expect(two.summary).toContain("已识别 2 个有效会话");

  // 完全重复的同一项同样按去重后计数
  await page.locator("#whitelistInput").fill("12347\n12347");
  const dup = await read();
  expect(dup.count).toBe("1");
  expect(dup.summary).toContain("已识别 1 个有效会话");

  expect(errors).toEqual([]);
});
