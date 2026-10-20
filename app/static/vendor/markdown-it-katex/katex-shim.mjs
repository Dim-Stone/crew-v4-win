// 构建期垫片（只在打包 @vscode/markdown-it-katex 时使用，不参与运行时）。
//
// 插件内部有 `const katex_1 = __importDefault(require("katex"))`；
// 打包时用 --alias:katex=./katex-shim.mjs 把它指向本文件，避免产物里留下
// 浏览器解析不了的裸 `import "katex"`（我们只有 UMD 版 katex.min.js），
// 也避免把整个 KaTeX 再打包一份进去（白胖 270 KB，而且页面里会有两份）。
//
// 这里用 Proxy **懒取** globalThis.katex：页面上的 KaTeX 是 defer 脚本引入的，
// 模块求值那一刻可能还没到位，直接 export 会把 undefined 锁死。
//
// 实跑时 md.js 会显式传自己的实例（md.use(plugin, { katex: guardedKatex })），
// 所以这个垫片只是"没传时也不至于整块加载失败"的保险。

const shim = new Proxy(
  {},
  {
    get(_target, prop) {
      const real = globalThis.katex;
      if (!real) throw new Error("KaTeX 尚未加载");
      const value = real[prop];
      return typeof value === "function" ? value.bind(real) : value;
    },
  },
);

export default shim;
