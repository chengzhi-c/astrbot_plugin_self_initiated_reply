import { providerNeedsManualInput } from "./frontend-core.mjs";
/**
 * @param {{select: HTMLSelectElement|null, input: HTMLInputElement|null,
 *          button: HTMLButtonElement|null, placeholder: string}} refs
 * @param {{ getOptions: () => any[], isListAvailable: () => boolean,
 *           showToast: (msg: string) => void,
 *           onModeChange?: (manual: boolean) => void,
 *           onDirty?: () => void }} deps
 */
export function createProviderControl(refs, deps) {
  let manual = false;
  const { getOptions, isListAvailable, showToast, onModeChange } = deps;
  function setManual(enabled, focusInput = false) {
    manual = Boolean(enabled);
    if (refs.button) {
      refs.button.textContent = manual ? "使用列表" : "手动输入";
      refs.button.setAttribute("aria-expanded", String(manual));
    }
    if (refs.select) refs.select.hidden = manual;
    if (refs.input) refs.input.hidden = !manual;
    if (onModeChange) onModeChange(manual);
    if (manual && focusInput && refs.input) refs.input.focus();
  }
  function value() {
    if (manual) return refs.input ? refs.input.value.trim() : "";
    return refs.select ? refs.select.value.trim() : "";
  }
  function render() {
    if (!refs.select) return;
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
    if (!providerNeedsManualInput(next, getOptions(), isListAvailable()) && refs.select) {
      refs.select.value = next;
      if (refs.input) refs.input.value = "";
      setManual(false);
      return;
    }
    if (refs.input) refs.input.value = next;
    setManual(true);
  }
  if (refs.button) {
    refs.button.addEventListener("click", () => {
      // 只是换输入方式不改配置值：标脏与否看前后取值是否真有变化，
      // 否则用户点一下「手动输入」再点回「使用列表」就会留下一个假的未保存标记。
      const before = value();
      if (manual) {
        sync(refs.input ? refs.input.value.trim() : "");
        if (manual) showToast("当前 Provider 不在列表中，继续保留手动输入");
      } else {
        if (refs.input) refs.input.value = refs.select ? refs.select.value || "" : "";
        setManual(true, true);
      }
      if (deps.onDirty && value() !== before) deps.onDirty();
    });
  }
  if (refs.select) {
    refs.select.addEventListener("change", () => {
      if (refs.input) refs.input.value = "";
    });
  }
  return { value, render, sync, setManual };
}
