import {
	FETCH_TIMEOUT_MS,
	MAX_FALLBACK_PROVIDERS,
	requestPluginApi,
} from "./frontend-core.mjs";
import { renderPromptTemplateHtml } from "./config-form.mjs";
import {
	createProviderControl,
	createProviderListControl,
	createProviderQueueControl,
} from "./providers.mjs";
import {
	THEME_KEY,
	applyTheme,
	nextTheme,
	normalizeTheme,
	persistTheme,
	restoreTheme,
} from "./theme.mjs";
import {
	applyBold,
	applyDim,
	bindDimBoldButtons,
	boldWasTouched,
	createScrollHandler,
	dimWasTouched,
	hideBoot,
	markThemeTouched,
	restoreDimBold,
	setupMobileTabs,
	setupMoreActionsMenu,
	setupNav,
	syncTopbarHeight,
	themeWasTouched,
	updateNavFades,
	updateTopbarStuck,
} from "./chrome.mjs";
import { createConfigIo } from "./config-io.mjs";

const PLUGIN_ID = "astrbot_plugin_self_initiated_reply";

let els = null;
const $ = (id) => document.getElementById(id);

function getEls() {
	if (els) return els;
	els = {
		topbar: document.querySelector(".topbar"),
		sidenavList: document.querySelector(".sidenav-list"),
		sidenavFadeStart: document.querySelector(".sidenav-fade-start"),
		sidenavFadeEnd: document.querySelector(".sidenav-fade-end"),
		navSaveDot: $("navSaveDot"),
		navSaveState: $("navSaveState"),
		refreshBtn: $("refreshBtn"),
		saveTopBtn: $("saveTopBtn"),
		saveBottomBtn: $("saveBottomBtn"),
		themeToggle: $("themeToggle"),
		selfStat: $("selfStat"),
		selfStatus: $("selfStatus"),
		decisionModelStat: $("decisionModelStat"),
		decisionModelStatus: $("decisionModelStatus"),
		whitelistCount: $("whitelistCount"),
		configForm: $("configForm"),
		enabledInput: $("enabledInput"),
		decisionModelInput: $("decisionModelInput"),
		providerListState: $("providerListState"),
		decisionPromptInput: $("decisionPromptInput"),
		promptPreview: $("promptPreview"),
		resetPromptBtn: $("resetPromptBtn"),
		cleanupImageCacheBtn: $("cleanupImageCacheBtn"),
		cleanupImageCacheState: $("cleanupImageCacheState"),
		whitelistInput: $("whitelistInput"),
		whitelistError: $("whitelistError"),
		whitelistSummary: $("whitelistSummary"),
		formatWhitelistBtn: $("formatWhitelistBtn"),
		configSaveState: $("configSaveState"),
		toast: $("toast"),
		boot: $("boot"),
		mobileSaveState: $("mobileSaveState"),
		saveMobileBtn: $("saveMobileBtn"),
		mobileTabbar: $("mobileTabbar"),
		moreActions: $("moreActions"),
		moreActionsBtn: $("moreActionsBtn"),
		moreActionsMenu: $("moreActionsMenu"),
	};
	return els;
}

getEls();

let bridgeReady = null;
let providerOptions = [];
let providerListAvailable = false;
const state = {
	savingConfig: false,
	refreshing: false,
	configLoaded: false,
	configRevision: "",
	runtimeEnabled: false,
	requiresConfigRefresh: false,
	isDirty: false,
};
const REFRESH_ARM_MS = 3000;
const TOAST_MS = 2200;
const PREVIEW_DEBOUNCE_MS = 80;
const BOOT_TIMEOUT_MS = 12000;
let toastTimer = null;

function setStatState(element, stateName) {
	if (!element) return;
	element.classList.remove("is-on", "is-off");
	element.classList.add(stateName);
}

function showToast(message, isError = false) {
	const toast = els.toast;
	if (!toast) return;
	toast.textContent = message;
	// role 恒为 status + aria-live="polite"（静态声明在 index.html）：紧急通道由
	// 带 role="alert" 的字段级错误承担，toast 不切 role 到 role="alert"，与显式
	// aria-live="polite" 是互相矛盾的组合（显式值优先，alert 的紧急语义被中和）。
	// 错误态只用 is-error 表达视觉差异，不改变朗读优先级。
	toast.classList.toggle("is-error", Boolean(isError));
	toast.classList.add("show");
	window.clearTimeout(toastTimer);
	toastTimer = window.setTimeout(() => toast.classList.remove("show"), TOAST_MS);
}

