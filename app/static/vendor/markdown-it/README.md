# markdown-it（vendor）

- 版本：**15.0.2** — 文件是官方 `dist/browser/markdown-it.esm.min.mjs`（138 KB）
- 来源：<https://unpkg.com/markdown-it@15.0.2/dist/browser/markdown-it.esm.min.mjs>
- 许可：MIT（Copyright (c) 2014 Vitaly Puzrin, Alex Kocharin）

放本地而不走 CDN，理由与 katex / mermaid 相同：**前端要能离线工作**。
用 ESM 构建而不是 UMD，是因为页面本来就是 `import` 直接引（没有打包器）。

为什么选它、为什么不再自己解析 Markdown：见 `app/static/js/md.js` 顶部的说明。

**升级方式**：换掉这个文件，然后跑

```powershell
node tests/js/test_markdown.mjs
python -m pytest tests/test_frontend_js.py
```

（测试集中在 `tests/js/test_markdown.mjs`，它既守着公式与 XSS，也守着嵌套列表 / 引用 / 链接 / 图片这些结构行为。）
