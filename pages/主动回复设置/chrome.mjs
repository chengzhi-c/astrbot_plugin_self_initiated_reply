const MORE_ACTIONS_MEDIA = "(max-width: 460px)";
const DIM_KEY = "selfreply-dim";
const BOLD_KEY = "selfreply-bold";
// Keep in sync with index.html mobile tabbar data-target values.
const TAB_GROUPS = {
  selfStat: "selfStat",
  "sec-scope": "sec-scope",
  "sec-triggers": "sec-scope",
  "sec-decision": "sec-decision",
  "sec-runtime": "sec-runtime",
  "sec-vision": "sec-runtime",
};
const REDUCED_MOTION_MEDIA = "(prefers-reduced-motion: reduce)";
// MediaQueryList 是活对象：`.matches` 会随环境变化，故惰性持有一份单例复用，
// 不必每次调用都新建（旧实现每次 matchMedia，等于每处平滑滚动各建一份）。
let reducedMotionQuery = null;
function prefersReducedMotion() {
  if (!window.matchMedia) return false;
  if (!reducedMotionQuery) reducedMotionQuery = window.matchMedia(REDUCED_MOTION_MEDIA);
  return reducedMotionQuery.matches;
}
export function setupMoreActionsMenu(els) {
  if (!els.moreActions || !els.moreActionsBtn || !els.moreActionsMenu) return;
  const media = window.matchMedia(MORE_ACTIONS_MEDIA);
  const setOpen = (open, focusMenu = false) => {
    const compact = media.matches;
    const visible = compact && Boolean(open);
    els.moreActionsMenu.hidden = compact ? !visible : false;
    // 桌面菜单常显、trigger 隐藏，不渲染折叠态。
    if (compact) els.moreActionsBtn.setAttribute("aria-expanded", String(visible));
    else els.moreActionsBtn.removeAttribute("aria-expanded");
    if (visible && focusMenu) {
      const first = els.moreActionsMenu.querySelector("button:not([disabled])");
      window.requestAnimationFrame(() => first?.focus());
    }
  };
  // 关闭时若焦点还在菜单里（点了菜单项 / 键盘操作后），隐藏菜单会让焦点掉到
  // body，键盘用户的下一次 Tab 从文档开头重新开始。焦点不在菜单内时不还
  // （点页面别处关闭、断点切换），免得把用户正处的焦点抢到触发器上。
  // 桌面端菜单常显、trigger 是 display:none，focus() 对它本就是 no-op。
  const closeMenu = () => {
    const restoreFocus = Boolean(
      document.activeElement &&
        els.moreActionsMenu.contains(document.activeElement),
    );
    setOpen(false);
    if (restoreFocus) els.moreActionsBtn.focus();
  };
  els.moreActionsBtn.addEventListener("click", () => {
    setOpen(els.moreActionsMenu.hidden, true);
  });
  els.moreActionsMenu.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", closeMenu);
  });
  document.addEventListener("click", (event) => {
    if (media.matches && !els.moreActions.contains(event.target)) closeMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !media.matches || els.moreActionsMenu.hidden) return;
    event.preventDefault();
    closeMenu();
    els.moreActionsBtn.focus();
  });
  media.addEventListener("change", closeMenu);
  closeMenu();
}
export function updateNavFades(els) {
  if (!els.sidenavList) return;
  const list = els.sidenavList;
  const startFade = document.querySelector(".sidenav-fade-start");
  const endFade = document.querySelector(".sidenav-fade-end");
  if (startFade) startFade.classList.toggle("is-hidden", list.scrollLeft <= 4);
  if (endFade) {
    const atEnd = list.scrollLeft + list.clientWidth >= list.scrollWidth - 4;
    endFade.classList.toggle("is-hidden", atEnd);
  }
}
function syncMobileTabs(els, active) {
  if (!els.mobileTabbar || !active) return;
  const group = TAB_GROUPS[active.dataset.target] || active.dataset.target;
  els.mobileTabbar.querySelectorAll(".mtab").forEach((tab) => {
    const current = tab.dataset.target === group;
    tab.classList.toggle("is-current", current);
    if (current) tab.setAttribute("aria-current", "location");
    else tab.removeAttribute("aria-current");
  });
}
function setCurrentNav(els, active) {
  document.querySelectorAll(".sidenav-link").forEach((link) => {
    const on = link === active;
    link.classList.toggle("is-current", on);
    if (on) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  });
  updateNavFades(els);
  if (active && els.sidenavList && window.matchMedia("(max-width: 1024px)").matches) {
    const linkRect = active.getBoundingClientRect();
    const listRect = els.sidenavList.getBoundingClientRect();
    if (linkRect.left < listRect.left + 2 || linkRect.right > listRect.right - 2) {
      const delta = linkRect.left - listRect.left - (listRect.width - linkRect.width) / 2;
      els.sidenavList.scrollTo({
        left: els.sidenavList.scrollLeft + delta, behavior: prefersReducedMotion() ? "auto" : "smooth", });
    }
  }
  syncMobileTabs(els, active);
}
// 章节跳转单点：侧栏链接与移动 tab 共用（差别只在 preventDefault /
// history 与「谁负责 setCurrentNav」，由调用方各自处理）。
function jumpToSection(targetId) {
  const target = document.getElementById(targetId);
  if (!target) return false;
  const details = target.closest("details");
  if (details && !details.open) details.open = true;
  target.scrollIntoView({
    behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
  return true;
}
export function setupNav(els) {
  const links = Array.from(document.querySelectorAll(".sidenav-link"));
  if (!links.length) return;
  const byTarget = new Map(links.map((link) => [link.dataset.target, link]));
  links.forEach((link) => {
    link.addEventListener("click", (e) => {
      if (!jumpToSection(link.dataset.target)) return;
      e.preventDefault();
      try {
        history.replaceState(null, "", "#" + link.dataset.target);
      } catch (_) {
        /* ignore */
      }
      setCurrentNav(els, link);
    });
  });
  if ("IntersectionObserver" in window) {
    const sections = links.map((l) => document.getElementById(l.dataset.target)).filter(Boolean);
    const observer = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            const link = byTarget.get(entry.target.id);
            if (link) setCurrentNav(els, link);
          }
        });
      }, { rootMargin: "-28% 0px -62% 0px", threshold: 0 });
    sections.forEach((s) => observer.observe(s));
  }
  setCurrentNav(els, links[0]);
}
export function setupMobileTabs(els) {
  if (!els.mobileTabbar) return;
  const byTarget = new Map(
    Array.from(document.querySelectorAll(".sidenav-link")).map((link) => [
      link.dataset.target,
      link,
    ]),
  );
  els.mobileTabbar.querySelectorAll(".mtab").forEach((tab) => {
    tab.addEventListener("click", () => {
      if (!jumpToSection(tab.dataset.target)) return;
      const link = byTarget.get(tab.dataset.target);
      if (link) setCurrentNav(els, link);
    });
  });
}
export function updateTopbarStuck(els) {
  if (!els.topbar) return;
  const y = window.scrollY || document.documentElement.scrollTop || 0;
  els.topbar.classList.toggle("is-stuck", y > 8);
}
/* 顶栏实际高度写回 --topbar-h：静态令牌（88/64/62）与实测值对不上，1024px
   断点内实测 83px，换行断点实测 115px。该令牌只被 .sidenav 的 sticky top 与
   scroll-margin-top 消费，与 .topbar 自身高度完全独立（实测把变量设成
   200px/20px，顶栏高度恒为 83px），因此不存在「高度→变量→布局→高度」的
   正反馈；只写一次即收敛（实测 writes=1）。静态值保留为无 JS 时的兜底。
   只在整数值变化时写，避免无谓的样式重算。 */
