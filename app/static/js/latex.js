// KaTeX 装载与公式渲染。
//
// 为什么选 KaTeX 而不是 MathJax：
//   授课是**逐字流式**输出的。KaTeX 是同步渲染，内容到了就能直接变成排版好的公式；
//   MathJax 是异步的，逐字新增时会反复重排闪烁，课堂体验很差。
//
// 装载策略与 mermaid 一致：本地 vendor 优先（离线可用），失败回退 CDN。

const LOCAL_JS = "/static/vendor/katex/katex.min.js";
const LOCAL_CSS = "/static/vendor/katex/katex.min.css";
const CDN_BASE = "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist";
const CDN_JS = `${CDN_BASE}/katex.min.js`;
const CDN_CSS = `${CDN_BASE}/katex.min.css`;

let loadingPromise = null;

/** 公式渲染器是否已经可用（同步判断，供渲染时使用）。 */
export function katexReady() {
  return typeof globalThis.katex?.renderToString === "function";
}

function injectCss() {
  if (document.querySelector('link[data-katex="1"]')) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = LOCAL_CSS;
  link.dataset.katex = "1";
  // 本地 CSS 不存在时换 CDN，否则公式会失去字体与排版
  link.onerror = () => {
    link.onerror = null;
    link.href = CDN_CSS;
  };
  document.head.appendChild(link);
}

/**
 * 确保 KaTeX 可用。本地文件通常已在 <head> 里 defer 载入，
 * 所以正常情况下这个 Promise 立即 resolve。
 */
export function ensureKatex() {
  if (katexReady()) return Promise.resolve(globalThis.katex);
  if (loadingPromise) return loadingPromise;

  injectCss();

  loadingPromise = new Promise((resolve, reject) => {
    const sources = [LOCAL_JS, CDN_JS];

    const tryLoad = (index) => {
      if (index >= sources.length) {
        reject(new Error("KaTeX 未能加载（本地文件缺失且 CDN 不可达）"));
        return;
      }
      const script = document.createElement("script");
      script.src = sources[index];
      script.onload = () => {
        if (katexReady()) resolve(globalThis.katex);
        else tryLoad(index + 1);
      };
      script.onerror = () => tryLoad(index + 1);
      document.head.appendChild(script);
    };

    tryLoad(0);
  });

  return loadingPromise;
}

/**
 * 把一段 LaTeX 渲染成 HTML。
 *
 * 关键行为：**渲染失败一律回退成原文**。
 * 流式输出时公式经常是半截的（只收到 $x^ 之类），
 * 这时绝不能把内容吃掉或显示成报错，宁可先原样展示、等下一帧再渲染。
 *
 * 失败时还会**修一次再重试**——理由见下面 repairTextGroups 的注释：
 * 有一类失败根本不是公式写错了，而是 KaTeX 自己把某个字符展开成了
 * text 模式里不存在的命令，修掉那一个字符就能正常渲染。
 */
export function renderLatex(tex, displayMode = false) {
  const source = String(tex ?? "").trim();
  if (!source) return null;

  if (katexReady()) {
    const render = (code) => globalThis.katex.renderToString(code, {
      displayMode,
      throwOnError: true, // 出错就抛，交给我们回退，而不是让 KaTeX 自己画成红色文本
      strict: false,
      trust: false,       // 禁止 \href 之类，避免把模型输出当可执行内容
      output: "html",
    });

    try {
      return render(source);
    } catch (err) {
      // 只在**内容真的被改动**时才重试：流式时公式半截也会失败，
      // 那种情况修不出东西来，重试一遍纯属浪费。
      const repaired = repairTextGroups(source);
      if (repaired !== source) {
        try {
          return render(repaired);
        } catch {
          /* 修不好就按原始错误回退 */
        }
      }
      return { failed: true, reason: err?.message || "语法错误" };
    }
  }

  return { failed: true, reason: "KaTeX 尚未加载" };
}

/* ---------------------------------------------------------------- 兼容修复
 *
 * 有一类"公式渲染失败"根本不是公式写错了，而是 KaTeX 自己展开出来的命令：
 *
 *   `\text{A·B}`（中文字面中点 U+00B7）
 *     → KaTeX parse error: Undefined control sequence: \cdotp at position 1: \cdotp
 *
 * 报错里那个 `\cdotp` 谁也搜不到——**源码里压根没有它**：
 * KaTeX 内部把 `·` 映射成命令 `\cdotp`（同形字符 ⋅ U+22C5 的数学命令名），
 * 而这个命令只有数学模式有。于是"文字里打个中点"就让整条公式渲染失败。
 * 实测在 vendor 的 0.16.11 上，text 组里同样炸掉的还有 ≠ / ∑ / ∫
 * （分别报 '\mathrel' / '∑' / '∫' can't be used in text mode）。
 *
 * 中文讲解里 `·` 太常见（"A·B""居里·夫人"），而公式里写 `\text{...}` 也很常见，
 * 两者一碰就中。所以这里做一次**范围极小**的修复：只动文字组里的这几个字符，
 * 数学模式与其它内容一律不碰（那边本来就没问题）。
 */