async function getBridge() {
	// 两个条件都查：宿主可能给出一个缺 ``ready`` 的残缺/旧版 SDK 对象。只查对象
	// 存在时，下一行的 ``.ready()`` 会抛裸 TypeError，被 normalizeApiError 原样当
	// 作用户可见的 toast 弹出来，而文档里承诺的 fetch 兜底根本不会触发。
	// 判据取「可调用」而非「真值」：ready 为 "yes" / 1 / {} 同样过得了真值检查，
	// 调到时照样抛那个 TypeError，兜底仍不触发。
	if (typeof window.AstrBotPluginPage?.ready !== "function") return null;
	// ready() 的结论必须被读出来：requestPluginApi 靠本函数的返回值决定走 bridge
	// 还是走 fetch，握手失败却仍返回对象，等于宣称 bridge 可用，兜底路径永不触发。
	// 映射成布尔而不是直接取真值，避免「握手成功但解析为 falsy」被误判成失败。
	if (!bridgeReady)
		bridgeReady = window.AstrBotPluginPage.ready().then(
			() => true,
			() => false,
		);
	return (await bridgeReady) ? window.AstrBotPluginPage : null;
}

function request(method, endpoint, payload = {}) {
	return requestPluginApi({
		getBridge,
		pluginId: PLUGIN_ID,
		endpoint,
		method,
		params: method === "GET" ? payload : {},
		body: method === "POST" ? payload : {},
		fetchImpl: window.fetch.bind(window),
		pageUrl: window.location.href,
	});
}

function apiGet(endpoint, params = {}) {
	return request("GET", endpoint, params);
}

function apiPost(endpoint, body = {}) {
	return request("POST", endpoint, body);
}

function fmtBool(value) {
	return value ? "启用" : "关闭";
}

function renderPromptPreview() {
	if (!els.promptPreview) return;
	const template =
		els.decisionPromptInput.value ||
		els.decisionPromptInput.dataset.defaultPrompt ||
		"";
	els.promptPreview.innerHTML = renderPromptTemplateHtml(template);
}

const providerDeps = {
	getOptions: () => providerOptions,
	isListAvailable: () => providerListAvailable,
	maxItems: MAX_FALLBACK_PROVIDERS,
	showToast,
	onDirty: () => configIo.setDirty(true),
};

/* Provider 控件只接收各自使用的元素；只有手动/列表切换控件需要包裹层。 */
const PROVIDER_CONTROLS = [
	// 生成模型与备用模型只能从列表里选：让用户手填 Provider ID 等于让他记住
	// 宿主内部标识，填错时这一轮静默回落，日志里也看不出是配置问题。
	{
		name: "proactive",
		select: $("proactiveProviderSelect"),
		placeholder: "使用当前会话模型",
		listOnly: true,
	},
	{
		name: "proactiveFallback",
		select: $("proactiveFallbackSelect"),
		list: $("proactiveFallbackList"),
		button: $("proactiveFallbackAddBtn"),
		status: $("proactiveFallbackStatus"),
		queue: true,
	},
	{
		name: "vision",
		field: $("visionProviderField"),
		select: $("visionProviderSelect"),
		input: $("visionProviderInput"),
		button: $("visionProviderManualBtn"),
		placeholder: "使用当前会话模型",
	},
	{
		name: "visionJudge",
		field: $("visionJudgeProviderField"),
		select: $("visionJudgeProviderSelect"),
		input: $("visionJudgeProviderInput"),
		button: $("visionJudgeProviderManualBtn"),
		placeholder: "与判断模型一致",
	},
	{
		name: "judge",
		field: $("judgeProviderField"),
		select: $("judgeProviderSelect"),
		input: $("judgeProviderInput"),
		button: $("providerManualBtn"),
		placeholder: "使用当前会话默认模型",
	},
];

