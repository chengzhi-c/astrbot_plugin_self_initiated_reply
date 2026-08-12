/** 导航 / 滚动 / 更多菜单 / 亮度粗体等壳层 UI。 */

const MORE_ACTIONS_MEDIA = "(max-width: 460px)";
const DIM_KEY = "selfreply-dim";
const BOLD_KEY = "selfreply-bold";

const TAB_GROUPS = {
  selfStat: "selfStat",
  "sec-scope": "sec-scope",
  "sec-triggers": "sec-scope",
  "sec-decision": "sec-decision",
  "sec-runtime": "sec-runtime",
  "sec-vision": "sec-runtime",
};

export function prefersReducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function setupMoreActionsMenu(els) {
  if (!els.moreActions || !els.moreActionsBtn || !els.moreActionsMenu) return;
  const media = window.matchMedia(MORE_ACTIONS_MEDIA);
  const setOpen = (open, focusMenu = false) => {
    const compact = media.matches;
    const visible = compact && Boolean(open);
    els.moreActions.classList.toggle("is-open", visible);
    els.moreActionsMenu.hidden = compact ? !visible : false;
    els.moreActionsBtn.setAttribute("aria-expanded", String(visible));
    if (visible && focusMenu) {
      const first = els.moreActionsMenu.querySelector("button:not([disabled])");
      window.requestAnimationFrame(() => first?.focus());
    }
  };
  const closeMenu = () => setOpen(false);
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

export function setCurrentNav(els, active) {
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
        left: els.sidenavList.scrollLeft + delta,
        behavior: prefersReducedMotion() ? "auto" : "smooth",
      });
    }
  }
  syncMobileTabs(els, active);
}

export function setupNav(els) {
  const links = Array.from(document.querySelectorAll(".sidenav-link"));
  if (!links.length) return;
  const byTarget = new Map(links.map((link) => [link.dataset.target, link]));
  links.forEach((link) => {
    link.addEventListener("click", (e) => {
      const target = document.getElementById(link.dataset.target);
      if (!target) return;
      e.preventDefault();
      const details = target.closest("details");
      if (details && !details.open) details.open = true;
      target.scrollIntoView({
        behavior: prefersReducedMotion() ? "auto" : "smooth",
        block: "start",
      });
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
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            const link = byTarget.get(entry.target.id);
            if (link) setCurrentNav(els, link);
          }
        });
      },
      { rootMargin: "-28% 0px -62% 0px", threshold: 0 }
    );
    sections.forEach((s) => observer.observe(s));
  }
  setCurrentNav(els, links[0]);
}

export function setupMobileTabs(els) {
  if (!els.mobileTabbar) return;
  els.mobileTabbar.querySelectorAll(".mtab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const target = document.getElementById(tab.dataset.target);
      if (!target) return;
      const details = target.closest("details");
      if (details && !details.open) details.open = true;
      target.scrollIntoView({
        behavior: prefersReducedMotion() ? "auto" : "smooth",
        block: "start",
      });
      const link = document.querySelector(
        '.sidenav-link[data-target="' + tab.dataset.target + '"]'
      );
      if (link) setCurrentNav(els, link);
    });
  });
}

export function updateTopbarStuck(els) {
  if (!els.topbar) return;
  const y = window.scrollY || document.documentElement.scrollTop || 0;
  els.topbar.classList.toggle("is-stuck", y > 8);
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
  if (btn) btn.classList.toggle("active", on);
  try {
    localStorage.setItem(DIM_KEY, on ? "1" : "0");
  } catch (e) {
    /* ignore */
  }
}

export function applyBold(on) {
  document.documentElement.classList.toggle("bold-text", on);
  const btn = document.getElementById("boldBtn");
  if (btn) btn.classList.toggle("active", on);
  try {
    localStorage.setItem(BOLD_KEY, on ? "1" : "0");
  } catch (e) {
    /* ignore */
  }
}

export function restoreDimBold() {
  try {
    if (localStorage.getItem(DIM_KEY) === "1") applyDim(true);
    if (localStorage.getItem(BOLD_KEY) === "1") applyBold(true);
  } catch (e) {
    /* ignore */
  }
}

export function bindDimBoldButtons() {
  const dimBtn = document.getElementById("dimBtn");
  const boldBtn = document.getElementById("boldBtn");
  if (dimBtn) {
    dimBtn.addEventListener("click", () =>
      applyDim(!document.documentElement.classList.contains("dimmed"))
    );
  }
  if (boldBtn) {
    boldBtn.addEventListener("click", () =>
      applyBold(!document.documentElement.classList.contains("bold-text"))
    );
  }
}

export function hideBoot(els) {
  if (els.boot) els.boot.classList.add("is-hidden");
  document.body.classList.add("is-ready");
  if (els.selfStat) els.selfStat.classList.add("is-entered");
}
