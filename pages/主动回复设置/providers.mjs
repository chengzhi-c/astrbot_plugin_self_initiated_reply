import { providerNeedsManualInput } from "./frontend-core.mjs";
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
 *           showToast: (msg: string) => void, onDirty: () => void,
 *           onModeChange?: (manual: boolean) => void }} deps
 */
export function createProviderControl(refs, deps) {
  let manual = false;
  const { getOptions, isListAvailable, showToast, onDirty, onModeChange } = deps;
  function setManual(enabled, focusInput = false) {
    manual = Boolean(enabled);
    // 容器类由本函数统一负责：三个 Provider 控件（judge / vision / visionJudge）
    // 共用这一处实现。只让 judge 的 onModeChange 加类时，vision 两个字段切手动
    // 后容器类恒为空，于是基类 `.provider-control` 的两列定义继续生效，按钮被拉成
    // 整行宽（实测 373px，judge 同态 78px）。
    // 注意这是「类没挂上」而不是 CSS 优先级问题：`.provider-field.manual
    // .provider-control` 含 3 个类（0,3,0），本就压过基类 `.provider-control`
    // （0,1,0），无需为 vision 另写规则。
    refs.field.classList.toggle("manual", manual);
    refs.button.textContent = manual ? "使用列表" : "手动输入";
    refs.button.setAttribute("aria-expanded", String(manual));
    refs.select.hidden = manual;
    refs.input.hidden = !manual;
    onModeChange?.(manual);
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
      if (manual) showToast("当前 Provider 不在列表中，继续保留手动输入");
    } else {
      refs.input.value = refs.select.value;
      setManual(true, true);
    }
    if (value() !== before) onDirty();
  });
  return { value, render, sync };
}