const providerControlsByName = {};
const providerControlList = PROVIDER_CONTROLS.map((spec) => {
	let control;
	if (spec.queue) control = createProviderQueueControl(spec, providerDeps);
	else if (spec.listOnly) control = createProviderListControl(spec, providerDeps);
	else control = createProviderControl(spec, providerDeps);
	providerControlsByName[spec.name] = control;
	return control;
});
const configIo = createConfigIo({
	// getEls 直传：它自带惰性初始化（首次调用时建 els 并缓存），包一层
	// () => els 会把首次调用提前到本行执行，缓存 null。state 两个键保留箭头，
	// 它们是对模块内对象的读写，没有可直传的函数引用。
	getEls,
	getState: () => state,
	setState: (patch) => Object.assign(state, patch),
	apiGet,
	apiPost,
	showToast,
	setStatState,
	renderPromptPreview,
	// 三个 Provider 控件的唯一注册表：读写表单与保存请求共用同一映射，
	// 键名即 index.html 的 data-config-control。
	providerControls: providerControlsByName,
	fmtBool,
	getProviderOptions: () => providerOptions,
	isProviderListAvailable: () => providerListAvailable,
});

async function loadProviders() {
	const renderAll = () => providerControlList.forEach((control) => control.render());
	try {
		const result = await apiGet("providers");
		if (!result || result.ok === false)
			throw new Error(result?.error || "无法加载 Provider 列表");
		providerOptions = Array.isArray(result.providers)
			? result.providers.filter((item) => item && item.id)
			: [];
		providerListAvailable = true;
		// 重建选项会清空列表外的当前值，先保存，再由 sync 恢复。
		const preserved = providerControlList.map((control) => control.value());
		renderAll();
		providerControlList.forEach((control, index) => control.sync(preserved[index]));
		if (els.providerListState) els.providerListState.textContent = "";
		return { listAvailable: true };
	} catch (error) {
		providerOptions = [];
		providerListAvailable = false;
		// 清空列表前保存当前值，由 sync 将原值恢复到手动输入框。
		const preserved = providerControlList.map((control) => control.value());
		renderAll();
		// 列表不可用时全部切手动输入：循环保证不会漏掉后加的控件。
		providerControlList.forEach((control, index) => control.sync(preserved[index]));
		if (els.providerListState) {
			els.providerListState.textContent =
				"Provider 列表不可用，判断与识图模型可手动填写";
		}
		showToast("无法加载 Provider 列表");
		return { listAvailable: false };
	}
}

// 首屏请求仍在途时让 boot 看门狗等待请求自身的超时处理。
let loadInFlight = false;

async function loadAll({ force = false } = {}) {
	// 布尔而非计数的前提：第二个入口只有刷新按钮，而 #boot 遮罩（style.css
	// z-index:90）盖住 .topbar（25）与窄屏下的更多操作菜单（50），在途时点不到它。
	loadInFlight = true;
	try {
		// 同时发起读取，让编辑检查覆盖刷新开始后的整个等待窗口。
		const [applied] = await Promise.all([
			configIo.loadConfig({ force }),
			loadProviders(),
		]);
		return applied;
	} finally {
		loadInFlight = false;
	}
}

let refreshArmed = false;
let refreshArmTimer = null;

async function doRefresh() {
	if (state.savingConfig || state.refreshing) return;
	state.refreshing = true;
	// disabled 不在这里直写：这几颗按钮的可用态只由 setSaving 判定，而它在刷新
	// 尾段（applyConfigPayload）也会被调一次，直写会把两边规则各记一半。
	// 起尾同一表达式：此刻 savingConfig 必为 false（上面的守卫），尾段则可能为真。
	configIo.setSaving(state.savingConfig);
	els.refreshBtn.classList.add("is-loading");
	try {
		const applied = await loadAll({ force: true });
		// 只说真话：表单脏时响应被拦下，内容仍是用户编辑的那份，谎报「已刷新为
		// 最新配置」会让用户以为磁盘内容已生效（随后保存会把他的编辑覆盖上去）。
		// 口径由 configIo 判定：脏表单给操作指引，干净表单只用中性提示；响应为何
		// 作废由协调器决定，页面不臆测具体竞态来源。
		showToast(applied === false ? configIo.lateRefreshMessage() : "已刷新为最新配置");
	} catch (err) {
		showToast(err.message || "刷新失败");
	} finally {
		state.refreshing = false;
		// 交回唯一判据重算：保存还在途时刷新必须继续按住。
		configIo.setSaving(state.savingConfig);
		els.refreshBtn.classList.remove("is-loading");
	}
}

