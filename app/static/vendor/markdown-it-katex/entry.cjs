// 打包入口（构建输入，不参与运行时）。
//
// 为什么不能直接打包上游的 dist/index.js：那是 CommonJS，
// esbuild 会把它的 `module.exports` 整个当作 default 导出，
// 于是产物 `export default` 出来的是"含 default 属性的对象"，而不是插件函数本身，
// `md.use(plugin)` 会当场报 `e.apply is not a function`。
//
// 显式取出 `.default` 之后，产物的默认导出才是那个插件函数。

module.exports = require("@vscode/markdown-it-katex").default;
