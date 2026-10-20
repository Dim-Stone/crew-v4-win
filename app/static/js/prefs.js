// 界面偏好：把服务端存的那几项变成 <html> 上的 CSS 变量。
//
// 为什么走"CSS 变量"而不是让每个视图自己去改样式：
//     字号、行宽、行高这三项影响整站，包括对话气泡里的讲义、公式、代码块。
//     让每个页面自己处理，一定会漏（而且漏的那一处只有"读完一整篇讲义"才会发现）。
//     写成变量之后，`app.css` 里所有引用它的地方自动跟着变。
//
// 为什么主题**不在这里**：
//     主题必须在首屏之前定下来，否则会先闪一下另一种底色——那段逻辑在
//     `base.html` 的内联脚本里（module 脚本是延迟执行的，来不及）。
//     所以主题留在 localStorage，由 theme.js 管；这里只管不影响首屏底色的项。
//
// 这个模块**不做任何报错提示**：偏好读不到就整套用默认值，界面照常能用。
// 字号没生效是小事，为此弹一个红条反而更烦人。

const root = document.documentElement;

/** 一份偏好，字段与 app/preferences.py 的 DEFAULTS 一致。 */
let current = null;

/**
 * 把一份偏好写到 CSS 变量上。
 *
 * 值会做一次温和的兜底（`Number(x) || fallback`）：接口坏了、返回了 null，
 * 都不该让字号变成 `NaNpx`——那会让整个页面的字变成浏览器默认大小，
 * 看起来像"样式全丢了"。
 */
export function apply(values) {
  current = values || {};

  const font = Number(current.font_size) || 15;
  const line = Number(current.line_height) || 1.75;
  const width = Number(current.content_width) || 920;

  root.style.setProperty("--reading-font", `${font}px`);
  root.style.setProperty("--reading-line", String(line));
  root.style.setProperty("--page-max", `${width}px`);
  root.style.setProperty("--chat-max", `${width}px`);

  // 密度只是一个倍数：紧凑模式下间距乘 0.8。
  // 用倍数而不是"给每个间距写两套值"，是因为后者改一处漏一处。
  root.style.setProperty("--density", current.density === "compact" ? "0.8" : "1");

  syncMotion();
}

/**
 * 动效开关：系统的偏好与用户在这一页里的选择是**两个独立的输入**。
 *
 *   · 系统说"减少动效" → 一律关掉（尊重系统设置，这是无障碍要求）
 *   · 用户在本应用里选"一直关掉" → 也关掉
 *   · 用户选"跟随系统" → 由上一行决定
 *
 * 结果写成一个属性 `data-motion`，由 CSS 负责真的压掉动画。
 */
function syncMotion() {
  let systemReduced = false;
  try {
    systemReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    /* 老内核没有 matchMedia：当作没开启 */
  }
  const off = systemReduced || current.reduce_motion === true;
  if (off) {
    root.dataset.motion = "off";
  } else {
    delete root.dataset.motion;
  }
}

/** 当前生效的这份（只读，给别的模块参考用）。 */
export function values() {
  return current;
}

/**
 * 从服务端重新读一次并应用。设置页保存之后调它。
 *
 * 返回值给调用方判断"读到了没有"——但**不抛异常**：设置页要用失败时的
 * 沉默来提示"这次没读到"，而不是弹一个用户看不懂的英文错误。
 */
export async function reload() {
  try {
    const response = await fetch("/api/preferences", { cache: "no-store" });
    if (!response.ok) return false;
    const payload = await response.json();
    if (!payload || payload.ok === false || !payload.data) return false;
    apply(payload.data.values);
    return true;
  } catch {
    return false;
  }
}

// 每个页面都会加载这个模块（base.html 里引了它），所以自己起步一次。
// 不 await：首屏不该为了字号而多等一个来回；字号到位之前那一小会儿，
// 浏览器画的是 CSS 里的默认值——而默认值与"没设置过"完全一致。
reload();