if (els.refreshBtn) {
	els.refreshBtn.addEventListener("click", (event) => {
		if (state.refreshing) return;
		if (state.isDirty && !refreshArmed) {
			refreshArmed = true;
			els.refreshBtn.classList.add("is-armed");
			showToast(
				`有未保存改动，${REFRESH_ARM_MS / 1000} 秒内再点一次刷新将丢弃改动`,
			);
			// 本按钮同时在 #moreActionsMenu 内，chrome.mjs 给菜单里每个 button 挂了
			// closeMenu。窄屏下它会在武装之后立刻隐藏菜单，「再点一次」就点不到了。
			// 两个监听器同目标同类型、按注册序触发，而 setupMoreActionsMenu 晚于此处
			// 注册，故停掉传播要挡的就是它。冒泡也一并停了，但 document 级那个监听
			// 只在点击落到 #moreActions 之外时关菜单，对本目标本来就是空操作。
			event.stopImmediatePropagation();
			window.clearTimeout(refreshArmTimer);
			refreshArmTimer = window.setTimeout(() => {
				refreshArmed = false;
				els.refreshBtn.classList.remove("is-armed");
			}, REFRESH_ARM_MS);
			return;
		}
		refreshArmed = false;
		window.clearTimeout(refreshArmTimer);
		els.refreshBtn.classList.remove("is-armed");
		doRefresh();
	});
}

if (els.cleanupImageCacheBtn) {
	els.cleanupImageCacheBtn.addEventListener("click", () =>
		configIo.cleanupImageCache(),
	);
}

if (els.resetPromptBtn) {
	els.resetPromptBtn.addEventListener("click", () => {
		if (!state.configLoaded) {
			showToast(configIo.configNotLoadedMessage());
			return;
		}
		els.decisionPromptInput.value =
			els.decisionPromptInput.dataset.defaultPrompt || "";
		renderPromptPreview();
		configIo.setDirty(true);
		showToast("已恢复默认提示词，点击保存后生效");
	});
}

if (els.decisionPromptInput) {
	let previewTimer = null;
	els.decisionPromptInput.addEventListener("input", () => {
		window.clearTimeout(previewTimer);
		previewTimer = window.setTimeout(renderPromptPreview, PREVIEW_DEBOUNCE_MS);
	});
}

if (els.enabledInput) {
	els.enabledInput.addEventListener("change", () => {
		const on = els.enabledInput.checked;
		// 运行态（/selfreply off 置的 runtimeEnabled）与开关态是两个轴：
		// applyConfigPayload 对暂停态显示「已暂停（/off）」并把状态灯按
		// runtimeEnabled 置 off。这里若只看复选框，暂停期间拨动开关会把
		// 文案写成「启用（未保存）」、状态灯重新脉冲，直到保存或刷新才自愈。
		const runtimeOn = state.runtimeEnabled !== false;
		els.selfStatus.textContent = on
			? runtimeOn
				? "启用（未保存）"
				: "已暂停（未保存）"
			: "关闭（未保存）";
		// 状态类必须与文案同步：`.master.is-off` 停掉 stat-dot 脉冲、换背景，
		// 只改文案会让开关在视觉上仍显示为启用。
		setStatState(els.selfStat, on && runtimeOn ? "is-on" : "is-off");
		configIo.setDirty(true);
	});
}

if (els.decisionModelInput) {
	els.decisionModelInput.addEventListener("change", () => {
		const on = els.decisionModelInput.checked;
		els.decisionModelStatus.textContent = fmtBool(on);
		setStatState(els.decisionModelStat, on ? "is-on" : "is-off");
		configIo.setDirty(true);
	});
}

if (els.configForm) {
	els.configForm.addEventListener("submit", (event) =>
		configIo.saveConfig(event).catch((err) => {
			configIo.setSaveState("保存失败", "error");
			showToast(err.message || "保存失败");
		}),
	);
}

