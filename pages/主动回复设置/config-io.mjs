import {
	numberFieldError,
	parseWhitelist,
	summarizeWhitelist,
	uniqueWhitelistItems,
	validateWhitelistLines,
} from "./config-form.mjs";
import { providerNeedsManualInput } from "./providers.mjs";
import {
	createConfigRequestCoordinator,
	isSuccessfulConfigPayload,
	missingConfigPayloadKeys,
} from "./frontend-core.mjs";
const CONFIG_CONTROL_SELECTOR = "[data-config-key]";

/** POST /config fields are declared by form data-config-key metadata. */
function configControls(form) {
	return [...form.querySelectorAll(CONFIG_CONTROL_SELECTOR)];
}

export function configSaveKeys(form) {
	return configControls(form).map((control) => control.dataset.configKey);
}

function providerConfigKeys(form) {
	return configControls(form)
		.filter((control) => control.dataset.configControl)
		.map((control) => control.dataset.configKey);
}

function configControlValue(control, providerControls) {
	const { configControl, configTransform } = control.dataset;
	if (configControl) return providerControls[configControl].value();
	if (configTransform === "whitelist") return parseWhitelist(control.value);
	if (control.type === "checkbox") return control.checked;
	// 数值不需要兜底：saveConfig 先跑 validateAll，空值与非法值都拦在取值之前。
	if (control.type === "number") return Number(control.value);
	return configTransform === "trim" ? control.value.trim() : control.value;
}

export function buildConfigSaveBody(form, providerControls, baseRevision = "") {
	const body = Object.fromEntries(
		configControls(form).map((control) => [
			control.dataset.configKey,
			configControlValue(control, providerControls),
		]),
	);
	if (baseRevision) body.base_revision = baseRevision;
	return body;
}

function loadConfigControls(form, config, providerControls) {
	for (const control of configControls(form)) {
		const {
			configControl,
			configFallbackKey,
			configKey,
			configTransform,
		} = control.dataset;
		if (configControl) {
			providerControls[configControl].sync(config[configKey] || "");
		} else if (configTransform === "whitelist") {
			control.value = Array.isArray(config[configKey])
				? config[configKey].join("\n")
				: "";
		} else if (control.type === "checkbox") {
			// 后端 bool 键经 as_bool 归一恒为真 bool，且每个 data-config-key 都是
			// GET /config 的 requiredKey（缺键在 isSuccessfulConfigPayload 处即抛错，
			// 走不到这里），故不需要"缺键时按默认值取反"的分支。
			control.checked = Boolean(config[configKey]);
		} else if (control.type === "number") {
			control.value = config[configKey] ?? "";
		} else {
			control.value =
				config[configKey] || config[configFallbackKey] || "";
		}
	}
}

