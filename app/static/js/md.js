// Markdown 渲染 + LaTeX 公式。
//
// 分工：
//   · **结构**交给 markdown-it（vendor 的 ESM 构建，离线可用）：CommonMark + GFM 表格/删除线。
//     它的默认 `html: false` 正是我们要的安全默认——正文里的内联 HTML 一律转义成文本。
//   · **公式**交给 @vscode/markdown-it-katex（同样 vendor 在本地，已打包成 ESM，见
//     vendor/markdown-it-katex/README.md）：它识别 `$...$` / `$$...$$`，还会把
//     `\begin{align}` 这类环境自动按块级渲染。
//
//   插件不是直接调 KaTeX，而是调**我们传进去的那个实例**——所以下面那层 wrapper 仍然管事：
//     ① 文字组字符兼容修复（`·` / `≠` / `∑` / `∫`，见 latex.js）；
//     ② 渲染失败回退显示源码。插件自己在 catch 里会输出 katex-error 文本，
//        而我们的规矩是"绝不吞内容"——办法是让 renderToString **永不抛**。
//
// 为什么不再自己解析 Markdown：原先那套极简实现只覆盖标题/列表/表格/公式/代码块，
// 实测却发现嵌套列表被拍平成同级、引用/链接/图片/水平线/删除线统统不支持，
// 而这些都是讲义里真会出现的写法。自己往下补，等于长期维护一个 CommonMark 子集。
//
// （注：任务列表 `- [ ]` 需要额外插件，CommonMark 里它就是普通列表项；
//   `_斜体_` 在词内不生效是 CommonMark 的规定，不是缺陷。
//   插件的行为与 VS Code 一致，已知差异记在 tests/js/test_markdown.mjs 里。）

import MarkdownIt from "../vendor/markdown-it/markdown-it.esm.min.mjs";
import markdownItKatex from "../vendor/markdown-it-katex/markdown-it-katex.esm.js";

import { renderLatex } from "./latex.js";

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

/* ---------------------------------------------------------------- 公式 */

/** 渲染不出来时的兜底：把源码摆出来，而不是吞掉内容。 */
function fallbackFormula(tex, display, reason) {
  const delimited = display ? `$$${tex}$$` : `$${tex}$`;
  const title = reason ? ` title="公式未能渲染：${escapeHtml(reason)}"` : "";
  return `<code class="tex-fallback"${title}>${escapeHtml(delimited)}</code>`;
}

/**
 * 交给插件的 KaTeX。
 *
 * 插件会按公式里有没有 `\begin{align}` 之类来给 displayMode，这里照它给的走；
 * 其余一切（兼容修复、失败兜底）都由 latex.js 那层负责，**保证不抛异常**——
 * 一抛，插件的 catch 就会把内容换成它自己的错误文本。
 *
 * 补一手：插件判定 display 的正则是 `/\\begin\{(align|equation|gather|cd|alignat)\}/`，
 * **漏了带星号的形式**。而 `\begin{align*}` / `equation*` 这类在 KaTeX 里和它们的
 * 不带星版本一样，**只能**在 display 模式下渲染——不补，它就会白白掉进"回退显示源码"。
 * （`aligned` / `cases` / `array` 行内本就可用，不在此列，免得把行内公式撑成段级。）
 *
 * ⚠️ 升级 vendor 里的 markdown-it-katex 时**要回来对一眼**：若上游哪天把星号版补上了，
 * 这条就成了重复判定——那时删掉它即可（tests/js/test_katex_compat.mjs 里有一条
 * 走完整管线的 `\begin{align*}` 用例守着这个行为）。
 */
const DISPLAY_ONLY_ENV = /\\begin\{(?:align|alignat|equation|gather|cd)\*?\}/;

const guardedKatex = {
  renderToString(tex, options = {}) {
    const display = !!options.displayMode || DISPLAY_ONLY_ENV.test(tex);
    const result = renderLatex(tex, display);
    if (typeof result === "string" && result) return result;
    return fallbackFormula(tex, display, (result && result.reason) || "");
  },
};

/* ---------------------------------------------------------------- 装配 */

const md = new MarkdownIt({
  html: false,        // 内联 HTML 一律转义：内容是模型给的，当纯文本对待
  linkify: false,     // 不开自动链接：讲义里的 URL 保持原样，免得把怪串变成链接
  breaks: true,       // 段落内换行 → <br>：讲义是一行一行写的，粘成一行会很难读
  typographer: false,
});

md.use(markdownItKatex, { katex: guardedKatex });

// 代码块沿用页面既有的样式类（.bubble .body 下的 pre.code / code）
md.renderer.rules.fence = (tokens, idx) => {
  const token = tokens[idx];
  const info = token.info ? md.utils.escapeHtml(token.info.trim().split(/\s+/)[0]) : "";
  const attr = info ? ` data-lang="${info}"` : "";
  return `<pre class="code"${attr}><code>${md.utils.escapeHtml(token.content)}</code></pre>\n`;
};

/** 标题整体降两级：讲师写的 `# xxx` 不该抢页面自己的标题层级。 */
function shiftHeadings(tokens) {
  for (const token of tokens) {
    if (token.type !== "heading_open" && token.type !== "heading_close") continue;
    const level = Number(token.tag.slice(1));
    if (level >= 1 && level <= 6) token.tag = `h${Math.min(level + 2, 6)}`;
  }
}