if (els.themeToggle) {
	els.themeToggle.addEventListener("click", () => {
		const next = nextTheme();
		markThemeTouched();
		applyTheme(next, els.themeToggle);
		persistTheme(next, apiPost, showToast);
	});
}

bindDimBoldButtons(() => persistTheme(null, apiPost, showToast));
restoreDimBold();
try {
	const saved = normalizeTheme(localStorage.getItem(THEME_KEY));
	if (saved !== "auto") applyTheme(saved, els.themeToggle);
} catch (error) {
	/* localStorage 不可用 */
}

// 两个显式保存按钮都走原生提交（底部按钮 type="submit" 同理）：同一语义一种实现。
[els.saveTopBtn, els.saveMobileBtn].forEach((btn) => {
	if (btn) btn.addEventListener("click", () => els.configForm.requestSubmit());
});

setupNav(els);
configIo.setupValidation();
setupMoreActionsMenu(els);
setupMobileTabs(els);
window.addEventListener("scroll", createScrollHandler(els), { passive: true });
syncTopbarHeight(els);
updateTopbarStuck(els);
configIo.attachDirtyListeners();

if (els.sidenavList) {
	els.sidenavList.addEventListener("scroll", () => updateNavFades(els), {
		passive: true,
	});
	window.addEventListener("resize", () => updateNavFades(els), {
		passive: true,
	});
}

window.addEventListener("beforeunload", (e) => {
	if (state.isDirty) {
		e.preventDefault();
		e.returnValue = "";
	}
});

configIo.setSaving(false);

// 模块装配已全部完成，从这里开始的失败才该由 loadAll 收尾。标志与定时器必须
// 留到这里再置：上面任何一个控件绑定抛错（元素被改名/缺失）都会中断模块求值，
// 提前置位会连带关掉 8s 的「脚本加载失败」遮罩，页面永久停在「正在加载配置…」
// 且没有任何提示。
window.__selfreplyAppStarted = true;
if (window.__selfreplyBootFailTimer)
	window.clearTimeout(window.__selfreplyBootFailTimer);

// 加载成功或失败均由 loadAll 收尾；延后检查时保留句柄以便取消。
let bootTimeout = window.setTimeout(function checkBoot() {
	if (state.configLoaded) return;
	if (loadInFlight) {
		// 两个请求并行，均受单次 API 超时约束。
		bootTimeout = window.setTimeout(checkBoot, FETCH_TIMEOUT_MS);
		return;
	}
	hideBoot(els);
	showToast("加载超时，请刷新页面或检查后端状态");
}, BOOT_TIMEOUT_MS);

loadAll()
	.then(() => {
		window.clearTimeout(bootTimeout);
		hideBoot(els);
	})
	.catch((err) => {
		window.clearTimeout(bootTimeout);
		state.configLoaded = false;
		configIo.setSaving(false);
		hideBoot(els);
		// 首屏读数从「加载中 / -」改写为「不可用」：否则用户看到三处加载态与一个
		// 错误 toast 并存，分不清是还没加载完还是已经失败。
		if (els.selfStatus) els.selfStatus.textContent = "加载失败";
		if (els.decisionModelStatus) els.decisionModelStatus.textContent = "加载失败";
		if (els.whitelistCount) els.whitelistCount.textContent = "-";
		showToast(err.message || "加载失败");
	});

restoreTheme(apiGet).then((prefs) => {
	// 同值也要走一次 applyTheme：按钮的「当前：」朗读名只由它写入，短路掉之后
	// 首屏（尤其 auto，本地没缓存可补）只剩 index.html 的静态 aria-label。
	if (!themeWasTouched()) applyTheme(prefs.theme, els.themeToggle);
	// 逐个字段判"用户动过没有"，不能合成一个总判据：GET 在途期间点过 dim 时，
	// DOM 里的 bold 还是本地默认（或 localStorage 旧值），此时跳过 applyBold
	// 会让 persistTheme 把那个来路不明的值一并提交，静默抹掉服务端上没动过的
	// 开关。分开判才能保证「未动的那个一定被服务端值补齐」。
	if (!dimWasTouched()) applyDim(prefs.dim);
	if (!boldWasTouched()) applyBold(prefs.bold);
});