export function syncTopbarHeight(els) {
  if (!els.topbar || typeof window.ResizeObserver !== "function") return;
  const write = () => {
    const height = Math.round(els.topbar.getBoundingClientRect().height);
    if (!height) return;
    const root = document.documentElement;
    if (root.style.getPropertyValue("--topbar-h") === `${height}px`) return;
    root.style.setProperty("--topbar-h", `${height}px`);
  };
  new window.ResizeObserver(write).observe(els.topbar);
  write();
}
export function createScrollHandler(els) {
  let ticking = false;
  return () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      updateTopbarStuck(els);
      ticking = false;
    });
  };
}
export function applyDim(on) {
  document.documentElement.classList.toggle("dimmed", on);
  const btn = document.getElementById("dimBtn");
  if (btn) setPressed(btn, on);
  try {
    localStorage.setItem(DIM_KEY, on ? "1" : "0");
  } catch (e) {
    /* ignore */
  }
}
export function applyBold(on) {
  document.documentElement.classList.toggle("bold-text", on);
  const btn = document.getElementById("boldBtn");
  if (btn) setPressed(btn, on);
  try {
    localStorage.setItem(BOLD_KEY, on ? "1" : "0");
  } catch (e) {
    /* ignore */
  }
}
// 开关的视觉态与 aria-pressed 同源：只改类会让读屏用户看到一个"没有状态"
// 的按钮（.active 是纯视觉约定）。
function setPressed(button, on) {
  button.classList.toggle("active", on);
  button.setAttribute("aria-pressed", String(Boolean(on)));
}
let dimBoldTouched = false;
function markDimBoldTouched() {
  dimBoldTouched = true;
}
export function dimBoldWasTouched() {
  return dimBoldTouched;
}
/* 主题与压暗/粗体同属"用户即时选择优先于迟到服务端值"：GET ui/theme 在途最长
   FETCH_TIMEOUT_MS，期间点过主题就不得被旧响应覆盖（DECISIONS「设置页 chrome」）。 */
let themeTouched = false;
export function markThemeTouched() {
  themeTouched = true;
}
export function themeWasTouched() {
  return themeTouched;
}
export function restoreDimBold() {
  try {
    if (localStorage.getItem(DIM_KEY) === "1") applyDim(true);
    if (localStorage.getItem(BOLD_KEY) === "1") applyBold(true);
  } catch (e) {
    /* ignore */
  }
}
export function bindDimBoldButtons(onChange) {
  const dimBtn = document.getElementById("dimBtn");
  const boldBtn = document.getElementById("boldBtn");
  if (dimBtn) {
    dimBtn.addEventListener("click", () => {
      markDimBoldTouched();
      applyDim(!document.documentElement.classList.contains("dimmed"));
      onChange?.();
    });
  }
  if (boldBtn) {
    boldBtn.addEventListener("click", () => {
      markDimBoldTouched();
      applyBold(!document.documentElement.classList.contains("bold-text"));
      onChange?.();
    });
  }
}
export function hideBoot(els) {
  if (els.boot) els.boot.classList.add("is-hidden");
  // 纯"模块已启动"的测试锚：样式表不消费 is-ready。改渲染时序请动 .boot 的
  // is-hidden，不要以为这个类控制任何视觉状态。
  document.body.classList.add("is-ready");
}