md.core.ruler.push("shift_headings", (state) => {
  shiftHeadings(state.tokens);
  return true;
});

/* ---------------------------------------------------------------- 公式源码 */

/** 给块级公式的包裹元素带上**原始 TeX**（`data-tex`）。
 *
 *  为什么要它：KaTeX 渲染完之后，DOM 里只剩下排好版的 HTML，原始写法（学生
 *  想复制去别处用的那一串）就没了。界面上那个"复制"按钮靠这个属性工作
 *  （见 views/course.js 的 enhanceFormulas）。
 *
 *  插件把 `$$…$$` 与行内的 `\begin{align}` 都渲染成 `<p class="katex-block">…</p>`，
 *  所以两种 token 都要包一层；渲染失败的分支是 `class="katex-block katex-error"`
 *  （没有独立属性位），**故意匹配不到**——渲染失败的公式本来就没有可复制的源码。
 */
for (const ruleName of ["math_block", "math_inline_block"]) {
  const base = md.renderer.rules[ruleName];
  if (!base) continue;
  md.renderer.rules[ruleName] = (tokens, idx, options, env, self) => {
    const html = base(tokens, idx, options, env, self);
    const tex = md.utils.escapeHtml(String(tokens[idx].content ?? ""));
    return html.replace('<p class="katex-block"', `<p class="katex-block" data-tex="${tex}"`);
  };
}

/* ------------------------------------------------------- 表格里的公式 */

/**
 * 表格行里的数学公式**不能直接交给 markdown-it**：
 * 它按未转义的 `|` 切列，而绝对值 / 条件的写法里全是 `|`——
 * `$f(x)=\dfrac{|x|}{x}$` 会被切成三格，渲染成 `$f(x)=\dfrac{` + `x` + `}|\x)$`。
 *
 * 做法：把这一行里**含 `|` 的公式**整体换成占位符（`@@MATH0@@`，不含 `|`，
 * 表结构就不会被破坏），渲染完再把占位符换回 KaTeX 的 HTML。
 *
 * 为什么只处理"表格行 + 含 `|`"这两个条件：
 *   · 普通段落里的公式交给插件（行为不变，`data-tex` 等仍由插件负责）；
 *   · 表格里不含 `|` 的公式也不动——没必要为一个罕见问题改变通用路径。
 *
 * 为什么不能把 `|` 换成 `\vert`：`\begin{array}{c|c}` 这类列格式里的 `|`
 * 一换就坏，而且渲染出来的间距也会变。占位符法不动 TeX 源码，最稳。
 */
function protectTableMath(line, bag) {
  let out = "";
  let i = 0;
  while (i < line.length) {
    const ch = line[i];
    if (ch === "\\") {          // 转义序列原样跳过（`\$` 不是公式起点）
      out += line.slice(i, i + 2);
      i += 2;
      continue;
    }
    if (ch !== "$") {
      out += ch;
      i += 1;
      continue;
    }

    const delim = line.startsWith("$$", i) ? "$$" : "$";
    const from = i + delim.length;
    const to = line.indexOf(delim, from);
    if (to < 0) {               // 没闭合：按普通文本处理
      out += line.slice(i);
      break;
    }

    const body = line.slice(from, to);
    if (body.includes("|")) {
      const token = `@@MATH${bag.length}@@`;
      bag.push({ token, tex: body, display: delim === "$$" });
      out += token;
    } else {
      out += delim + body + delim;
    }
    i = to + delim.length;
  }
  return out;
}

/** 这一行是不是表格行（以 `|` 开头）。表头、表体、分隔行都满足。 */
function isTableRow(line) {
  return /^\s*\|/.test(line);
}

/**
 * 只渲染**行内**内容（不包 <p>）。
 *
 * 为什么需要：选项是 <span>A. …</span> 里的内容，而 
enderMarkdown 会包一层
 * <p>。<p> 出现在 <span> 里是非法嵌套，浏览器会把节点**拎出去**整理——
 * 于是选项文字与公式被拆开、位置错乱，看起来就是"公式渲染坏了"。
 */
export function renderInline(input) {
  const bag = [];
  const source = String(input ?? "")
    .split("\n")
    .map((line) => (isTableRow(line) ? protectTableMath(line, bag) : line))
    .join(" ");

  let html = md.renderInline(source);
  for (const item of bag) {
    if (!html.includes(item.token)) continue;
    const rendered = guardedKatex.renderToString(item.tex, { displayMode: item.display });
    const tex = escapeHtml(item.tex);
    html = html.split(item.token).join(
      `<span class="katex-inline" data-tex="${tex}">${rendered}</span>`
    );
  }
  return html;
}

export function renderMarkdown(input) {
  const bag = [];
  const source = String(input ?? "")
    .split("\n")
    .map((line) => (isTableRow(line) ? protectTableMath(line, bag) : line))
    .join("\n");

  let html = md.render(source);

  // 把占位符换回真正渲染好的公式。用 split/join 而不是正则：
  // 占位符是固定字符串，正则反而要转义一堆东西。
  for (const item of bag) {
    if (!html.includes(item.token)) continue;
    const rendered = guardedKatex.renderToString(item.tex, { displayMode: item.display });
    const tex = escapeHtml(item.tex);
    html = html.split(item.token).join(
      `<span class="katex-inline" data-tex="${tex}">${rendered}</span>`
    );
  }
  return html;
}