export function createConfigIo(deps) {
	const {
		getEls,
		getState,
		setState,
		apiGet,
		apiPost,
		showToast,
		setStatState,
		renderPromptPreview,
		providerControls,
		fmtBool,
		getProviderOptions,
		isProviderListAvailable,
	} = deps;
	const coordinator = createConfigRequestCoordinator();
	let numberFields = [];
	let saveStateKind = "";
	// 「保存状态未知」的单点：POST 抛错与响应体校验失败是同一语义（无法确认服务
	// 端是否已写入），状态、toast 文案与保存前置守卫同进同退，否则三处文案会各自漂移。
	const SAVE_UNKNOWN_MESSAGE = "保存状态未知，请刷新配置后重试";
	function markSaveUnknown() {
		setState({ requiresConfigRefresh: true });
		setSaveState("保存状态未知", "unknown");
		showToast(SAVE_UNKNOWN_MESSAGE, true);
	}
	function setSaveState(message, state) {
		saveStateKind = state;
		const e = getEls();
		if (!e.configSaveState) return;
		// #configSaveState / #mobileSaveState 都是 aria-live="polite"，而
		// setDirty 挂在每个 input/change 上：逐字相同就不写，否则同样一句「有未
		// 保存改动」会在每次按键时被重新排进播报队列（同 #whitelistSummary 口径）。
		if (e.configSaveState.textContent !== message)
			e.configSaveState.textContent = message;
		e.configSaveState.classList.remove("is-pending", "is-ok", "is-error", "is-saving");
		// unknown 借用 error 的红样式：同样是「没存进去，要看一眼」。
		const cssKind = state === "dirty" ? "pending" : state === "unknown" ? "error" : state;
		if (cssKind) e.configSaveState.classList.add(`is-${cssKind}`);
		// 失败态要有个不随 toast 消失的指示：三处保存入口同时染红。
		const failed = state === "error" || state === "unknown";
		[e.saveTopBtn, e.saveBottomBtn, e.saveMobileBtn].forEach((btn) => {
			if (btn) btn.classList.toggle("is-failed", failed);
		});
		if (e.navSaveState) {
			if (state === "ok") e.navSaveState.textContent = "已保存";
			else if (state === "error") e.navSaveState.textContent = "保存失败";
			else if (state === "unknown") e.navSaveState.textContent = "状态未知";
			else if (state === "saving") e.navSaveState.textContent = "保存中";
			else if (state === "") e.navSaveState.textContent = "已同步";
		}
		if (e.mobileSaveState) {
			const mobileText = message || (state ? "" : "已同步");
			if (e.mobileSaveState.textContent !== mobileText)
				e.mobileSaveState.textContent = mobileText;
			e.mobileSaveState.classList.remove("is-pending", "is-ok", "is-error", "is-saving");
			if (cssKind) e.mobileSaveState.classList.add(`is-${cssKind}`);
		}
	}
	function setDirty(dirty = true) {
		if (dirty) coordinator.markEdited();
		setState({ isDirty: dirty });
		const e = getEls();
		// 四个脏值入口同源：桌面顶栏、移动端保存按钮、底部保存按钮与侧栏圆点。
		// saveMobileBtn 必须在内，否则窄屏下唯一可见的保存入口没有未保存角标。
		[e.saveTopBtn, e.saveMobileBtn, e.saveBottomBtn, e.navSaveDot].forEach((btn) => {
			if (btn) btn.classList.toggle("is-dirty", dirty);
		});
		// unknown/saving 的文案由 setSaveState 负责，这里不能盖掉。
		if (e.navSaveState && saveStateKind !== "unknown" && saveStateKind !== "saving")
			e.navSaveState.textContent = dirty ? "有未保存改动" : "已同步";
		// 刷新成功也会走到这里：失败外环随「已同步」一起清掉。
		if (!dirty) {
			[e.saveTopBtn, e.saveBottomBtn, e.saveMobileBtn].forEach((btn) => {
				if (btn) btn.classList.remove("is-failed");
			});
		}
		// unknown 是「先把配置刷回来才能再存」：这时改字不能把这句话盖掉。
		if (dirty && saveStateKind !== "saving" && saveStateKind !== "unknown")
			setSaveState("有未保存改动", "dirty");
		// error 一并清：一次成功的刷新/加载已经拿到服务端真值，此时保留
		// 「保存失败」会让顶标说「已同步」、正文说「保存失败」，两句互相矛盾。
		else if (
			!dirty &&
			(saveStateKind === "dirty" ||
				saveStateKind === "error" ||
				saveStateKind === "unknown")
		)
			setSaveState("", "");
	}
	function attachDirtyListeners() {
		const e = getEls();
		if (!e.configForm) return;
		e.configForm.addEventListener("change", () => setDirty(true));
		e.configForm.addEventListener("input", (ev) => {
			if (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA")
				setDirty(true);
		});
	}
	function setSaving(loading) {
		const e = getEls();
		const { configLoaded, requiresConfigRefresh } = getState();
		const blocked = loading || !configLoaded || requiresConfigRefresh;
		const buttons = [
			e.saveTopBtn,
			e.saveMobileBtn,
			e.saveBottomBtn,
		];
		buttons.forEach((btn) => {
			if (!btn) return;
			btn.classList.toggle("is-loading", loading);
			btn.disabled = blocked;
		});
		// 刷新自己也在途时不能提前放回：applyConfigPayload 会走到这里，
		// 而 doRefresh 的尾段还没跑完。
		if (e.refreshBtn) e.refreshBtn.disabled = loading || getState().refreshing;
	}
	function updateWhitelistFeedback() {
		const e = getEls();
		if (!e.whitelistInput) return;
		const text = e.whitelistInput.value;
		// 只 parse 一次：计数与摘要共用同一结果（两者共用同一套分隔/去重规则，
		// 分别调用会让同一份文本被切分三遍）。
		const parsed = parseWhitelist(text);
		// 计数取**去重后**的长度：这个读数的标签是「生效会话」，而后端把裸群键与
		// 其群 UMO 视为同一会话（utils.session_whitelisted）。用未去重的 parsed.length
		// 时，`12347` + `qq:GroupMessage:12347` 会让顶栏读 2 而下方的摘要读
		// 「共 1 条」，两个数字互相矛盾。
		// 注意读数本身不校验合法性：`bad"quote` 这种保存时会被后端拦下的条目也会
		// 被计入。摘要文案因此说「共 N 条」而不是「有效」——顶栏那个
		// 「生效会话 N」在有条目非法时同样偏乐观，保存被拦下时以字段报错为准。
		const unique = uniqueWhitelistItems(text, parsed);
		// 摘要与计数都在 input 事件上写，而 #whitelistSummary 是
		// aria-live="polite"：textContent 赋值恒会替换文本节点，同一句话每敲一键
		// 就被重新排进播报队列，读屏用户粘一段 20 条的名单会听到整句被反复打断。
		// aria-describedby 已在聚焦时给出这两句，逐字相同就不再写。
		if (e.whitelistCount && e.whitelistCount.textContent !== String(unique.length))
			e.whitelistCount.textContent = String(unique.length);
		const summary = summarizeWhitelist(text, parsed);
		if (e.whitelistSummary && e.whitelistSummary.textContent !== summary)
			e.whitelistSummary.textContent = summary;
	}
	function formatWhitelist() {
		const e = getEls();
		if (!e.whitelistInput) return;
		const before = e.whitelistInput.value;
		const unique = uniqueWhitelistItems(before);
		const after = unique.join("\n");
		e.whitelistInput.value = after;
		updateWhitelistFeedback();
		// 仅在内容真变了时才置脏。已干净、已去重的列表点一下会写出逐字节相同的文本，
		// 无条件置脏会让四个入口的「未保存」角标与 beforeunload 守卫凭空出现，
		// 运营者被推着做一次没有意义的保存。比的是 parseWhitelist 之后的数组：
		// 只改空行/首尾空白的名单语义完全等价（parseWhitelist 本来就切分并 trim），
		// 却会让 beforeunload 守卫拦住一次没有改动的离开。
		if (parseWhitelist(after).join("\n") !== parseWhitelist(before).join("\n"))
			setDirty(true);
		validateWhitelist();
		showToast(
			unique.length ? `已整理并保留 ${unique.length} 条` : "白名单已清空",
		);
	}
	function setupValidation() {
		const e = getEls();
		if (!e.configForm) return;
		numberFields = [];
		e.configForm.querySelectorAll('input[type="number"]').forEach((input) => {
			if (!input.hasAttribute("min") && !input.hasAttribute("max")) return;
			const min = input.hasAttribute("min")
				? Number(input.getAttribute("min"))
				: null;
			const max = input.hasAttribute("max")
				? Number(input.getAttribute("max"))
				: null;
			const error = document.createElement("span");
			error.className = "field-error";
			error.id = `${input.id}Error`;
			error.setAttribute("role", "alert");
			// 合并而非覆盖：静态 aria-describedby 指向 field-hint，独占会把
			// 提示从可访问名计算里抹掉。
			const described = input.getAttribute("aria-describedby");
			input.setAttribute(
				"aria-describedby",
				described ? `${described} ${error.id}` : error.id,
			);
			input.insertAdjacentElement("afterend", error);
			const field = {
				input,
				error,
				min,
				max,
				integer: input.hasAttribute("data-integer"),
			};
			numberFields.push(field);
			input.addEventListener("input", () => validateField(field));
			input.addEventListener("blur", () => validateField(field));
		});
		if (e.whitelistInput) {
			e.whitelistInput.addEventListener("input", () => {
				// 摘要与计数都是 aria-live 区域：逐键重写会把每个中间态都排进播报队列
				// （输一个 UMO 三四次），停顿后再写一次。
				clearTimeout(whitelistFeedbackTimer);
				whitelistFeedbackTimer = setTimeout(updateWhitelistFeedback, 400);
				// 逐键报错会让 role=alert 的每个中间态都被播报（输一个 UMO 十几次），
				// 文案等输入停顿后再写；红框与 aria-invalid 仍即时更新。
				validateWhitelist({ quiet: true });
			});
			e.whitelistInput.addEventListener("blur", () => {
				clearTimeout(whitelistFeedbackTimer);
				updateWhitelistFeedback();
				validateWhitelist();
			});
		}
		if (e.formatWhitelistBtn) {
			e.formatWhitelistBtn.addEventListener("click", () => formatWhitelist());
		}
	}
	function validateField(field) {
		const msg = numberFieldError(
			field.input.value,
			field.min,
			field.max,
			field.integer,
		);
		if (msg) {
			field.input.setAttribute("aria-invalid", "true");
			field.error.textContent = msg;
			field.error.classList.add("show");
			return false;
		}
		field.input.removeAttribute("aria-invalid");
		field.error.classList.remove("show");
		field.error.textContent = "";
		return true;
	}
	// 出错的控件可能躺在收起的 <details> 里（「运行边界」「图片识别」）：
	// 折叠时元素不可见，focus() 既滚不到也不会展开，用户只看到一句 toast，
	// 找不到是哪个字段。展开后再聚焦，让红字真的出现在视口里。
	function revealAndFocus(element) {
		if (!element) return;
		const details = element.closest("details");
		if (details && !details.open) details.open = true;
		element.focus();
		element.scrollIntoView({ block: "center" });
	}
	function validateAll() {
		let firstBad = null;
		for (const field of numberFields) {
			if (!validateField(field) && !firstBad) firstBad = field.input;
		}
		if (firstBad) {
			revealAndFocus(firstBad);
			return false;
		}
		return true;
	}
	let whitelistErrorTimer = 0;
	let whitelistFeedbackTimer = 0;
	function validateWhitelist({ focus = false, quiet = false } = {}) {
		const e = getEls();
		if (!e.whitelistInput || !e.whitelistError) return true;
		const errors = validateWhitelistLines(e.whitelistInput.value);
		clearTimeout(whitelistErrorTimer);
		if (errors.length > 0) {
			const first = errors[0];
			// 按码点截断：按 UTF-16 码元切会切出孤立高位代理，界面显示成乱码。
			const chars = [...first.item];
			const shown = chars.slice(0, 24).join("") + (chars.length > 24 ? "…" : "");
			const msg = `第 ${first.line} 项${first.reason}：${shown}`;
			e.whitelistInput.setAttribute("aria-invalid", "true");
			// #whitelistError 是 role="alert"：同样的道理见 updateWhitelistFeedback，
			// 逐字相同就不写；逐键路径再等停顿，避免中间态把播报队列塞满。
			const writeMsg = () => {
				if (e.whitelistError.textContent !== msg) e.whitelistError.textContent = msg;
				// 文案与可见性一起写：只亮红框不写字会留下 400ms 的无解释「!」。
				e.whitelistError.classList.add("show");
			};
			if (quiet) whitelistErrorTimer = setTimeout(writeMsg, 400);
			else writeMsg();
			// 只在保存路径抢焦点：input/blur 上抢会把用户困在该字段
			// （点其他字段被拽回、Tab 逃不出），可达性缺陷。
			if (focus) revealAndFocus(e.whitelistInput);
			return false;
		}
		e.whitelistInput.removeAttribute("aria-invalid");
		e.whitelistError.classList.remove("show");
		e.whitelistError.textContent = "";
		whitelistErrorTimer = 0;
		return true;
	}
	function applyConfigPayload(config) {
		const e = getEls();
		loadConfigControls(e.configForm, config, providerControls);
		// 只有 GET /config 携带 decision_prompt_default（面板视图键）；POST 返回的
		// config 是持久配置，不含它。此处若用 decision_prompt_template 兜底，会把用户
		// 刚提交的值写成"默认"，「恢复默认提示词」随之变成空操作。
		if (config.decision_prompt_default && e.decisionPromptInput) {
			e.decisionPromptInput.dataset.defaultPrompt = config.decision_prompt_default;
		}
		if (e.whitelistInput) {
			const whitelist = parseWhitelist(e.whitelistInput.value);
			e.whitelistInput.value = whitelist.join("\n");
			updateWhitelistFeedback();
		}
		if (e.decisionModelStatus) {
			const decisionOn = config.decision_model_enabled !== false;
			e.decisionModelStatus.textContent = fmtBool(decisionOn);
			setStatState(e.decisionModelStat, decisionOn ? "is-on" : "is-off");
		}
		renderPromptPreview();
		const runtimeOn = config.runtime_enabled !== false;
		if (e.selfStatus) {
			e.selfStatus.textContent = config.enabled
				? runtimeOn
					? "启用"
					: "已暂停（/off）"
				: "关闭";
		}
		// 状态类必须同时看持久 enabled 与运行态：只按 runtimeOn 判定时，
		// 「面板里关掉总开关并保存」这一路会让复选框未勾、文字写「关闭」，
		// 而 .master.is-on 仍命中脉冲/高亮/强调色，视觉上显示为启用。
		setStatState(e.selfStat, config.enabled && runtimeOn ? "is-on" : "is-off");
		setState({
			configLoaded: true,
			configRevision: config.config_revision,
			runtimeEnabled: runtimeOn,
			requiresConfigRefresh: false,
		});
		// 强制刷新会整块重填控件值，旧校验痕迹必须一并抹掉：不重验就是
		// 控件已被服务端值覆盖、错误文案却还说「不能小于 5」（aria-invalid
		// 也停在 true），用户被指向一个并不存在的问题。validateField /
		// validateWhitelist 在无 focus 入参时不抢焦点，适合此处。
		for (const field of numberFields) validateField(field);
		validateWhitelist();
		setDirty(false);
		if (e.configForm) e.configForm.inert = false;
		setSaving(false);
	}

	async function loadConfig({ force = false } = {}) {
		const e = getEls();
		const requestEpoch = coordinator.beginLoad(getState().isDirty);
		const initialLoad = !getState().configLoaded;
		if (initialLoad && e.configForm) e.configForm.inert = true;
		try {
			const config = await apiGet("config");
			const requiredKeys = configSaveKeys(e.configForm);
			if (!isSuccessfulConfigPayload(config, requiredKeys)) {
				const missing = missingConfigPayloadKeys(config, requiredKeys);
				throw new Error(
					config?.error ||
						(missing.length
							? `配置加载失败：缺少字段 ${missing.join(", ")}`
							: "配置加载失败"),
				);
			}
			if (!coordinator.canApplyLoad(requestEpoch, getState().isDirty, force)) return false;
			applyConfigPayload(config);
			return true;
		} catch (error) {
			if (coordinator.isCurrentLoad(requestEpoch)) {
				setSaving(false);
				if (initialLoad && e.configForm) e.configForm.inert = true;
			}
			throw error;
		}
	}
	function adjustedFieldLabels(keys) {
		const form = getEls().configForm;
		return keys.map((key) => {
			const control = configControls(form).find(
				(item) => item.dataset.configKey === key,
			);
			// 容器选择器之后再兜一层 label：判断提示词的 textarea 裹在
			// `<label class="prompt-editor">` 里（label 包控件、没有 for=），
			// 前两个分支都取不到它，回退会把内部 schema 键
			// "decision_prompt_template" 直接显示给运营者。清空该输入框即触发
			// 后端 reset_default 归一化，这条路径是可达的。
			const container = control?.closest(
				".field, .provider-field, .toggle-row, .master-switch",
			) || control?.closest("label");
			// 识图两个 Provider 的 .field-label 是 .provider-field 的**兄弟**（判断
			// 模型那个在自己内部）：只在本层找会一路回退到内部 schema 键，把
			// "vision_provider_id" 直接显示给运营者。往上再找一层即可。
			const label = container?.querySelector(".field-label") ||
				container?.parentElement?.querySelector(".field-label") ||
				(control?.id
					? [...document.querySelectorAll("label[for]")].find(
						(item) => item.htmlFor === control.id,
					)
					: null);
			return (label?.textContent || key).replace(/\s+/g, " ").trim();
		});
	}

	async function saveConfig(event) {
		event.preventDefault();
		const state = getState();
		if (state.savingConfig) {
			showToast("正在保存…");
			return;
		}
		if (state.requiresConfigRefresh) {
			showToast(SAVE_UNKNOWN_MESSAGE);
			return;
		}
		if (!state.configLoaded) {
			showToast(configNotLoadedMessage());
			return;
		}
		// 白名单校验先行短路：两个校验器都会 focus 各自首个非法字段，
		// 若数值校验后跑，白名单的焦点会被抢走、错误提示跳变。
		if (!validateWhitelist({ focus: true })) {
			showToast("白名单有非法条目，请检查标红区域");
			return;
		}
		if (!validateAll()) {
			showToast("部分数值超出允许范围，请检查标红字段");
			return;
		}
		const e = getEls();
		// 保存按钮 disabled 时浏览器把焦点丢给 body，成功后要能回到触发点。
		// 契约用例会在没有 DOM 的 node 环境里直接跑 saveConfig，故先探 document。
		const trigger = typeof document === "undefined" ? null : document.activeElement;
		setState({ savingConfig: true });
		setSaving(true);
		e.configForm.inert = true;
		e.configForm.classList.add("is-saving");
		setSaveState("保存中", "saving");
		// 表单在保存期间 inert，此时 focus() 会被浏览器忽略：错误分支只登记
		// 目标，等 finally 解除 inert 后再聚焦。
		let pendingFocus = null;
		try {
			const body = buildConfigSaveBody(
				e.configForm,
				providerControls,
				state.configRevision,
			);
			let result;
			// 「不在列表中」是一个关于列表的断言，只有列表真的加载成功时才作得
			// 出来；不可用时无从比对，成因由 loadProviders 另行告知。留空是各控件
			// 自己声明的默认语义（见 app.js PROVIDER_CONTROLS 的 placeholder），
			// 由 providerNeedsManualInput 的空串分支放行。
			const listAvailable = isProviderListAvailable();
			const offList =
				listAvailable &&
				providerConfigKeys(e.configForm).some((key) =>
					providerNeedsManualInput(
						body[key],
						getProviderOptions(),
						listAvailable,
					),
				);
			if (offList) {
				showToast("部分 Provider ID 不在列表中，已继续保存，请确认拼写无误");
			}
			// 上面那条在成功时会被下文的保存结果 toast 覆盖（一次本地请求往返只有
			// 几十毫秒，用户读不到），所以成功文案里再带上一次：拼写风险提示是这条
			// 分支唯一的目的，不能被自己的成功回执吞掉。
			try {
				result = await apiPost("config", body);
			} catch (error) {
				markSaveUnknown();
				return;
			}
			if (!result || result.ok !== true) {
				const errorText = result?.error || "保存失败";
				if (result?.error_code === "STALE_WRITE") {
					// stale 之后必须刷新才能再提交（契约 §7 / DECISIONS 的
					// 「设置页配置写入与请求协调」）。新 revision 先收着：
					// requiresConfigRefresh 已挡住一切提交，它只在刷新成功后
					// 经 applyConfigPayload 被整体替换，但服务端给了就记下，
					// 免得将来放松前置守卫时这里又悄悄用回旧值。
					setState({
						configRevision: result.config_revision || state.configRevision,
						requiresConfigRefresh: true,
					});
					// 与 markSaveUnknown 同一句话术：四个保存入口全被禁用时，
					// 用户需要知道"为什么点不动"和"该做什么"。
					setSaveState("保存状态未知", "unknown");
					showToast(`${errorText}，请刷新配置后重试`, true);
					return;
				}
				setSaveState("保存失败", "error");
				const errorKey = configSaveKeys(e.configForm).find((key) =>
					String(errorText).startsWith(`${key} `),
				);
				// 字段级定位：后端校验文案以「键名 + 空格」前缀自带定位（见 webapi
				// 错误分级注释）。命中 number 控件时复用 validateField 的红字与
				// aria-invalid；白名单保留专属错误框。
				const numberField = errorKey
					? numberFields.find((f) => f.input.dataset.configKey === errorKey)
					: null;
				if (numberField) {
					numberField.input.setAttribute("aria-invalid", "true");
					numberField.error.textContent = errorText;
					numberField.error.classList.add("show");
					pendingFocus = numberField.input;
				} else if (
					errorKey === "whitelist_sessions" &&
					e.whitelistInput &&
					e.whitelistError
				) {
					e.whitelistInput.setAttribute("aria-invalid", "true");
					e.whitelistError.textContent = errorText;
					e.whitelistError.classList.add("show");
					pendingFocus = e.whitelistInput;
				}
				showToast(errorText, true);
				return;
			}
			const savedConfig = {
				...(result.config || result),
				ok: true,
				config_revision:
					result.config_revision || result.config?.config_revision,
				runtime_enabled:
					result.runtime_enabled ??
					result.config?.runtime_enabled ??
					getState().runtimeEnabled,
			};
			if (!isSuccessfulConfigPayload(savedConfig, configSaveKeys(e.configForm))) {
				markSaveUnknown();
				return;
			}
			const adjusted = Array.isArray(result.adjusted_fields)
				? result.adjusted_fields
				: [];
			// 保存即一次写入：在途 refresh 的快照可能早于本次保存，而
			// setDirty(false) 不推进 editEpoch。先推进 epoch 让那些迟到响应作废，
			// 否则它们会用旧快照覆盖已保存的值并回退 configRevision。
			coordinator.markEdited();
			applyConfigPayload(savedConfig);
			setSaveState("已保存", "ok");
			const labels = adjustedFieldLabels(adjusted);
			const savedText = labels.length
				? `配置已保存，已规范化：${labels.join("、")}`
				: "配置已保存";
			showToast(
				offList
					? `${savedText}（部分 Provider ID 不在列表中，请确认拼写）`
					: savedText,
			);
		} finally {
			setState({ savingConfig: false });
			e.configForm.classList.remove("is-saving");
			e.configForm.inert = false;
			setSaving(false);
			// 先解除 disabled 再回焦：按钮仍 disabled 时 focus() 会被忽略。
			if (pendingFocus) {
				revealAndFocus(pendingFocus);
				pendingFocus = null;
			} else if (getState().requiresConfigRefresh && e.refreshBtn) {
				// 保存入口全被禁用（状态未知 / STALE），下一步唯一能做的就是刷新；
				// 窄屏刷新收在「更多操作」菜单里，菜单收起时聚焦它的触发器。
				const refreshHidden = e.refreshBtn.closest?.("[hidden]");
				const target = refreshHidden && e.moreActionsBtn ? e.moreActionsBtn : e.refreshBtn;
				target.focus({ preventScroll: true });
			} else if (
				trigger &&
				typeof trigger.focus === "function" &&
				trigger.isConnected !== false &&
				// 只在焦点真的丢了才回填：慢保存期间用户可能已经移到别的控件。
				document.activeElement === document.body
			) {
				trigger.focus({ preventScroll: true });
			}
		}
	}
	// 刷新响应被协调器作废（loadConfig 返回 false）时的口径：表单仍脏，说明用户的
	// 编辑还在盘外，给「保存后再刷新」的操作指引；表单已干净时无法仅凭 false 判断
	// 具体竞态来源，因此用中性说明，不虚构「有未保存改动」。此处只读 state，不推进
	// 任何 epoch。
	function lateRefreshMessage() {
		return getState().isDirty
			? "检测到未保存改动，已保留当前内容，请保存后再刷新"
			: "响应未应用，已保留当前内容";
	}
	async function cleanupImageCache() {
		const e = getEls();
		if (!e.cleanupImageCacheBtn) return;
		e.cleanupImageCacheBtn.disabled = true;
		if (e.cleanupImageCacheState)
			e.cleanupImageCacheState.textContent = "清理中…";
		try {
			const result = await apiPost("image-cache/cleanup");
			if (!result || result.ok !== true)
				throw new Error(result?.error || "图片缓存清理失败");
			const removed = Number(result.removed || 0);
			// 状态栏与 toast 共用同一句：分开写会让两侧文案各说各话。
			const summary = removed
				? `已清理 ${removed} 个过期图片`
				: "没有需要清理的过期图片";
			if (e.cleanupImageCacheState) e.cleanupImageCacheState.textContent = summary;
			showToast(summary);
		} catch (error) {
			if (e.cleanupImageCacheState)
				e.cleanupImageCacheState.textContent = "清理失败";
			showToast(error.message || "图片缓存清理失败");
		} finally {
			e.cleanupImageCacheBtn.disabled = false;
		}
	}
	return {
		setSaveState,
		setDirty,
		attachDirtyListeners,
		setSaving,
		setupValidation,
		loadConfig,
		saveConfig,
		cleanupImageCache,
		configNotLoadedMessage,
		lateRefreshMessage,
	};
}

// 配置未加载完时禁止写操作（保存、重置提示词）的统一判据与文案。
// 两处调用点共用：提示文案分叉会让同一个前置条件对用户呈现两种说法，
// 而判据分叉更危险，一处放开、一处仍拦时，被放开的那处会把空表单写成盘。
function configNotLoadedMessage() {
	return "配置尚未成功加载，请先刷新页面";
}