// 文字组：`\text{...}` 这一族。
// 命中的候选还要过一道 `isEscaped`——`\\text{...}` 是"换行命令 + text"，
// 从它中间那个反斜杠起匹配会把组认错。**不用正则 lookbehind**：
// 那样既与 isEscaped 的"连续反斜杠奇偶"原则不一致（`\\\text{}` 会漏判），
// 又把兼容性押在 ES2018 上（Safari < 16.4 会在解析期直接抛错，整个模块加载失败）。
const TEXT_COMMAND_RE = /\\(?:text|textrm|textbf|textit|textsf|texttt|mbox)\s*\{/;

// 每个字符给出"在 text 模式下能渲染"的等价写法。
// `·` 直接换成同形的 ⋅（两种模式都认），其余借文字组里内嵌数学的写法（KaTeX 支持 `\text{a$\neq$b}`）。
const TEXT_MODE_FIXES = [
  ["\u00b7", "\u22c5"],        // ·  →  ⋅
  ["\u2260", "$\\neq$"],       // ≠  →  $ \neq $
  ["\u2211", "$\\sum$"],       // ∑  →  $ \sum $
  ["\u222b", "$\\int$"],       // ∫  →  $ \int $
];

/** 组内**非数学**部分：把 KaTeX 在 text 模式下不认的字符换成等价写法。 */
function replaceTextChars(text) {
  let out = text;
  for (const [from, to] of TEXT_MODE_FIXES) {
    if (out.includes(from)) out = out.split(from).join(to);
  }
  return out;
}

/** 修一个文字组的内容。
 *
 *  **跳过组里嵌套的 `$...$` 数学段**：KaTeX 支持 `\text{a$\neq$b}` 这种写法，
 *  那段内容本来就是数学模式，`·` 之类在这里没问题——
 *  若一并替换，`≠` 会变成 `$\neq$`，凭空多出一对 `$`，整个组的分组就错乱了。
 *  做法是按 `$` 切开（带捕获组，数学段会留在结果里），只处理偶数索引的部分。
 *
 *  （这条跳过逻辑由 tests/js/test_katex_compat.mjs 里那条"嵌套数学段不被动"护着，
 *  并且验证过：把它去掉、那条用例就会红——不是白写的守卫。）
 */
function fixTextGroup(inner) {
  return inner
    .split(/(\$[^$]*\$)/g)
    .map((part, index) => (index % 2 === 1 ? part : replaceTextChars(part)))
    .join("");
}

/** 位置 `index` 上的字符是否被反斜杠转义。
 *
 *  必须看**连续**反斜杠的奇偶，不能只看前一个字符：
 *  `\{` 是转义（不算组），而 `\\{` 是"字面反斜杠 + 真的组开始"（要算组）。
 *  只看前一个字符的话，`\\{` 会被误判成转义，深度就数偏了。
 */
function isEscaped(text, index) {
  let backslashes = 0;
  for (let i = index - 1; i >= 0 && text[i] === "\\"; i -= 1) backslashes += 1;
  return backslashes % 2 === 1;
}

/** 从 `from` 起找下一个**没被转义**的 `\text{` 系命令；找不到返回 null。
 *
 *  被转义的候选（`\\text{`）要跳过再往后面找，而不是就地放弃——
 *  否则 `\\text{x}` 后面的真正文字组会被整段漏掉。
 */
function findTextCommand(tex, from) {
  let cursor = from;
  while (cursor < tex.length) {
    const match = tex.slice(cursor).match(TEXT_COMMAND_RE);
    if (!match) return null;
    const at = cursor + match.index;
    if (!isEscaped(tex, at)) return { at, text: match[0] };
    cursor = at + 1;   // 这个候选是转义的，往后挪一格继续找
  }
  return null;
}

/** 逐个文字组做修复；组外（含数学模式）一律原样保留。 */
function repairTextGroups(tex) {
  let out = "";
  let index = 0;

  while (index < tex.length) {
    const match = findTextCommand(tex, index);
    if (!match) {
      out += tex.slice(index);
      break;
    }

    const commandAt = match.at;
    out += tex.slice(index, commandAt);

    // 找到与命令自带的 `{` 配对的那个 `}`。花括号要计数：组里可能还有 `\frac{a}{b}`。
    const braceAt = commandAt + match.text.length - 1;
    let depth = 0;
    let cursor = braceAt;
    for (; cursor < tex.length; cursor += 1) {
      const ch = tex[cursor];
      if (ch === "{" && !isEscaped(tex, cursor)) depth += 1;
      else if (ch === "}" && !isEscaped(tex, cursor)) {
        depth -= 1;
        if (depth === 0) break;
      }
    }

    // 半截的 `\text{`（流式时常见）：找不到配对就整个原样留着，别去猜
    if (cursor >= tex.length) {
      out += tex.slice(commandAt);
      break;
    }

    out += match.text + fixTextGroup(tex.slice(braceAt + 1, cursor)) + "}";
    index = cursor + 1;
  }

  return out;
}
