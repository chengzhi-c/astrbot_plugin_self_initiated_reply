/**
 * @param {{select: HTMLSelectElement,
 *          input: HTMLInputElement, button: HTMLButtonElement,
 *          placeholder: string}} refs
 *          元素引用（多余字段被忽略）。四项都必传：调用方是 app.js 的
 *          PROVIDER_CONTROLS 表，脚本按字面量取的 id 都必须在 index.html 里声明，
 *          缺项就是页面写坏，不再逐处判空降级。``field`` 是包裹层，手动/列表切换由 ``setManual``
 *          在它上面挂 ``manual`` 类。
 * @param {{ getOptions: () => any[], isListAvailable: () => boolean,
 *           showToast: (msg: string) => void, onDirty: () => void }} deps
 */
export function createProviderControl(refs, deps) {
  let manual = false;
  const { getOptions, isListAvailable, showToast, onDirty } = deps;
  function setManual(enabled, focusInput = false) {
    manual = Boolean(enabled);
    // manual 类同时切换列表与手动输入的列布局。
    refs.field.classList.toggle("manual", manual);
    refs.button.textContent = manual ? "使用列表" : "手动输入";
    refs.button.setAttribute("aria-expanded", String(manual));
    refs.select.hidden = manual;
    refs.input.hidden = !manual;
    if (manual && focusInput) refs.input.focus();
  }
  function value() {
    return (manual ? refs.input : refs.select).value.trim();
  }
  function render() {
    const current = refs.select.value;
    refs.select.innerHTML = "";
    const fallback = document.createElement("option");
    fallback.value = "";
    fallback.textContent = refs.placeholder;
    refs.select.appendChild(fallback);
    getOptions().forEach((provider) => {
      const option = document.createElement("option");
      option.value = provider.id;
      option.textContent = provider.label || provider.id;
      refs.select.appendChild(option);
    });
    refs.select.value = current;
  }
  function sync(providerId) {
    const next = String(providerId || "").trim();
    if (!providerNeedsManualInput(next, getOptions(), isListAvailable())) {
      refs.select.value = next;
      refs.input.value = "";
      setManual(false);
      return;
    }
    refs.input.value = next;
    setManual(true);
  }
  refs.button.addEventListener("click", () => {
    // 只是换输入方式（列表 ↔ 手动）不改配置值：标脏与否看前后取值是否真有变化，
    // 否则用户点一下「手动输入」再点回「使用列表」就会留下一个假的未保存标记。
    const before = value();
    if (manual) {
      sync(refs.input.value.trim());
      // 「不在列表中」是关于列表的断言，只有列表真的加载成功时才作得出来；
      // 不可用时 sync 必然落回手动分支，此时提示等于凭空断言（config-io
      // 保存路径对同一判据已有同样的 listAvailable 守卫）。但静默不置一词也
      // 不对：两个识图控件没有常驻的列表状态位，点了按钮屏幕毫无变化。
      if (manual) {
        showToast(
          isListAvailable()
            ? "当前 Provider 不在列表中，继续保留手动输入"
            : "Provider 列表不可用，无法切回列表",
        );
      }
    } else {
      refs.input.value = refs.select.value;
      setManual(true, true);
    }
    if (value() !== before) onDirty();
  });
  return { value, render, sync };
}
/* Provider 手动态的判定：列表不可用时无从比对，恒为真（各控件自行决定兜底
   文案）；列表可用时才做 id 成员检查。原在 frontend-core.mjs，只因该文件不能
   import 本模块（config-io 与 providers 都依赖它，反向依赖会成环）。 */
export function providerNeedsManualInput(providerId, providers, listAvailable) {
  if (!listAvailable) return true;
  const value = String(providerId || "").trim();
  return value !== "" && !providers.some((provider) => provider?.id === value);
}

/**
 * 只能从列表里选的 Provider 控件：没有手动输入。
 *
 * 「填一个 ID」要求用户记住 Provider 标识，填错的表现是这一轮静默回落，
 * 排查起来毫无线索。已保存但当前不在列表里的 ID 仍然保留成一项（标注
 * 「不在列表」），否则刷新一次列表再保存就会把它悄悄删掉。
 *
 * @param {{select: HTMLSelectElement,
 *          placeholder?: string}} refs
 * @param {{ getOptions: () => any[], onDirty: () => void }} deps
 */
export function createProviderListControl(refs, deps) {
  const { getOptions, isListAvailable, onDirty } = deps;
  // 列表没加载成功时「不在列表」是凭空断言：所有 ID 都不在，标了等于没说。
  const missingLabel = (id) => (isListAvailable() ? `${id}（不在列表）` : id);

  function addOption(id, label) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = label;
    refs.select.appendChild(option);
  }

  function render() {
    const current = refs.select.value;
    refs.select.innerHTML = "";
    const fallback = document.createElement("option");
    fallback.value = "";
    fallback.textContent = refs.placeholder || "使用当前会话模型";
    refs.select.appendChild(fallback);
    const options = getOptions().map((provider) => ({
      id: provider.id,
      label: provider.label || provider.id,
    }));
    for (const option of options) addOption(option.id, option.label);
    if (current && !options.some((option) => option.id === current)) {
      addOption(current, missingLabel(current));
    }
    refs.select.value = current;
  }

  function sync(providerId) {
    const wanted = String(providerId || "").trim();
    if (
      wanted &&
      ![...refs.select.options].some((option) => option.value === wanted)
    ) {
      addOption(wanted, missingLabel(wanted));
    }
    refs.select.value = wanted;
  }

  refs.select.addEventListener("change", onDirty);
  return { value: () => refs.select.value.trim(), render, sync };
}

