import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
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
  numberFieldError,
  renderPromptTemplateHtml,
  summarizeWhitelist,
  uniqueWhitelistItems,
  validateWhitelistLines,
} from "../pages/主动回复设置/config-form.mjs";
import { THEME_KEY } from "../pages/主动回复设置/theme.mjs";
import { configPayload } from "./fixtures/config-payload.mjs";

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
  // 正向对照：否则下面那批负例被一个「恒 false」的实现也能满足。
  assert.equal(
    isSuccessfulConfigPayload({
      ok: true,
      enabled: true,
      whitelist_sessions: [],
      config_revision: TEST_REVISION,
    }),
    true,
  );
});

test("config payload rejects a malformed config_revision", () => {
  // config_revision 是保存时的 CAS 期望值（POST body 的 base_revision）：形状不对
  // 时必须判加载失败。放宽成「只要是个字符串」会让 revision 退化为常量，乐观并发
  // 控制在最上层静默失效（两次并发保存都「成功」，后者覆盖前者）——而页面其余
  // 逻辑全部照常工作，只有这个函数变红能发现。
  const rejected = ["", "sha256:", "sha256:short", `sha256:${"A".repeat(64)}`, 123, null];
  for (const revision of rejected) {
    assert.equal(
      isSuccessfulConfigPayload({
        ok: true,
        enabled: true,
        whitelist_sessions: [],
        config_revision: revision,
      }),
      false,
      `config_revision=${JSON.stringify(revision)} 应判加载失败`,
    );
  }
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
  const registry = configIo.match(/const providerControls = \(\) => \(\{([\s\S]*?)\}\);/);
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
  assert.match(configIo, /function providerConfigKeys\(form\)/);
  assert.doesNotMatch(configIo, /PROVIDER_CONFIG_KEYS/);
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

test("browser config fixture covers every form-declared key", async () => {
  // Playwright 用这份夹具当 GET /config。缺键时 isSuccessfulConfigPayload 失败，
  // 表单一直 inert，浏览器用例会集体红。node 契约原先不读这份夹具，
  // 第四轮加 quote/skip 键后本地 CI frontend 仍绿、浏览器才爆。
  const html = await readFile(join(pageDir, "index.html"), "utf8");
  const htmlKeys = [...html.matchAll(/data-config-key="([a-z0-9_]+)"/g)].map(
    (match) => match[1],
  );
  const fixture = configPayload();
  const missing = htmlKeys.filter(
    (key) => !Object.prototype.hasOwnProperty.call(fixture, key),
  );
  assert.deepEqual(missing, [], `config-payload.mjs 缺少表单键：${missing}`);

  // 反向：夹具里的每个键都必须被页面消费（表单控件或 JS 读取），否则是
  // 后端早已删除、夹具却残留的孤儿键——浏览器测试用这份夹具当桩，
  // 残留键永远绿，漂移只有这一侧能抓。同 Python 侧
  // test_every_exposed_config_key_is_consumed_by_the_panel 的口径。
  const names = (await readdir(pageDir)).filter((name) => /\.(js|mjs)$/.test(name));
  let front = html;
  for (const name of names) {
    front += await readFile(join(pageDir, name), "utf8");
  }
  const stale = Object.keys(fixture).filter((key) => !front.includes(key));
  assert.deepEqual(
    stale,
    [],
    `config-payload.mjs 含前端零消费的孤儿键（后端可能已删）：${stale}`,
  );
});

test("CI runs the dependency-free frontend gate", async () => {
  const workflow = await readFile(join(root, ".github", "workflows", "ci.yml"), "utf8");
  // 只钉两条对"零依赖门禁还在跑"有决定意义的锚：frontend job 存在、且它执行
  // node --test 跑契约文件。此前另有 3 条（setup-node 版本、通配符字面量、
  // node --check 子串）——那些是**实现细节**：升级 action 版本或调整 glob 写法
  // 都会变红，而红的原因与被守卫的契约（前端门禁不被误删）无关。
  assert.match(workflow, /^ {2}frontend:\r?\n/m);
  assert.match(workflow, /node --test tests\/frontend_contract\.test\.mjs/);
});

test("theme localStorage key stays single-sourced with the HTML bootstrap", async () => {
  const html = await readFile(join(pageDir, "index.html"), "utf8");
  const htmlKey = html.match(/localStorage\.getItem\("([^"]+)"\)/)?.[1];
  assert.equal(htmlKey, THEME_KEY);
  assert.equal(html.split(THEME_KEY).length - 1, 1);
});

test("mobile tab groups stay in sync with the page section anchors", async () => {
  // TAB_GROUPS 把侧栏分区 id 映射到移动端 tabbar 分组。分区改名或新增而漏改
  // 映射表时，chrome.mjs 的 `TAB_GROUPS[target] || target` 兜底会静默降级为
  // "不高亮任何 tab"——不抛异常、无日志，只有窄屏肉眼可能看出。
  // 与 theme key / 主题标签同性质：跨源清单，靠这条钉在一起。
  const [html, chrome] = await Promise.all([
    readFile(join(pageDir, "index.html"), "utf8"),
    readFile(join(pageDir, "chrome.mjs"), "utf8"),
  ]);
  const body = chrome.match(/const TAB_GROUPS = \{([\s\S]*?)\n\};/)?.[1];
  assert.ok(body, "chrome.mjs 未声明 TAB_GROUPS");
  const groups = new Map(
    [...body.matchAll(/([A-Za-z_$][\w$]*|"[^"]*")\s*:\s*"([^"]*)"/g)].map((m) => [
      m[1].replace(/"/g, ""),
      m[2],
    ])
  );
  assert.ok(groups.size > 0, "TAB_GROUPS 为空");

  const sidenav = new Set(
    [...html.matchAll(/<a\b[^>]*class="sidenav-link"[^>]*data-target="([^"]+)"/g)].map(
      (m) => m[1]
    )
  );
  const tabbar = new Set(
    [...html.matchAll(/<button\b[^>]*class="mtab"[^>]*data-target="([^"]+)"/g)].map(
      (m) => m[1]
    )
  );
  assert.ok(sidenav.size > 0 && tabbar.size > 0, "index.html 缺少 data-target 锚点");
  assert.deepEqual([...groups.keys()].sort(), [...sidenav].sort());
  assert.deepEqual([...new Set(groups.values())].sort(), [...tabbar].sort());
});

test("settings page scripts only look up ids that index.html declares", async () => {
  // 页面脚本按字面量取元素（app.js 的 $()、chrome.mjs 的 getElementById）。
  // 拼错 id（或页面删掉对应元素）不抛异常：调用点普遍有 `if (el)` 守卫，用户
  // 只是静默少一块功能。实测把 whitelistSummary 拼成 whitelistSummaryTYPO 后，
  // 本文件其余契约、浏览器用例与全量 pytest 全部保持绿色（计数不写数字，
  // 写了必然随开发过时）。
  // 文件清单由目录派生（同下面的行宽守卫），新增脚本自动纳入。
  // 只做单向 JS ⊆ HTML：反向的"孤儿 id"是无害死标记，而且会在
  // <svg><use href="#…"> 与 aria-* 锚点上误报，豁免名单本身会腐烂。
  const names = (await readdir(pageDir)).filter((name) => /\.(js|mjs)$/.test(name)).sort();
  assert.ok(names.includes("app.js"), "设置页脚本清单为空或目录读错");
  const html = await readFile(join(pageDir, "index.html"), "utf8");
  const declared = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]));
  assert.ok(declared.size > 0, "index.html 未声明任何 id");

  const missing = [];
  let lookups = 0;
  for (const name of names) {
    const source = await readFile(join(pageDir, name), "utf8");
    const ids = [
      ...source.matchAll(/\$\(\s*"([^"]+)"\s*\)/g),
      ...source.matchAll(/getElementById\(\s*"([^"]+)"\s*\)/g),
    ];
    lookups += ids.length;
    for (const match of ids) {
      if (!declared.has(match[1])) missing.push(`${name} -> ${match[1]}`);
    }
  }
  assert.ok(lookups > 0, "页面脚本未按字面量取任何元素：守卫已失去对象");
  assert.deepEqual(missing.sort(), [], `index.html 缺少脚本引用的 id：${missing.join(", ")}`);
});

