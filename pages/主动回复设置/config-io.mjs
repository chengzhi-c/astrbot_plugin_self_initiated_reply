import {
	numberFieldError,
	parseWhitelist,
	summarizeWhitelist,
	uniqueWhitelistItems,
	validateWhitelistLines,
} from "./config-form.mjs";
import {
	createConfigRequestCoordinator,
	isSuccessfulConfigPayload,
	missingConfigPayloadKeys,
	providerNeedsManualInput,
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
		judgeProviderControl,
		visionProviderControl,
		visionJudgeProviderControl,
		fmtBool,
		requestCoordinator,
		getProviderOptions = () => [],
		isProviderListAvailable = () => false,
	} = deps;
	const coordinator = requestCoordinator || createConfigRequestCoordinator();
	// 三个 Provider 控件的唯一注册表：读写表单与保存请求共用同一映射。
	const providerControls = {
		judge: judgeProviderControl,
		vision: visionProviderControl,
		visionJudge: visionJudgeProviderControl,
	};
	let numberFields = [];
	let saveStateKind = "";
	function els() {
		return getEls();
	}
	// 「保存状态未知」的单点：POST 抛错与响应体校验失败是同一语义（无法确认服务
	// 端是否已写入），状态、toast 文案与保存前置守卫同进同退，否则三处文案会各自漂移。
	const SAVE_UNKNOWN_MESSAGE = "保存状态未知，请刷新配置后重试";
	function markSaveUnknown() {
		setState({ requiresConfigRefresh: true });
		setSaveState("保存状态未知", "error");
		showToast(SAVE_UNKNOWN_MESSAGE, true);
	}
	function setSaveState(message, state) {
		saveStateKind = state;
		const e = els();
		if (!e.configSaveState) return;
		e.configSaveState.textContent = message;
		e.configSaveState.classList.remove("is-pending", "is-ok", "is-error");
		const cssKind = state === "dirty" ? "pending" : state;
		if (cssKind) e.configSaveState.classList.add(`is-${cssKind}`);
		if (e.navSaveState) {
			if (state === "ok") e.navSaveState.textContent = "已保存";
			else if (state === "error") e.navSaveState.textContent = "保存失败";
			else if (state === "saving") e.navSaveState.textContent = "保存中";
		}
		if (e.mobileSaveState) {
			e.mobileSaveState.textContent = message || (state ? "" : "已同步");
			e.mobileSaveState.classList.remove("is-pending", "is-ok", "is-error");
			if (cssKind) e.mobileSaveState.classList.add(`is-${cssKind}`);
		}
	}
	function setDirty(dirty = true) {
		if (dirty) coordinator.markEdited();
		setState({ isDirty: dirty });
		const e = els();
		// 四个脏值入口同源：桌面顶栏、移动端保存按钮、底部保存按钮与侧栏圆点。
		// saveMobileBtn 必须在内，否则窄屏下唯一可见的保存入口没有未保存角标。
		[e.saveTopBtn, e.saveMobileBtn, e.saveBottomBtn, e.navSaveDot].forEach((btn) => {
			if (btn) btn.classList.toggle("is-dirty", dirty);
		});
		if (e.navSaveState)
			e.navSaveState.textContent = dirty ? "有未保存改动" : "已同步";
		if (dirty && saveStateKind !== "saving")
			setSaveState("有未保存改动", "dirty");
		else if (!dirty && saveStateKind === "dirty") setSaveState("", "");
	}
	function attachDirtyListeners() {
		const e = els();
		if (!e.configForm) return;
		e.configForm.addEventListener("change", () => setDirty(true));
		e.configForm.addEventListener("input", (ev) => {
			if (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA")
				setDirty(true);
		});
	}
	function setSaving(loading) {
		const e = els();
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
		if (e.refreshBtn) e.refreshBtn.disabled = loading;
	}
	function updateWhitelistFeedback() {
		const e = els();
		if (!e.whitelistInput) return;
		const text = e.whitelistInput.value;
		// 只 parse 一次：计数与摘要共用同一结果（两者共用同一套分隔/去重规则，
		// 分别调用会让同一份文本被切分三遍）。
		const parsed = parseWhitelist(text);
		// 计数取**去重后**的长度：这个读数的标签是「生效会话」，而后端把裸群号与
		// 其群 UMO 视为同一会话（utils.session_whitelisted）。用未去重的 parsed.length
		// 时，`12347` + `qq:GroupMessage:12347` 会让顶栏读 2 而下方的摘要读
		// 「已识别 1 个有效会话」两条都是 aria-live="polite"，读屏连续播报
		// 两个互相抵消的数字。
		const unique = uniqueWhitelistItems(text, parsed);
		if (e.whitelistCount) e.whitelistCount.textContent = String(unique.length);
		if (e.whitelistSummary)
			e.whitelistSummary.textContent = summarizeWhitelist(text, parsed);
	}
	function formatWhitelist() {
		const e = els();
		if (!e.whitelistInput) return;
		const unique = uniqueWhitelistItems(e.whitelistInput.value);
		e.whitelistInput.value = unique.join("\n");
		updateWhitelistFeedback();
		setDirty(true);
		validateWhitelist();
		showToast(
			unique.length ? `已整理并保留 ${unique.length} 个会话` : "白名单已清空",
		);
	}
	function setupValidation() {
		const e = els();
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
				updateWhitelistFeedback();
				validateWhitelist();
			});
			e.whitelistInput.addEventListener("blur", () => validateWhitelist());
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
	function validateWhitelist({ focus = false } = {}) {
		const e = els();
		if (!e.whitelistInput || !e.whitelistError) return true;
		const errors = validateWhitelistLines(e.whitelistInput.value);
		if (errors.length > 0) {
			const first = errors[0];
			e.whitelistInput.setAttribute("aria-invalid", "true");
			e.whitelistError.textContent = `第 ${first.line} 项${first.reason}：${first.item.slice(0, 24)}`;
			e.whitelistError.classList.add("show");
			// 只在保存路径抢焦点：input/blur 上抢会把用户困在该字段
			// （点其他字段被拽回、Tab 逃不出），可达性缺陷。
			if (focus) revealAndFocus(e.whitelistInput);
			return false;
		}
		e.whitelistInput.removeAttribute("aria-invalid");
		e.whitelistError.classList.remove("show");
		e.whitelistError.textContent = "";
		return true;
	}
	function applyConfigPayload(config) {
		const e = els();
		loadConfigControls(e.configForm, config, providerControls);
		// 只有 GET /config 携带 decision_prompt_default（面板视图键）；POST 返回的
		// config 是持久配置，不含它。此处若用 decision_prompt_template 兜底，会把用户
		// 刚提交的值写成"默认"，「恢复默认提示词」随之变成空操作。
		if (config.decision_prompt_default) {
			e.decisionPromptInput.dataset.defaultPrompt = config.decision_prompt_default;
		}
		const whitelist = parseWhitelist(e.whitelistInput.value);
		e.whitelistInput.value = whitelist.join("\n");
		updateWhitelistFeedback();
		if (e.decisionModelStatus) {
			const decisionOn = config.decision_model_enabled !== false;
			e.decisionModelStatus.textContent = fmtBool(decisionOn);
			setStatState(e.decisionModelStat, decisionOn ? "is-on" : "is-off");
		}
		renderPromptPreview();
		const runtimeOn = config.runtime_enabled !== false;
		e.selfStatus.textContent = config.enabled
			? runtimeOn
				? "启用"
				: "已暂停（/off）"
			: "关闭";
		setStatState(e.selfStat, runtimeOn ? "is-on" : "is-off");
		setState({
			configLoaded: true,
			configRevision: config.config_revision,
			runtimeEnabled: runtimeOn,
			requiresConfigRefresh: false,
		});
		setDirty(false);
		if (e.configForm) e.configForm.inert = false;
		setSaving(false);
	}

	async function loadConfig({ force = false } = {}) {
		const e = els();
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
		const form = els().configForm;
		return keys.map((key) => {
			const control = configControls(form).find(
				(item) => item.dataset.configKey === key,
			);
			const container = control?.closest(
				".field, .provider-field, .toggle-row, .master-switch",
			);
			const label = container?.querySelector(".field-label") ||
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
		const e = els();
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
			// 先滤掉留空：留空表示「用当前会话默认模型」，而列表不可用时
			// providerNeedsManualInput 对空串也返回 true，会把默认语义误报成
			// 「不在列表中」。
			const offList = providerConfigKeys(e.configForm)
				.filter((key) => String(body[key] ?? "").trim() !== "")
				.some((key) =>
					providerNeedsManualInput(
						body[key],
						getProviderOptions(),
						isProviderListAvailable(),
					),
				);
			if (offList) {
				showToast("部分 Provider ID 不在列表中，已继续保存，请确认拼写无误");
			}
			try {
				result = await apiPost("config", body);
			} catch (error) {
				markSaveUnknown();
				return;
			}
			if (!result || result.ok !== true) {
				const errorText = result?.error || "保存失败";
				if (result?.error_code === "STALE_WRITE") {
					setState({
						configRevision: result.config_revision || state.configRevision,
						requiresConfigRefresh: true,
					});
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
			showToast(
				labels.length
					? `配置已保存，已规范化：${labels.join("、")}`
					: "配置已保存",
			);
		} finally {
			setState({ savingConfig: false });
			e.configForm.classList.remove("is-saving");
			e.configForm.inert = false;
			if (pendingFocus) {
				revealAndFocus(pendingFocus);
				pendingFocus = null;
			}
			setSaving(false);
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
		const e = els();
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
