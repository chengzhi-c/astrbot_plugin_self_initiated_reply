/**
 * @param {{field: HTMLElement, select: HTMLSelectElement,
 *          input: HTMLInputElement, button: HTMLButtonElement,
 *          placeholder: string}} refs
 *          元素引用（多余字段被忽略）。四项都必传：调用方是 app.js 的
 *          PROVIDER_CONTROLS 表，元素 id 存在性由 frontend_contract 的
 *          「脚本按字面量取的 id 都在 index.html 里声明」钉住，缺项就是页面写坏，
 *          不再逐处判空降级。``field`` 是包裹层，手动/列表切换由 ``setManual``
 *          在它上面挂 ``manual`` 类。
 * @param {{ getOptions: () => any[], isListAvailable: () => boolean,
 *           showToast: (msg: string) => void, onDirty: () => void }} deps
 */
export function createProviderControl(refs, deps) {
  let manual = false;
  const { getOptions, isListAvailable, showToast, onDirty } = deps;
  function setManual(enabled, focusInput = false) {
    manual = Boolean(enabled);
    // 容器类由本函数统一负责，三个 Provider 控件（judge / vision / visionJudge）
    // 共用这一处实现。缺少这个类时基类 `.provider-control` 的两列定义仍生效，
    // 按钮会被拉成整行宽（实测 373px，正常 78px）。这是「类没挂上」而不是 CSS
    // 优先级问题：`.provider-field.manual .provider-control` 含 3 个类（0,3,0），
    // 本就压过基类 `.provider-control`（0,1,0）。
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