/**
 * 有序的多选 Provider 列表：下拉里挑一个「添加」进列表，列表可上下移动、可移除。
 *
 * 备用链是**有序**的，顺序就是尝试顺序。`<select multiple>` 的取值顺序恒为
 * DOM 顺序（即宿主列表顺序），用户无法表达优先级，所以这里用队列控件。
 *
 * @param {{field: HTMLElement, select: HTMLSelectElement,
 *          list: HTMLElement, button: HTMLButtonElement}} refs
 * @param {{ getOptions: () => any[], onDirty: () => void }} deps
 */
export function createProviderQueueControl(refs, deps) {
  const { getOptions, isListAvailable, maxItems, onDirty } = deps;
  let items = [];

  function labelOf(id) {
    const match = getOptions().find((provider) => provider.id === id);
    if (match) return match.label || match.id;
    return isListAvailable() ? `${id}（不在列表）` : id;
  }

  function addOption(id, label) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = label;
    refs.select.appendChild(option);
  }

  function renderSelect() {
    const current = refs.select.value;
    refs.select.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "选择备用模型";
    refs.select.appendChild(placeholder);
    for (const provider of getOptions()) {
      if (items.includes(provider.id)) continue;
      addOption(provider.id, provider.label || provider.id);
    }
    refs.select.value = items.includes(current) ? "" : current;
    // 按「下拉里还有没有可选的一项」判断，而不是比长度：items 里可能有已不在
    // 列表中的旧 ID，长度相等时仍可能还有可选项，比长度会把按钮误禁用。
    const full = items.length >= maxItems;
    const wasFocused = document.activeElement === refs.button;
    refs.button.disabled = full || refs.select.options.length <= 1;
    if (refs.button.disabled && wasFocused) refs.select.focus();
    refs.button.title = full
      ? `最多 ${maxItems} 个备用模型`
      : refs.button.disabled
        ? "没有可添加的模型"
        : "";
  }

  function announce(text) {
    if (refs.status) refs.status.textContent = text;
  }

  function renderList(focusOverride) {
    // 每次改动都整表重渲染，旧按钮连同焦点一起被销毁。不归位时键盘用户按一次
    // ↑/↓ 焦点就掉到 body，接下来 Tab 从头开始；鼠标连点则会落到「原位新按钮」
    // 上，把刚做的移动再移回去（看起来弹回）。按行位置和动作恢复焦点。
    const active = document.activeElement;
    const focusKey =
      focusOverride === undefined
        ? active && refs.list.contains(active) && active.dataset.action
          ? { index: Number(active.dataset.index), action: active.dataset.action }
          : null
        : focusOverride;
    refs.list.innerHTML = "";
    items.forEach((id, index) => {
      const row = document.createElement("li");
      row.className = "fallback-item";
      // 序号让「这是有先后的队列」一眼可见，否则整行看着像第二个下拉框
      const label = labelOf(id);
      const name = document.createElement("span");
      name.textContent = `${index + 1}. ${label}`;
      // 窄屏下名称会被省略号截断，悬停要能看全
      name.title = label;
      row.appendChild(name);
      const actions = [
        ["上移", "↑", () => move(index, index - 1), index === 0, "up"],
        ["下移", "↓", () => move(index, index + 1), index === items.length - 1, "down"],
        ["移除", "×", () => remove(index), false, "remove"],
      ];
      for (const [label, glyph, handler, disabled, action] of actions) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn-icon";
        button.textContent = glyph;
        // 序号进可访问名：两个同名 provider 同时在队列里时，只念名字
        // 会让读屏用户分不清在操作哪一条。
        button.setAttribute("aria-label", `${label}第 ${index + 1} 项 ${labelOf(id)}`);
        button.dataset.index = String(index);
        button.dataset.action = action;
        button.disabled = disabled;
        button.addEventListener("click", handler);
        row.appendChild(button);
      }
      refs.list.appendChild(row);
    });
    refs.list.hidden = items.length === 0;
    if (focusKey) {
      const buttons = [...refs.list.querySelectorAll("button")];
      const target =
        buttons.find(
          (button) =>
            button.dataset.index === String(focusKey.index) &&
            button.dataset.action === focusKey.action &&
            !button.disabled,
        ) ||
        buttons.find(
          (button) => button.dataset.index === String(focusKey.index) && !button.disabled,
        );
      if (target) target.focus();
    }
  }

  function render(focusOverride) {
    renderSelect();
    renderList(focusOverride);
  }

  function move(from, to) {
    if (to < 0 || to >= items.length) return;
    const action = document.activeElement?.dataset?.action;
    const [moved] = items.splice(from, 1);
    items.splice(to, 0, moved);
    render({ index: to, action });
    announce(`${labelOf(moved)} 已移到第 ${to + 1} 项，共 ${items.length} 项`);
    onDirty();
  }

  function remove(index) {
    const [removed] = items.splice(index, 1);
    const focusKey = items.length
      ? { index: Math.min(index, items.length - 1), action: "remove" }
      : null;
    render(focusKey);
    if (!items.length) refs.select.focus();
    announce(`已移除 ${labelOf(removed)}，还剩 ${items.length} 项`);
    onDirty();
  }

  function value() {
    return [...items];
  }

  function sync(ids) {
    items = (Array.isArray(ids) ? ids : []).map(String).filter(Boolean);
    render();
  }

  refs.button.addEventListener("click", () => {
    const picked = refs.select.value;
    if (!picked) return;
    items.push(picked);
    refs.select.value = "";
    render();
    announce(`已添加 ${labelOf(picked)}，共 ${items.length} 项`);
    onDirty();
  });
  return { value, render, sync };
}
