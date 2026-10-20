# @vscode/markdown-it-katex（vendor，已打包成 ESM）

- 上游：**`@vscode/markdown-it-katex` 1.1.2**（Microsoft，MIT）——VS Code 的 Markdown 数学预览用的就是它
- 上游分发的是 **CommonJS**（`dist/index.js`，17.5 KB），而本前端是裸 ESM（没有打包器），
  浏览器不能直接 `import` CJS。所以这里放的是用它打包出来的 ESM 单文件：

| 文件 | 用途 |
|---|---|
| `markdown-it-katex.esm.js` | 产物（7 KB，minified）。`md.js` 直接 import 的就是它 |
| `katex-shim.mjs` | **构建输入**，不参与运行时。把插件内部的 `require("katex")` 换掉，见下 |
| `entry.cjs` | **构建输入**，不参与运行时。打包入口，见下 |
| `README.md` | 本文件：版本、来源、重新生成的命令 |

## 两个构建输入为什么必需

**① `katex-shim.mjs`** —— 插件源码里有一行 `const katex_1 = __importDefault(require("katex"))`。
如果就这么打包，产物里会留下一个浏览器解析不了的裸 `import "katex"`（我们只有 UMD 版 `katex.min.js`）；
如果把 KaTeX 一起打包进来，产物会白胖 270 KB，而且页面里就有两份 KaTeX 了。

所以打包时用它顶替：垫片把调用**转发到页面上的 `globalThis.katex`**（`course.html` 里那份）。
实际使用时 `md.js` 还会显式传入自己的实例（`md.use(plugin, { katex: guardedKatex })`），
垫片只是保证"即便没传也不会因为裸 import 而整块加载失败"。用 Proxy 懒取，是为了避开
defer 顺序导致模块求值那一刻 `globalThis.katex` 还不存在的情况。

**② `entry.cjs`** —— 上游是 CommonJS，直接拿 `dist/index.js` 当入口，产物会是
`export default module.exports`，也就是"含 `default` 属性的对象"，`md.use()` 当场报
`e.apply is not a function`。入口里显式取 `.default`，产物的默认导出才是插件函数本身。

## 重新生成（升级上游时做一次）

```powershell
$dir = Join-Path $env:TEMP 'mkatex-build'
New-Item -ItemType Directory -Force $dir | Out-Null
Push-Location $dir
npm init -y
npm install --no-audit --no-fund esbuild "@vscode/markdown-it-katex@1.1.2"

# 两个构建输入拷到临时工程（esbuild 的 --alias 相对当前工作目录解析）
Copy-Item "<仓库>\app\static\vendor\markdown-it-katex\katex-shim.mjs" $dir
Copy-Item "<仓库>\app\static\vendor\markdown-it-katex\entry.cjs" $dir

npx esbuild entry.cjs --bundle --format=esm --minify `
  --alias:katex=./katex-shim.mjs `
  --banner:js='/** @vscode/markdown-it-katex v1.1.2 (MIT, Microsoft Corp.) - esbuild bundle. KaTeX is taken from globalThis.katex on the page. Build command: see README.md in this folder. */' `
  --outfile="<仓库>\app\static\vendor\markdown-it-katex\markdown-it-katex.esm.js"
Pop-Location
```

打包**只在升级时**跑一次：仓库里不需要 `node_modules`，临时工程留在系统 Temp 即可。

升级后跑：

```powershell
python -m pytest tests/test_frontend_js.py tests/test_pages.py -q
```

（后者会验"产物能通过静态路径拿到、且导出形态是 ESM"。）

## 它与"自己写 80 行规则"的差别（选它的理由与代价）

- 得到：对齐 VS Code 的行为，包括**自动把 `\begin{align}` 等环境当块级公式**渲染（自己写的规则没有这一条）；
  上游维护、将来多支持一种写法时跟着升级即可。
- 代价 1：块级公式外面会多一层 `<p class="katex-block">`（VS Code 也是这样），`app.css` 里已经收掉多余边距。
- 代价 2：块级规则在**缺收尾 `$$`** 时会一直吃到文末。对逐字流式反而有利——公式先渲染出来，
  下一帧收尾的 `$$` 到了就补全；测试里钉住的是"内容一个字都没丢"。
- 实测澄清一条：插件的行内规则虽排在 `backticks` 之前，但规则只在**当前位置**是 `$` 时才触发，
  所以 `` `$x$` `` 会被 backticks 先吃掉、**不会**被当成公式（`tests/js/test_markdown.mjs` 里有断言）。

> 失败兜底不靠插件：插件自己在 catch 里输出 `katex-error` 文本，而我们的规矩是"回退显示源码、
> 绝不吞内容"。做法是让传进去的 `renderToString` **永不抛**（见 `md.js` 的 `guardedKatex`）。
