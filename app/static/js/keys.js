// 全局快捷键：让这个窗口更像客户端，而不是"一堆网页"。
//
// 客户端和网页在这一点上差别很直接：网页里的东西要用鼠标去**找**，
// 而客户端的动作用键盘就能**到**。这一步不需要新依赖，是"客户端感"里最便宜的一步。
//
// **这里只放跨页面通用的键**（去设置、切页面、发送）。页面自己的键留在各自的模块：
// 课程页输入框的 `Enter = 发送`、图解全屏的 `Esc = 关闭`（见 `views/course.js`）
// ——那些是那个页面的局部语义，搬到这里会让"这个键到底谁在处理"变得说不清。

/**
 * 数字键去哪个页面，顺序就是编号（`1` = 课程）。
 *
 * **不全是左栏那一列**：左栏只有"课程 / 讲义 / 设置"（创建课程的入口在工作台那个
 * 大按钮上，不再重复放一个导航项），而这里给"新建课程"留了 `2`——它是高频动作，
 * 键盘直达比"先回工作台再点按钮"快。
 *
 * 是**数据**而不是散在各处的 `if`：加一个目的地只改这张表。
 */
export const DESTINATIONS = [
  { key: "1", href: "/", label: "课程" },
  { key: "2", href: "/new", label: "新建课程" },
  { key: "3", href: "/notes", label: "讲义" },
  { key: "4", href: "/settings", label: "设置" },
];

/**
 * 这个按键事件该做什么？返回 `null` 表示"不归我管"。
 *
 * 抽成**纯函数**是为了能测：`tests/js/test_keys.mjs` 直接喂一个只有几个字段的
 * 对象就行，不需要浏览器（`md.js` / `rail.js` 的断言也是这么做的）。
 *
 * 返回值只有两种形状：
 *     `{ kind: "go", href, why }`   —— 跳到一个页面
 *     `{ kind: "send" }`            —— 触发当前页面的主操作（发送）
 */
export function route(event) {
  const accel = event.ctrlKey || event.metaKey;   // metaKey 让 macOS 上也顺手

  // `Ctrl+,` 打开设置：这是应用界的通行约定（VS Code、浏览器、各种编辑器）。
  if (accel && event.key === ",") {
    return { kind: "go", href: "/settings", why: "Ctrl+," };
  }

  // `Ctrl+Enter` 发送：让"焦点不在输入框里"时也能把话发出去。
  //
  // 注意它**不会**和输入框自己的 `Enter = 发送` 撞车：那条会在输入框里
  // preventDefault，于是这里开头那句 `defaultPrevented` 判断直接放行。
  // 反过来（焦点在页面上别处）才是这条生效的地方。
  if (accel && event.key === "Enter") {
    return { kind: "send", why: "Ctrl+Enter" };
  }

  // `Ctrl+数字` / `Alt+数字` 切页面。
  //
  // 为什么**两个修饰键都收**：`Ctrl+数字` 在**浏览器**里是"切换标签页"，
  // 那是浏览器级的键，页面拦不住（`preventDefault` 无效）。桌面窗口走的是
  // WebView2，没有标签页，所以 `Ctrl+数字` 能到页面；而在浏览器里调试
  // （`run_web.py`）时用 `Alt+数字` —— 那个组合在浏览器里没有默认行为。
  if (event.ctrlKey || event.altKey) {
    // Shift / Cmd 的组合留给别人（比如将来的多选、或者系统的快捷键）
    if (event.shiftKey || event.metaKey) return null;
    const hit = DESTINATIONS.find((dest) => dest.key === event.key);
    if (hit) {
      return { kind: "go", href: hit.href, why: `${event.ctrlKey ? "Ctrl" : "Alt"}+${hit.key}` };
    }
  }

  return null;
}

/** 执行一个路由结果。 */
function run(hit) {
  if (hit.kind === "go") {
    // 已经在那一页就不动：重载一次没有意义，还会把页面上的临时状态（比如
    // 刚生成到一半的图解）弄丢。
    if (location.pathname !== hit.href) location.href = hit.href;
    return;
  }

  // `#send` 是课程页那个发送按钮（`course.html`）。找不到就什么都不做——
  // 别的页面没有"主操作"，按了不该有反应，更不该报错。
  const send = document.getElementById("send");
  if (send && !send.disabled) send.click();
}

// **只在浏览器里挂监听器**。
//
// 这一句判断不是为了跨平台，而是为了这个模块**能被测**：上面那些路由逻辑要用
// Node 断言（`tests/js/test_keys.mjs`），而 Node 里没有 `addEventListener`——
// 顶层直接调用会让 `import` 本身就抛异常，断言根本跑不到。
if (typeof window !== "undefined") {
  window.addEventListener("keydown", (event) => {
    // 页面自己已经处理过了。**这一行不能删**：输入框里的 `Enter` 会
    // preventDefault 并发送，而事件仍会冒泡到这里——不挡住的话
    // `Ctrl+Enter` 在输入框里会发两遍。
    if (event.defaultPrevented) return;

    const hit = route(event);
    if (!hit) return;

    event.preventDefault();
    run(hit);
  });
}
