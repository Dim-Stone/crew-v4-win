// 深浅主题开关 + 左栏折叠。
//
// 两件事放在一个文件里，是因为它们的形状完全一样：**一个纯界面状态 +
// 记住它**。而且两者都有同一个坑——状态必须在**首屏之前**定下来，
// 否则会先画出默认状态再跳一下（主题是底色闪一下，折叠是宽度抖一下）。
// 所以真正的首屏设置都在 base.html 的内联脚本里，这里只负责"点击之后"。

const KEY = "crew-theme";
const RAIL_KEY = "crew-rail";
const root = document.documentElement;
const button = document.getElementById("theme-toggle");

function current() {
  return root.dataset.theme === "dark" ? "dark" : "light";
}

/** 按钮上的图标与提示跟着当前主题走（暗色显示月亮）。 */
function sync() {
  if (!button) return;
  const dark = current() === "dark";
  button.setAttribute("aria-pressed", String(dark));
  button.title = dark ? "当前是暗色，点一下切到明色" : "当前是明色，点一下切到暗色";
}

if (button) {
  sync();
  button.addEventListener("click", () => {
    root.dataset.theme = current() === "dark" ? "light" : "dark";
    try {
      localStorage.setItem(KEY, root.dataset.theme);
    } catch {
      /* 隐私模式下写不进去：这次切换照常生效，只是下次打开回到系统偏好 */
    }
    sync();
  });
}

/* ------------------------------------------------------------ 左栏折叠 */

const navToggle = document.getElementById("nav-toggle");

/** 折叠状态**只有一处**：`<html data-rail>`。
 *
 *  宽度、导航文字的显隐、箭头方向**全部**由 CSS 按这一个属性算出来
 *  （见 `app.css` 里 `html[data-rail="collapsed"]` 那几条）。JS 只改这个属性和
 *  无障碍属性，**不碰任何样式**——两处各改一半的话，迟早会出现"箭头反了但宽度没变"。
 *
 *  这里曾经多做了一件事：把状态再复制一份到 `.shell` 的 `data-rail` 上，
 *  而宽度规则当时认的正是那一份。代价是"首屏脚本设了 `<html>`，CSS 却不认"，
 *  于是每次整页加载都要等这个 module 跑起来才折叠——页面先按展开画一遍、
 *  再跳成折叠，就是那个"抽搐"。状态只留一处之后，这个时间差没有了。 */
function railCollapsed() {
  return root.dataset.rail === "collapsed";
}

function syncRail() {
  if (!navToggle) return;
  const collapsed = railCollapsed();
  // 箭头方向由 CSS 按 `<html data-rail>` 决定，这里**不碰图标**。
  // 但无障碍属性必须在 JS 里维护：它是状态，不是样式。
  navToggle.setAttribute("aria-expanded", String(!collapsed));
  navToggle.title = collapsed ? "展开左栏" : "折叠左栏";
}

if (navToggle) {
  syncRail();
  navToggle.addEventListener("click", () => {
    if (railCollapsed()) {
      delete root.dataset.rail;
    } else {
      root.dataset.rail = "collapsed";
    }
    try {
      localStorage.setItem(RAIL_KEY, railCollapsed() ? "collapsed" : "expanded");
    } catch {
      /* 同上：这次生效，下次回到默认的展开状态 */
    }
    syncRail();
  });
}

/* ------------------------------------------------------------ 环境徽标 */

// 顶栏那个"环境就绪"徽标：所有页面统一更新。
const envBadge = document.getElementById("env");
if (envBadge) {
  fetch("/api/health")
    .then((r) => (r.ok ? r.json() : null))
    .then((res) => {
      if (!res || !res.ok || !res.data) return;
      const data = res.data;
      envBadge.textContent = data.llm_ready ? data.model : "未配置密钥";
      envBadge.className = data.llm_ready ? "badge badge-ok" : "badge badge-warn";
      envBadge.title = `模型：${data.model || "默认"} · 数据库：${data.database} · 点一下打开设置`;
    })
    .catch(() => {
      envBadge.textContent = "环境检查失败";
      envBadge.className = "badge badge-warn";
    });
}