test("responsive breakpoints stay in sync between chrome.mjs and the stylesheet", async () => {
  // 这两处 JS 行为与 CSS 断点强耦合：更多操作菜单在 460px 上下切换折叠形态，
  // 侧栏在 1024px 以下点击后自动收起。改 CSS 漏改 JS（或反之）只在断点附近的
  // 一段宽度里表现异常，属静默错配。CSS 无法 import JS，故用守卫固定两侧，
  // 而不是引入跨语言常量（那要破坏零构建承诺）。
  const [chrome, css] = await Promise.all([
    readFile(join(pageDir, "chrome.mjs"), "utf8"),
    readFile(join(pageDir, "style.css"), "utf8"),
  ]);
  const widths = new Set([...chrome.matchAll(/max-width:\s*(\d+)px/g)].map((m) => m[1]));
  assert.ok(widths.size > 0, "chrome.mjs 未声明任何 max-width 断点");
  for (const width of widths) {
    assert.match(
      css,
      new RegExp(`@media \\(max-width: ${width}px\\)`),
      `chrome.mjs 使用 ${width}px 断点，但 style.css 没有同名 @media`
    );
  }
  // 两个已知耦合值必须在两侧都出现：即便 JS 整体删掉断点也不得静默通过。
  for (const width of ["460", "1024"]) {
    assert.ok(widths.has(width), `chrome.mjs 不再使用 ${width}px 断点`);
    assert.match(css, new RegExp(`@media \\(max-width: ${width}px\\)`));
  }
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

test("styles do not target element ids", async () => {
  // 样式不用 #id 选择器：ID 特异性(100)会压过类(10)，同一按钮的普通类规则
  // 从此改不动它，只能靠再写一条更长的 ID 规则去覆盖（此前 4 个顶栏按钮各自
  // 攒了 4–9 条 #id 规则）。id 仍是 JS 取节点的锚（getElementById 不动），
  // 只有样式改用类。
  //
  // 判据取"选择器区域"（`{` 之前的部分）而不是"全文找 # 再剔除颜色"。
  // 旧判据 `(^|[\s,{])(#[\w-]+)` 有三类漏检，均已实测：
  //   - 无空格组合符：`.a>#id`、`.a~#id`、`*#id`（`#` 前既非空白也非 `,{`）；
  //   - 属性选择器相连：`[attr]#id`；
  //   - 3–8 位纯十六进制字形 ID（如 `#abc123`）：被 `#[0-9a-fA-F]{3,8}\b`
  //     当颜色剔除。注意 9 位以上反而漏不进颜色正则（`\b` 不满足），
  //     所以区分力只在这个区间。
  // 旧判据另有一处真误报：`content: "#hash #id"` 里的 `#id` 会被当成选择器。
  // 新判据把这三点都修掉了（声明区天然落在 `{` 之后）。
  //
  // 判据仍是文本级，两处刻意写法会成为已知边界（本仓不出现，不加固——
  // 上 CSS 解析器换取这点收益不划算）：
  //   - 假阳性：选择器区的属性选择器字符串含 #，如 `[data-icon="#x"]`；
  //     同一规则内声明字符串含 `}` 且其后再有 `url(#…)`（`}` 使切分错位）。
  //   - 假阴性：`content:"/*"` 与后续 `content:"*/"` 之间的选择器会被注释
  //     剔除整体吞掉；`\23 ` 转义写法新旧判据都看不见。
  const css = await readFile(join(pageDir, "style.css"), "utf8");
  const selectorArea = css
    .replace(/\/\*[\s\S]*?\*\//g, "") // 注释整体排除
    .split("}")
    .map((chunk) => chunk.split("{")[0])
    .join("\n");
  const idSelectors = selectorArea.match(/#[A-Za-z_][\w-]*/g) || [];
  assert.deepEqual(idSelectors, [], `style.css 又用 ID 选择器做样式锚：${idSelectors}`);
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
    "quote_mode",
    "quote_probability",
    "reply_request_requires_model",
    "skip_after_direct_call",
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
    checkbox("reply_request_requires_model", false),
    checkbox("skip_after_direct_call", true),
    providerField("judge_provider_id", "judge"),
    number("decision_temperature", "0.3"),
    number("decision_timeout_sec", "21"),
    field("decision_prompt_template", "  prompt  ", { configTransform: "trim" }),
    field("quote_mode", "random"),
    number("quote_probability", "60"),
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

test("saved whitelist count reflects the server-normalized payload", async () => {
  // 服务端归一化（去重/裁剪）后计数以返回值为准：applyConfigPayload 已按
  // 归一化白名单刷新过计数，保存分支若再用提交时的 body 长度覆盖，会展示
  // 与实际生效不一致的数字。
  const classList = { add() {}, remove() {}, toggle() {} };
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
    field("whitelist_sessions", "group:a\ngroup:b", { configTransform: "whitelist" }),
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
    // 服务端把提交的两条归一化成一条（例如去重/裁剪后仅剩 group:a）。
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

  assert.equal(elements.whitelistInput.value, "group:a");
  assert.equal(elements.whitelistCount.textContent, "1");
});

test("save validation guards on whitelist before the numeric scan", async () => {
  // 焦点唯一归属：白名单非法时校验必须短路返回，不再跑数值校验——
  // 两个校验器都会 focus 各自首个非法字段，后跑者抢焦点、错误提示跳变。
  const source = await readFile(join(pageDir, "config-io.mjs"), "utf8");
  const whitelistGuard = source.indexOf("if (!validateWhitelist()) {");
  const numericGuard = source.indexOf("if (!validateAll()) {");
  assert.ok(whitelistGuard !== -1, "saveConfig must guard on validateWhitelist()");
  assert.ok(numericGuard !== -1, "saveConfig must guard on validateAll()");
  assert.ok(
    whitelistGuard < numericGuard,
    "validateWhitelist() must run before validateAll() so the whitelist focus is not stolen back",
  );
});

test("non-whitelist illegal-char save errors do not paint the whitelist field", async () => {
  const classList = {
    add() {},
    remove() {},
    toggle() {},
  };
  const fields = [
    { dataset: { configKey: "whitelist_sessions", configTransform: "whitelist" }, value: "ok" },
    { dataset: { configKey: "judge_provider_id", configControl: "judge" } },
  ];
  const form = {
    classList,
    inert: false,
    querySelector: () => null,
    querySelectorAll: () => fields,
  };
  const whitelistError = { textContent: "", classList };
  const whitelistInput = {
    ...fields[0],
    removeAttribute() {},
    setAttribute() {
      throw new Error("whitelist should not be painted for bot_aliases errors");
    },
    focus() {
      throw new Error("whitelist should not be focused for bot_aliases errors");
    },
  };
  const elements = {
    configForm: form,
    whitelistInput,
    whitelistError,
    configSaveState: { textContent: "", classList },
  };
  const state = {
    configLoaded: true,
    savingConfig: false,
    configRevision: TEST_REVISION,
    isDirty: false,
    requiresConfigRefresh: false,
  };
  const toasts = [];
  const io = createConfigIo({
    getEls: () => elements,
    getState: () => state,
    setState: (updates) => Object.assign(state, updates),
    apiGet: async () => {
      throw new Error("skip refresh");
    },
    apiPost: async () => ({
      ok: false,
      error: "bot_aliases 条目含非法字符",
    }),
    showToast: (msg) => toasts.push(msg),
    setStatState() {},
    renderPromptPreview() {},
    judgeProviderControl: { value: () => "", sync() {} },
    visionProviderControl: { value: () => "", sync() {} },
    visionJudgeProviderControl: { value: () => "", sync() {} },
    fmtBool: String,
  });

  await io.saveConfig({ preventDefault() {} });

  assert.equal(whitelistError.textContent, "");
  assert.ok(toasts.includes("bot_aliases 条目含非法字符"));
});

test("whitelist illegal-char save errors still paint the whitelist field", async () => {
  const classList = {
    added: [],
    add(name) {
      this.added.push(name);
    },
    remove() {},
    toggle() {},
  };
  const fields = [
    { dataset: { configKey: "whitelist_sessions", configTransform: "whitelist" }, value: "ok" },
  ];
  const form = {
    classList,
    inert: false,
    querySelector: () => null,
    querySelectorAll: () => fields,
  };
  const attrs = {};
  const focused = [];
  const whitelistError = { textContent: "", classList };
  const whitelistInput = {
    ...fields[0],
    removeAttribute(name) {
      delete attrs[name];
    },
    setAttribute(name, value) {
      attrs[name] = value;
    },
    focus() {
      focused.push("whitelist");
    },
  };
  const elements = {
    configForm: form,
    whitelistInput,
    whitelistError,
    configSaveState: { textContent: "", classList },
  };
  const state = {
    configLoaded: true,
    savingConfig: false,
    configRevision: TEST_REVISION,
    isDirty: false,
    requiresConfigRefresh: false,
  };
  const io = createConfigIo({
    getEls: () => elements,
    getState: () => state,
    setState: (updates) => Object.assign(state, updates),
    apiGet: async () => {
      throw new Error("skip refresh");
    },
    apiPost: async () => ({
      ok: false,
      error: "whitelist_sessions 条目含非法字符",
    }),
    showToast() {},
    setStatState() {},
    renderPromptPreview() {},
    judgeProviderControl: { value: () => "", sync() {} },
    visionProviderControl: { value: () => "", sync() {} },
    visionJudgeProviderControl: { value: () => "", sync() {} },
    fmtBool: String,
  });

  await io.saveConfig({ preventDefault() {} });

  assert.equal(attrs["aria-invalid"], "true");
  assert.equal(whitelistError.textContent, "whitelist_sessions 条目含非法字符");
  assert.deepEqual(focused, ["whitelist"]);
});

test("unknown provider id warns but does not block save", async () => {
  const classList = { add() {}, remove() {}, toggle() {} };
  const fields = [
    {
      dataset: { configKey: "judge_provider_id", configControl: "judge" },
    },
  ];
  const form = {
    classList,
    inert: false,
    querySelector: () => null,
    querySelectorAll: () => fields,
  };
  const elements = {
    configForm: form,
    configSaveState: { textContent: "", classList },
  };
  const state = {
    configLoaded: true,
    savingConfig: false,
    configRevision: TEST_REVISION,
    isDirty: false,
    requiresConfigRefresh: false,
  };
  const toasts = [];
  let posted = false;
  const io = createConfigIo({
    getEls: () => elements,
    getState: () => state,
    setState: (updates) => Object.assign(state, updates),
    apiGet: async () => {
      throw new Error("skip refresh");
    },
    apiPost: async () => {
      posted = true;
      return { ok: true, config: {}, adjusted_fields: [] };
    },
    showToast: (msg) => toasts.push(msg),
    setStatState() {},
    renderPromptPreview() {},
    judgeProviderControl: { value: () => "typo-id", sync() {} },
    visionProviderControl: { value: () => "", sync() {} },
    visionJudgeProviderControl: { value: () => "", sync() {} },
    fmtBool: String,
    getProviderOptions: () => [{ id: "real-id", label: "real" }],
    isProviderListAvailable: () => true,
  });

  await io.saveConfig({ preventDefault() {} });

  assert.equal(posted, true);
  assert.ok(toasts.some((msg) => msg.includes("不在列表中")));
});

test("undici and abort failures map to the connection hint", () => {
  const hint = "无法连接插件 API，请重载页面或重启 AstrBot 后重试";
  assert.equal(normalizeApiError(new Error("fetch failed")).message, hint);
  const aborted = new Error("This operation was aborted");
  aborted.name = "AbortError";
  assert.equal(normalizeApiError(aborted).message, hint);
});

test("backend messages containing fetch details pass through untouched", () => {
  const backend = new Error("保存失败：fetch failed details");
  assert.equal(normalizeApiError(backend), backend);
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

test("validateWhitelistLines splits on commas like the save path", () => {
  const errors = validateWhitelistLines('ok1,bad"q');
  assert.equal(errors.length, 1);
  assert.equal(errors[0].line, 2);
  assert.ok(errors[0].reason.includes("非法字符"));
});

test("summarizeWhitelist warns past the backend whitelist cap", () => {
  const raw = Array.from({ length: 1002 }, (_, i) => `s${i}`).join("\n");
  assert.ok(summarizeWhitelist(raw).includes("超过 1000 条上限"));
});

test("empty number input is a validation error, not a silent fallback", () => {
  assert.equal(numberFieldError("5", 0, 30), "");
  assert.ok(numberFieldError("", 0, 30).length > 0);
  assert.ok(numberFieldError("99", 0, 30).includes("不能大于"));
});

test("integer number controls reject fractional input before save", () => {
  assert.ok(numberFieldError("5.5", 0, 100, true).includes("整数"));
  // step=5 只是滑杆增量，不是整除约束：47 必须合法，否则前端比后端更严。
  assert.equal(numberFieldError("47", 0, 100, true), "");
  assert.equal(numberFieldError("5.5", 0, 100), "");
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

test("settings page JS sources keep lines within the width cap", async () => {
  // 文件清单由目录派生，不手抄：手写清单在新增设置页脚本时不会自动跟上，
  // 新文件的行宽就此无人看守（这类"名单腐烂"正是本套契约要防的形态）。
  const names = (await readdir(pageDir)).filter((name) => /\.(js|mjs)$/.test(name)).sort();
  assert.ok(names.includes("app.js"), "设置页脚本清单为空或目录读错");
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

test("a failed POST leaves the page requiring a refresh before the next save", async () => {
  // 保存请求结果未知（异常/超时）是"必须刷新才能再写"的两条路径之一：该状态
  // 一旦丢失，页面会带着旧 revision 再提交一次，而服务端可能已经写入。
  // 进入状态与阻挡第二次提交都要成立，缺一不可。
  const classList = { add() {}, remove() {}, toggle() {} };
  const fields = [
    { dataset: { configKey: "enabled" }, type: "checkbox", checked: true },
  ];
  const form = {
    classList,
    inert: false,
    querySelector: () => null,
    querySelectorAll: () => fields,
  };
  const elements = {
    configForm: form,
    configSaveState: { textContent: "", classList },
  };
  const state = {
    configLoaded: true,
    savingConfig: false,
    configRevision: TEST_REVISION,
    isDirty: false,
    requiresConfigRefresh: false,
  };
  const toasts = [];
  let posts = 0;
  const io = createConfigIo({
    getEls: () => elements,
    getState: () => state,
    setState: (updates) => Object.assign(state, updates),
    apiGet: async () => {
      throw new Error("skip refresh");
    },
    apiPost: async () => {
      posts += 1;
      throw new Error("Failed to fetch");
    },
    showToast: (message) => toasts.push(message),
    setStatState() {},
    renderPromptPreview() {},
    judgeProviderControl: { value: () => "", sync() {} },
    visionProviderControl: { value: () => "", sync() {} },
    visionJudgeProviderControl: { value: () => "", sync() {} },
    fmtBool: String,
  });

  await io.saveConfig({ preventDefault() {} });

  assert.equal(posts, 1);
  assert.equal(state.requiresConfigRefresh, true);
  assert.ok(toasts.some((message) => message.includes("保存状态未知")));

  await io.saveConfig({ preventDefault() {} });
  assert.equal(posts, 1, "未知写入后不得在刷新前再次提交");
});

test("a STALE_WRITE response adopts the server revision and requires a refresh", async () => {
  // 迟到旧写被服务端拒绝时，客户端必须接受服务端返回的新 revision 并进入
  // "必须刷新"，否则会继续拿旧 revision 重试（永远 STALE_WRITE）。
  const classList = { add() {}, remove() {}, toggle() {} };
  const fields = [
    { dataset: { configKey: "enabled" }, type: "checkbox", checked: true },
  ];
  const form = {
    classList,
    inert: false,
    querySelector: () => null,
    querySelectorAll: () => fields,
  };
  const elements = {
    configForm: form,
    configSaveState: { textContent: "", classList },
  };
  const newerRevision = `sha256:${"b".repeat(64)}`;
  const state = {
    configLoaded: true,
    savingConfig: false,
    configRevision: TEST_REVISION,
    isDirty: false,
    requiresConfigRefresh: false,
  };
  const toasts = [];
  let posts = 0;
  const io = createConfigIo({
    getEls: () => elements,
    getState: () => state,
    setState: (updates) => Object.assign(state, updates),
    apiGet: async () => {
      throw new Error("skip refresh");
    },
    apiPost: async () => {
      posts += 1;
      return {
        ok: false,
        error: "配置已被其他请求修改",
        error_code: "STALE_WRITE",
        config_revision: newerRevision,
      };
    },
    showToast: (message) => toasts.push(message),
    setStatState() {},
    renderPromptPreview() {},
    judgeProviderControl: { value: () => "", sync() {} },
    visionProviderControl: { value: () => "", sync() {} },
    visionJudgeProviderControl: { value: () => "", sync() {} },
    fmtBool: String,
  });

  await io.saveConfig({ preventDefault() {} });

  assert.equal(posts, 1);
  assert.equal(state.configRevision, newerRevision);
  assert.equal(state.requiresConfigRefresh, true);
  assert.ok(toasts.some((message) => message.includes("配置已被其他请求修改")));

  await io.saveConfig({ preventDefault() {} });
  assert.equal(posts, 1, "STALE_WRITE 后不得在刷新前再次提交");
});

test("boot watchdog yields to in-flight first load instead of failing early", async () => {
  // 首屏串行两个请求（各 FETCH_TIMEOUT_MS 上限，最坏 30s）会超过 12s 的 boot
  // 定时器：看门狗在加载在途时必须重新武装而不是误报「加载超时」，否则慢网
  // 用户会先看到失败提示、随后配置又正常渲染。源码级契约，防止回退。
  const app = await readFile(join(pageDir, "app.js"), "utf8");
  assert.match(app, /let loadInFlight = false;/);
  assert.match(
    app,
    /loadInFlight = true;[\s\S]*?finally\s*\{[\s\S]*?loadInFlight = false;/,
    "loadAll 必须在收尾处置位 loadInFlight，成功与失败路径都要覆盖"
  );
  assert.match(
    app,
    /function checkBoot\(\) \{[\s\S]*?state\.configLoaded\) return;[\s\S]*?if \(loadInFlight\) \{[\s\S]*?window\.setTimeout\(checkBoot, FETCH_TIMEOUT_MS\);/,
    "看门狗必须先查加载状态：已加载静默退出，在途则重新武装"
  );
  // 再查分支重写句柄（let），否则收尾路径 clearTimeout 取消不到最新的定时器，
  // 失败后仍会弹出第二条误导性的「加载超时」。
  assert.match(app, /let bootTimeout = window\.setTimeout\(function checkBoot/);
  assert.match(app, /bootTimeout = window\.setTimeout\(checkBoot, FETCH_TIMEOUT_MS\);/);
});
