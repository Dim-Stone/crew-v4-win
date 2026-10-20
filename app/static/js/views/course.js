// 课程页：左栏章节时间轴 + 右栏对话 + 贴右边缘的可折叠进度尺。
//
// 左栏刻意做成「时间轴」而不是普通目录树：节点三态（已讲实心 / 正在讲带环 / 未讲空心）
// 本身就在表达进度，所以页面上不需要再加一个进度条。**知识点切换也只走这里**。
//
// 进度尺（.rail）解决两件在长对话里会痛的事：
//   · 这一段讲到哪里了——刻度直接来自后端的教案进度；
//   · 前面那个知识点在哪——"讲到过"列表点一下就跳回去。
// 它默认折叠，只留一个按钮卡在右边缘上，不跟正文抢地方。
//
// 顶部那条协作轨与输入框上方的快捷追问，都是"把后端已经在做的事显出来"：
//   · 协作轨认的是 status 事件里的阶段文字（备课中 / 翻资料中 / 讲课中 / 画图中），
//     认不出来就不亮（映射的纯逻辑在 crew.js，有 node 用例守着）；
//   · 快捷追问里的句子都挑后端真会处理的说法（"没懂"会调节奏、"继续吧"才会翻页），
//     点了等于打字，不多一条捷径（见 chips.js）。

import { get, post, toast } from "../api.js";
import { buildIndexEntries } from "../rail.js";
import { CREW, agentForStatus } from "../crew.js";
import { buildChips } from "../chips.js";
import { subscribe } from "../sse.js";
import { escapeHtml, renderMarkdown } from "../md.js";
import { ensureKatex } from "../latex.js";

// 地址形如 /course/3
const courseId = Number(location.pathname.split("/").filter(Boolean).pop());

const timelineEl = document.getElementById("timeline");
const chatBox = document.getElementById("chat");
const scrollBox = document.getElementById("scroll");
const progressEl = document.getElementById("course-progress");
const adaptationEl = document.getElementById("adaptation");
const input = document.getElementById("message");
const sendButton = document.getElementById("send");
const crewEl = document.getElementById("crew");
const chipsEl = document.getElementById("chips");

const railEl = document.getElementById("rail");
// 进度尺的折叠按钮。名字里的 `progress` 是必需的——**不能**叫 `rail-toggle`：
// 顶栏那个"折叠左栏"已经占了这个 id，同一个页面里两个同名 id 会让
// `getElementById` 只返回先出现的那个，于是这个按钮会去折叠左栏。
const railToggle = document.getElementById("progress-toggle");
const railPointEl = document.getElementById("rail-point");
const railStepsEl = document.getElementById("rail-steps");
const railNoteEl = document.getElementById("rail-note");
const railIndexEl = document.getElementById("rail-index");
const railFilesEl = document.getElementById("rail-files");

let sessionId = null;
let session = null;
let streaming = false;

// 每个知识点"讲到第几步"（来自 /api/sessions/<id> 的 points_progress）。
//
// 为什么要它：进度尺里的"讲到过"只是一串知识点标题，往回翻时看不出
// 那个点当时讲到了哪儿——尤其在没有学生反馈、直接一路「继续讲」的时候。
// 键是知识点标题：那正是列表分组用的键（见 buildIndexEntries）。
let pointsProgress = new Map();

// 用户手动展开/折叠过的章节。重渲染时必须保留，否则每讲一轮左栏就弹回默认。
const expandedOverride = new Map();

// 这次打开页面之后存下的讲义（来自 saved 事件）。
// 只在内存里：后端没有"列出讲义"的接口，而这里要回答的也只是
// "刚才那几轮落到哪儿了"——所以宁可如实说"这次打开期间"。
const savedFiles = [];

/* ------------------------------------------------------- 协作轨（谁在动手） */

/** 静态渲染四个节点：模块里的 CREW 是唯一出处，模板里不再抄一份名字。 */
function renderCrew() {
  crewEl.innerHTML = "";
  for (const node of CREW) {
    const el = document.createElement("span");
    el.className = "crew-node";
    el.dataset.agent = node.agent;
    el.title = node.hint;
    el.textContent = node.name;
    crewEl.appendChild(el);
  }
}

/** 高亮此刻在动手的那个（传 null 表示没人——整条安静下来）。 */
function setCrewBusy(agent) {
  for (const node of crewEl.querySelectorAll(".crew-node")) {
    node.classList.toggle("is-busy", !!agent && node.dataset.agent === agent);
  }
}


/* ----------------------------------------------------------- Mermaid */

let mermaidPromise = null;

/** mermaid 跟着主题走：深色下用它自己的 dark 主题。
 *  不这么做，图纸会是白底紫框——在暗色页面上像贴了块白纸。 */
function mermaidTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "default";
}

function ensureMermaid() {
  if (mermaidPromise) return mermaidPromise;
  mermaidPromise = new Promise((resolve, reject) => {
    const sources = [
      "/static/vendor/mermaid.min.js",
      "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js",
    ];
    const tryLoad = (index) => {
      if (index >= sources.length) {
        reject(new Error("mermaid 未能加载（本地文件缺失且 CDN 不可达）"));
        return;
      }
      const script = document.createElement("script");
      script.src = sources[index];
      script.onload = () => {
        try {
          window.mermaid.initialize({ startOnLoad: false, theme: mermaidTheme() });
          resolve(window.mermaid);
        } catch (err) {
          reject(err);
        }
      };
      script.onerror = () => tryLoad(index + 1);
      document.head.appendChild(script);
    };
    tryLoad(0);
  });
  return mermaidPromise;
}

/** 切了主题之后：重新初始化 mermaid，并**把已经画出来的图按原源码重画一遍**。
 *
 *  mermaid 的主题在渲染那一刻就固定下来了，不重画的话，切到深色会留下一堆
 *  白底图纸。源码在 renderDiagram 里存进了 `data-code`，所以重画不需要再问模型。 */
document.getElementById("theme-toggle")?.addEventListener("click", () => {
  requestAnimationFrame(async () => {
    if (!window.mermaid) return;
    window.mermaid.initialize({ startOnLoad: false, theme: mermaidTheme() });
    for (const holder of document.querySelectorAll(".diagram[data-code]")) {
      await renderDiagram(holder, holder.dataset.code);
    }
  });
});

/* ----------------------------------------------------------- 图解放大 */

/** 点一下把图解放到全屏（设计稿里的"放大全屏平移"）。
 *
 *  复杂的图在气泡那点宽度里根本看不清。遮罩里直接复制图纸的标记——
 *  svg 本身就是标记，不必再让 mermaid 渲染一遍。
 *  平移交给滚动条，缩放交给浏览器（Ctrl+滚轮）：自造一套手势只会更难用。 */
function openDiagramFullscreen(holder) {
  const overlay = document.createElement("div");
  overlay.className = "diagram-modal";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-label", "放大的图解");
  overlay.title = "点击任意处或按 Esc 关闭";

  const body = document.createElement("div");
  body.className = "diagram-modal-body";
  body.innerHTML = holder.innerHTML;
  overlay.appendChild(body);

  function close() {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  }
  function onKey(event) {
    if (event.key === "Escape") close();
  }

  overlay.addEventListener("click", close);
  document.addEventListener("keydown", onKey);
  document.body.appendChild(overlay);
}

async function renderDiagram(holder, code) {
  // 记住源码：切换深浅主题时要照着它重画
  // （mermaid 的主题在渲染那一刻就固定了，不重画就会在深色页面里留一张白底图纸）
  holder.dataset.code = code;
  try {
    const mermaid = await ensureMermaid();
    const { svg } = await mermaid.render("d" + Math.random().toString(36).slice(2, 9), code.trim());
    holder.innerHTML = svg;
    holder.title = "点击放大";
    // 重绘（换主题）时 holder 还是同一个元素，别把监听叠成好几层
    if (!holder.dataset.zoomable) {
      holder.dataset.zoomable = "1";
      holder.addEventListener("click", () => openDiagramFullscreen(holder));
    }
  } catch (err) {
    holder.innerHTML = `
      <div class="diagram-fallback">
        <strong>图没画出来</strong>：${escapeHtml(err.message)}
        <p class="muted">下面是模型产出的 Mermaid 源码，可以复制到支持 Mermaid 的编辑器里看。</p>
        <pre class="code"><code>${escapeHtml(code)}</code></pre>
      </div>`;
  }
}

/** 给块级公式加一个"复制源码"按钮（设计稿里的 katex-showcase）。
 *
 *  原始 TeX 由 `md.js` 写进 `data-tex`——KaTeX 渲染完之后，DOM 里只剩下排版好的
 *  HTML，学生想拿那一串写法去别处用就没了。
 *  流式输出时正文每帧都会重建，所以这里每次都先查一遍有没有，别叠出两个按钮。 */
function enhanceFormulas(container) {
  for (const block of container.querySelectorAll(".katex-block[data-tex]")) {
    if (block.querySelector(".formula-copy")) continue;

    const button = document.createElement("button");
    button.type = "button";
    button.className = "formula-copy";
    button.textContent = "复制";
    button.title = "复制这段公式的 LaTeX 源码";
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(block.dataset.tex);
        button.textContent = "已复制";
        setTimeout(() => { button.textContent = "复制"; }, 1500);
      } catch {
        // 剪贴板可能被浏览器策略挡住（非安全上下文、用户拒绝授权）
        toast("复制没成功，可以手动选中公式源码", "warn");
      }
    });
    block.appendChild(button);
  }
}

/* ----------------------------------------------------------- 左栏 */

function renderSidebar() {
  const course = session.course;
  const currentPointId = session.current_point ? session.current_point.id : null;

  document.getElementById("course-title").textContent = course.title;

  const points = course.chapters.flatMap((chapter) => chapter.points);
  const done = points.filter((point) => point.status === "done").length;

  // 教案进度：把"备课到哪一步"也显示出来，而不是只藏在提示词里
  const plan = session.plan || {};
  const planText = plan.total_steps
    ? ` · 这一节第 ${Math.min(plan.current_step + 1, plan.total_steps)}/${plan.total_steps} 步`
    : "";

  progressEl.textContent = `${done} / ${points.length} 个知识点已讲${planText}`;

  timelineEl.innerHTML = "";

  course.chapters.forEach((chapter, chapterIndex) => {
    const hasCurrent = chapter.points.some((point) => point.id === currentPointId);
    const chapterDone = chapter.points.filter((point) => point.status === "done").length;

    const node = document.createElement("li");
    node.className = "chapter-node" + (hasCurrent ? " is-current" : "");

    const toggle = document.createElement("button");
    toggle.innerHTML =
      `${escapeHtml(chapter.title)} <span class="chapter-count">${chapterDone}/${chapter.points.length}</span>`;

    const list = document.createElement("ul");
    list.className = "point-list";

    chapter.points.forEach((point) => {
      const item = document.createElement("li");
      if (point.status === "done") item.classList.add("is-done");
      if (point.id === currentPointId) item.classList.add("is-current");

      const button = document.createElement("button");
      button.textContent = point.title;
      button.addEventListener("click", () => gotoPoint(point.id));
      item.appendChild(button);
      list.appendChild(item);
    });

    // 展开状态：优先用用户的手动记录，其次默认展开当前章节。
    // 重渲染时不能丢用户的选择，否则每讲一轮左栏都会弹回默认。
    const expanded = expandedOverride.has(chapter.id)
      ? expandedOverride.get(chapter.id)
      : hasCurrent || (!currentPointId && chapterIndex === 0);
    list.classList.toggle("hidden", !expanded);
    toggle.addEventListener("click", () => {
      const shouldExpand = list.classList.contains("hidden");
      expandedOverride.set(chapter.id, shouldExpand);
      list.classList.toggle("hidden", !shouldExpand);
    });

    node.appendChild(toggle);
    node.appendChild(list);
    timelineEl.appendChild(node);
  });
}

/* ----------------------------------------------------------- 右栏 */

function messageElement(message) {
  const el = document.createElement("div");
  el.className = "bubble " + (message.role === "user" ? "user" : "agent");
  // 谁说的就带谁的标识：气泡左边框与铭牌颜色都按它取（见 app.css 的身份色规则）
  if (message.agent) el.dataset.agent = message.agent;

  const meta = document.createElement("div");
  meta.className = "meta";
  const who = message.agent === "visual" ? "图解" : message.role === "user" ? "我" : "讲师";
  meta.innerHTML = `<span>${escapeHtml(who)}</span>`;
  for (const tag of message.tags || []) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = tag;
    meta.appendChild(badge);
  }

  const body = document.createElement("div");
  body.className = "body";

  if (message.agent === "visual") {
    const holder = document.createElement("div");
    holder.className = "diagram";
    body.appendChild(holder);
    renderDiagram(holder, message.content);
  } else {
    body.innerHTML = renderMarkdown(message.content);
    enhanceFormulas(body);
  }

  el.appendChild(meta);
  el.appendChild(body);
  return el;
}

/** 按知识点把消息分组：连续属于同一知识点的消息归为一组。 */
function groupByPoint(messages) {
  const groups = [];
  for (const message of messages) {
    const tag = (message.tags || []).find((t) => t.startsWith("知识点："));
    const title = tag ? tag.slice("知识点：".length) : "";
    const last = groups[groups.length - 1];
    if (last && last.title === title) last.messages.push(message);
    else groups.push({ title, messages: [message] });
  }
  return groups;
}

function renderMessages() {
  chatBox.innerHTML = "";
  // 只渲染最近 60 条，避免页面过长；按知识点分组
  for (const group of groupByPoint(session.messages.slice(-60))) {
    const wrap = document.createElement("div");
    wrap.className = "chat-group";

    if (group.title) {
      const bar = document.createElement("div");
      bar.className = "chat-section";
      bar.dataset.point = group.title; // 左栏用它把"当前知识点"对上段落
      // 每段一个**唯一序号**：同一个知识点会被切成好几段（讲 A → 跳去 B → 又回 A），
      // 光靠知识点名定位时 `querySelector` 只会命中第一段——于是列表里几个同名条目
      // 会一起跳到同一个地方。序号是唯一身份，跳转与高亮都认它。
      bar.dataset.sectionIndex = String(chatBox.querySelectorAll(".chat-section").length);
      bar.textContent = group.title;
      wrap.appendChild(bar);
    }

    for (const message of group.messages) wrap.appendChild(messageElement(message));
    chatBox.appendChild(wrap);
  }

  renderRail();
  scrollToBottom();
}

/* ------------------------------------------- 进度尺（贴右边缘的可折叠小窗） */

/** 刻度：这一节一共几步、走到第几步。
 *
 *  只信后端推来的 `plan`（total_steps / current_step / step_title / finished）。
 *  后端没有给每一步的名字，所以刻度就如实画成 1..M 的等分格——
 *  不为了好看去编造步骤标题。 */
function renderRailProgress() {
  const plan = session.plan || {};
  const total = Number(plan.total_steps || 0);
  const current = Number(plan.current_step || 0);

  railStepsEl.innerHTML = "";

  if (!total) {
    railNoteEl.textContent = "这一节还没备课";
    return;
  }

  for (let index = 0; index < total; index += 1) {
    const item = document.createElement("li");
    if (plan.finished || index < current) item.className = "is-done";
    else if (index === current) item.className = "is-current";
    item.innerHTML = `<span class="num">${index + 1}</span><i></i>`;
    railStepsEl.appendChild(item);
  }

  railNoteEl.textContent = plan.finished
    ? `${total} 步都讲完了`
    : `第 ${Math.min(current + 1, total)} / ${total} 步${plan.step_title ? ` · ${plan.step_title}` : ""}`;
}

/** "讲到过"：按对话里段落出现的顺序列出，点一条跳回**那一段**。
 *
 *  条目认的是段落的**序号**而不是知识点名——同一个知识点可能被切成好几段
 *  （讲 A → 跳去 B → 又回 A），用名字定位会让几个同名条目一起跳到第一段去。
 *  序号与标签由 `rail.js` 的 `buildIndexEntries` 算（那段是纯逻辑，有 node 测试守着）。 */
function renderRailIndex() {
  const sections = Array.from(chatBox.querySelectorAll(".chat-section"));
  const entries = buildIndexEntries(sections.map((section) => section.dataset.point || ""));

  railIndexEl.innerHTML = "";
  if (!entries.length) {
    railIndexEl.innerHTML = '<li class="rail-empty">还没有讲过的段落</li>';
    return;
  }

  for (const entry of entries) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    // 跳转认序号：`scrollToSection` 按 data-section-index 精确查找
    button.dataset.section = String(entry.sectionIndex);
    button.title = entry.order > 1
      ? `回到「${entry.title}」第 ${entry.order} 次`
      : `回到「${entry.title}」`;

    button.append(document.createTextNode(entry.title));
    // "讲到第 N/M 步"：只标第一次出现的那一段（同名知识点第二次出现时，
    // 进度标签会重复，反而看不清）
    const progress = entry.order === 1 ? pointsProgress.get(entry.title) : null;
    if (progress && progress.total_steps) {
      const stepTag = document.createElement("span");
      stepTag.className = "rail-step";
      stepTag.textContent = progress.finished
        ? "已讲完"
        : `第 ${Math.min(progress.current_step + 1, progress.total_steps)}/${progress.total_steps} 步`;
      button.append(stepTag);
    }
    if (entry.order > 1) {
      const orderTag = document.createElement("span");
      orderTag.className = "rail-order";
      orderTag.textContent = `第 ${entry.order} 次`;
      button.append(orderTag);
    }

    button.addEventListener("click", () => scrollToSection(button.dataset.section));
    item.appendChild(button);
    railIndexEl.appendChild(item);
  }

  updateRailIndex();
}

/** 跳到某一段——按**序号**找，不按知识点名。 */
function scrollToSection(index) {
  if (index === undefined || index === null || index === "") return;
  const target = chatBox.querySelector(
    `.chat-section[data-section-index="${CSS.escape(String(index))}"]`,
  );
  if (target) target.scrollIntoView({ block: "start" });
}

/** 滚动时把"视线所在的那一段"标出来（同样按序号对齐，不用名字）。 */
function updateRailIndex() {
  const items = Array.from(railIndexEl.querySelectorAll("button[data-section]"));
  if (!items.length) return;

  const containerTop = scrollBox.getBoundingClientRect().top;
  let activeIndex = 0;

  items.forEach((item, index) => {
    const section = chatBox.querySelector(
      `.chat-section[data-section-index="${CSS.escape(String(item.dataset.section))}"]`,
    );
    if (!section) return;
    // 段落顶端越过容器顶（留 8px 容差）就算已经进入这一段
    if (section.getBoundingClientRect().top - containerTop <= 8) activeIndex = index;
  });

  items.forEach((item, index) => item.classList.toggle("is-current", index === activeIndex));
}

function renderRail() {
  railPointEl.textContent = session.current_point
    ? session.current_point.title
    : "知识点已全部讲完";
  renderRailProgress();
  renderRailIndex();
  renderSavedFiles();
}

/* ------------------------------------------- 存下的讲义（进度尺第三块） */

/** 记一条 saved 事件。同一条讲义会被反复追加（同一个知识点讲几轮就存几次），
 *  路径相同只留一条——这里是"东西落在哪了"的索引，不是流水。 */
function noteSaved(data) {
  const path = data && data.path ? String(data.path) : "";
  if (!path) return;
  const kind = data.kind === "diagram" ? "diagram" : "lecture";
  if (!savedFiles.some((file) => file.path === path && file.kind === kind)) {
    savedFiles.push({ path, kind });
  }
  renderSavedFiles();
}

function renderSavedFiles() {
  if (!savedFiles.length) {
    railFilesEl.innerHTML = '<li class="rail-empty">这次打开还没有讲义落盘</li>';
    return;
  }

  railFilesEl.innerHTML = "";
  for (const file of savedFiles) {
    const item = document.createElement("li");
    const kind = document.createElement("span");
    kind.className = "rail-file-kind";
    kind.textContent = file.kind === "diagram" ? "图解" : "讲义";
    const path = document.createElement("span");
    // 路径是模型之外的本地路径，仍然照规矩转义：这里是 innerHTML，不是 textContent
    path.className = "rail-file-path";
    path.textContent = file.path;
    item.append(kind, path);
    railFilesEl.appendChild(item);
  }
}

/* ------------------------------------------- 快捷追问（输入框上方） */

/** 摆哪几个芯片由 chips.js 决定（纯逻辑，有 node 用例守着）。
 *  点一下等于把这句话打出来发出去——所以这里不绕开 sendMessage 另走一条路。 */
function renderChips() {
  if (!session) return;

  const chips = buildChips({
    hasPoint: !!session.current_point,
    lessonFinished: !!(session.plan && session.plan.finished),
  });

  chipsEl.innerHTML = "";

  for (const chip of chips) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chip";
    button.textContent = chip.text;
    if (chip.hint) button.title = chip.hint;
    // 正在讲的时候按不动：同一时刻只允许一轮对话
    button.disabled = streaming;
    button.addEventListener("click", () => {
      if (streaming) return;
      input.value = chip.text;
      sendMessage();
    });
    chipsEl.appendChild(button);
  }

  // 图解入口也摆在这一排（它原来在顶栏）：操作聚在输入区附近，
  // 鼠标不用在屏幕上下两头跑；而且它不受"有没有当前知识点"影响，永远可点。
  chipsEl.appendChild(diagramChip());
}

/** "画成图解"那一枚：与追问芯片同一排、同一副样子（见 app.css 的说明）。
 *  它触发的是一个动作，靠文案与 title 说清楚，不额外加强调色。 */
function diagramChip() {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "chip";
  button.textContent = "画成图解";
  button.title = "把刚才的讲解画成流程图（生成完可以点图放大）";
  button.disabled = streaming;
  button.addEventListener("click", () => {
    if (!streaming) requestDiagram(button);
  });
  return button;
}

/** 展开 / 收起。
 *
 *  展开时给正文**让出宽度**，而不是盖在上面：用小窗挡住正在读的那几行，
 *  比让它占点地方难受得多。 */
function setRailOpen(open) {
  railEl.classList.toggle("is-open", open);
  document.body.classList.toggle("rail-open", open);
  railToggle.setAttribute("aria-expanded", String(open));
  railToggle.title = open ? "收起进度" : "展开进度与定位";
}

railToggle.addEventListener("click", () => {
  setRailOpen(!railEl.classList.contains("is-open"));
});

scrollBox.addEventListener("scroll", updateRailIndex);

/** 左栏点了某个知识点后，把右栏滚到对应那一段（讲过的才有段落）。 */
function scrollToPointSection(title) {
  if (!title) return;
  const target = chatBox.querySelector(`[data-point="${CSS.escape(title)}"]`);
  if (target) {
    target.scrollIntoView({ block: "start" });
  } else {
    toast(`「${title}」还没讲过，发一句话就开始了`);
  }
}

function findPoint(pointId) {
  for (const chapter of session.course.chapters) {
    const point = chapter.points.find((p) => p.id === pointId);
    if (point) return { chapter, point };
  }
  return null;
}

/** 把后端推来的进度快照落到本地状态，并刷新左栏。 */
function applyProgress(progress) {
  session.plan = progress.plan || {};
  const statusById = new Map(progress.points.map((p) => [p.id, p.status]));
  for (const chapter of session.course.chapters) {
    for (const point of chapter.points) {
      if (statusById.has(point.id)) point.status = statusById.get(point.id);
    }
  }

  const found = findPoint(progress.current_point_id);
  session.current_chapter = found
    ? { id: found.chapter.id, title: found.chapter.title }
    : null;
  session.current_point = found
    ? { id: found.point.id, title: found.point.title, summary: found.point.summary }
    : null;

  // 左栏因此"活"起来：这一轮讲过的点会变色，当前点的高亮跟着走
  renderSidebar();
  renderRail();
  renderAdaptation(progress.adaptation || "");
  // 教案可能刚刚讲完（"继续吧"该出现了），芯片要跟着换
  renderChips();
}

/** "系统正在怎么适应你"。
 *
 *  空串就让它空着（CSS 的 :empty 会整条隐藏），不要写"暂无调整"——
 *  那只是噪音，还会给学生"系统什么都没做"的暗示。 */
function renderAdaptation(text) {
  adaptationEl.textContent = text || "";
}

function scrollToBottom() {
  scrollBox.scrollTop = scrollBox.scrollHeight;
}

/** 找到（或新建）当前知识点对应的分组，让新消息落进正确的那一段。 */
function targetGroup() {
  const title = session.current_point ? session.current_point.title : "";
  const last = chatBox.lastElementChild;
  const lastTitle = last ? (last.querySelector(".chat-section")?.dataset.point ?? "") : null;
  if (last && lastTitle === title) return last;

  const wrap = document.createElement("div");
  wrap.className = "chat-group";
  if (title) {
    const bar = document.createElement("div");
    bar.className = "chat-section";
    bar.dataset.point = title;
    // 与 renderMessages 同一套编号：此刻这一条还没进 DOM，所以"已有段落数"就是它的序号
    bar.dataset.sectionIndex = String(chatBox.querySelectorAll(".chat-section").length);
    bar.textContent = title;
    wrap.appendChild(bar);
  }
  chatBox.appendChild(wrap);
  renderRail(); // 出现了新段落，"讲到过"要跟着长
  return wrap;
}

function appendUserBubble(text) {
  const el = document.createElement("div");
  el.className = "bubble user";
  el.innerHTML = `<div class="body">${renderMarkdown(text)}</div>`;
  targetGroup().appendChild(el);
}

function appendAgentBubble(agent = "lecturer") {
  const el = document.createElement("div");
  el.className = "bubble agent";
  el.dataset.agent = agent;
  el.innerHTML = `
    <div class="meta"><span>${agent === "visual" ? "图解" : "讲师"}</span><span class="phase"></span></div>
    <div class="body"><span class="spinner"></span></div>`;
  targetGroup().appendChild(el);

  return {
    setPhase(text) { el.querySelector(".phase").textContent = text || ""; },
    setBody(html, cursor = false) {
      const body = el.querySelector(".body");
      body.classList.toggle("cursor", cursor);
      body.innerHTML = html;
      enhanceFormulas(body);
    },
    setError(text) {
      el.querySelector(".phase").textContent = "";
      el.querySelector(".body").innerHTML =
        `<div class="notice notice-error">${escapeHtml(text)}</div>`;
    },
    setDiagram(code) {
      const body = el.querySelector(".body");
      body.classList.remove("cursor");
      body.innerHTML = '<div class="diagram"></div>';
      renderDiagram(body.querySelector(".diagram"), code);
    },
    setNote(text) {
      const meta = el.querySelector(".meta");
      meta.querySelectorAll(".saved-note").forEach((node) => node.remove());
      const note = document.createElement("span");
      note.className = "badge badge-ok saved-note";
      note.textContent = text;
      meta.appendChild(note);
    },
    // 这一轮依据的资料出处（"依据：矩阵引论.pdf 第 2 页"）。
    // 落在气泡的 meta 行里，学生当场就能核对"讲的是不是我那份资料"——
    // 不必等刷新之后从消息标签里找。
    setSources(labels) {
      const meta = el.querySelector(".meta");
      meta.querySelectorAll(".source-note").forEach((node) => node.remove());
      for (const label of labels) {
        const note = document.createElement("span");
        note.className = "badge badge-accent source-note";
        note.textContent = `依据：${label}`;
        meta.appendChild(note);
      }
    },
    // 与 setNote 相反：这条**不自动消失**。
    // 它报告的是"这一轮的产物没落盘"——如果它像 toast 那样几秒就飘走，
    // 用户回头看讲义时会以为是自己记错了。所以留一条痕迹在这儿。
    setAlert(text) {
      el.querySelectorAll(".alert-note").forEach((node) => node.remove());
      const note = document.createElement("div");
      note.className = "notice notice-warn alert-note";
      note.textContent = text;
      el.querySelector(".body").before(note);
    },
  };
}

/* ----------------------------------------------------------- 交互 */

async function gotoPoint(pointId) {
  if (streaming) {
    toast("等这一轮讲完再切换知识点");
    return;
  }
  try {
    const result = await post(`/api/sessions/${sessionId}/goto-point`, { point_id: pointId });
    session.current_chapter = result.current_chapter;
    session.current_point = result.current_point;
    renderSidebar();
    renderRail();
    renderChips();
    // 滚到那个知识点对应的对话段落，而不是无脑跳到底部
    scrollToPointSection(result.current_point.title);
  } catch (err) {
    toast(err.message, "error");
  }
}

async function sendMessage() {
  if (streaming) return;
  const content = input.value.trim();
  if (!content) return;

  if (!session.current_point) {
    toast("本课知识点已讲完，可以点左栏回到某个知识点继续");
    return;
  }

  streaming = true;
  sendButton.disabled = true;
  input.value = "";
  renderChips(); // 芯片跟着禁用：这一轮没讲完，不该再接第二轮
  appendUserBubble(content);
  const bubble = appendAgentBubble();
  scrollToBottom();

  try {
    const { turn_id: turnId } = await post(`/api/sessions/${sessionId}/messages`, { content });
    let accumulated = "";
    let renderScheduled = false;

    subscribe(turnId, {
      onStatus: (data) => {
        bubble.setPhase(data.status || "");
        // 顶部协作轨："现在是谁在动手"——认不出来就不亮（见 crew.js）
        setCrewBusy(agentForStatus(data.status));
      },
      onSaved: (data) => {
        bubble.setNote(`讲义已存入 ${data.path}`);
        noteSaved(data);
      },
      onSources: (data) => bubble.setSources(data.labels || []),
      onProgress: (data) => { if (data.progress) applyProgress(data.progress); },
      onWarn: (data) => toast(data.message),
      // 很久没有新内容：在气泡上标一句，别让人以为是自己网断了。
      // 对话页不提供中止（课堂那边有），因为这里的失败由服务端看门狗兜底。
      onStall: (seconds) => bubble.setPhase(`已 ${seconds}s 没有新内容，可能卡住了`),
      onAlert: (data) => {
        bubble.setAlert(data.message);
        toast(data.message, "warn");
      },
      onDelta: (data) => {
        accumulated += data.text || "";
        // 每帧最多渲染一次。
        // 实测流式时 delta 可达每秒两百次，而每次都要把**全文**重新 Markdown
        // （还要让 KaTeX 把公式全部重排一遍）——不节流的话，绝大多数渲染在几毫秒后
        // 就被下一次覆盖了，纯属浪费，而且越到后面越慢。
        // 用 requestAnimationFrame 把同一帧里的增量合并成一次渲染。
        if (renderScheduled) return;
        renderScheduled = true;
        requestAnimationFrame(() => {
          renderScheduled = false;
          bubble.setBody(renderMarkdown(accumulated), true);
          scrollToBottom();
        });
      },
      onDone: (data) => {
        bubble.setBody(renderMarkdown(data.text || accumulated), false);
        bubble.setPhase("");
        streaming = false;
        sendButton.disabled = false;
        setCrewBusy(null);
        renderChips();
        input.focus();
        scrollToBottom();
        // 这一轮可能产生了新的一页：入口上的"第几页"要跟着走
        refreshClassLink();
      },
      onError: (data) => {
        bubble.setError(data.message || "这一轮没有成功");
        streaming = false;
        sendButton.disabled = false;
        setCrewBusy(null);
        renderChips();
      },
    });

    // 轮询兜底：**不能只依赖 SSE 的 done**。
    // 实际故障：课堂页停在"讲课中"，切到对话再回来就好了——说明那一轮其实成功了，
    // 只是页面没收到最后一个事件。对话页同样可能遇到（网络/代理缓冲/页面被挂起）。
    const poll = setInterval(async () => {
      if (!streaming) { clearInterval(poll); return; }
      try {
        const info = await get(`/api/turns/${turnId}`);
        if (info.status === "done") {
          clearInterval(poll);
          bubble.setBody(renderMarkdown(info.result || accumulated), false);
          bubble.setPhase("");
          streaming = false;
          sendButton.disabled = false;
          setCrewBusy(null);
          renderChips();
          refreshClassLink();
        } else if (info.status === "failed" || info.status === "cancelled") {
          clearInterval(poll);
          bubble.setError(info.error || "这一轮没有成功");
          streaming = false;
          sendButton.disabled = false;
          setCrewBusy(null);
          renderChips();
        }
      } catch {
        /* 查不到就下一轮再试 */
      }
    }, 5000);
  } catch (err) {
    bubble.setError(err.message);
    streaming = false;
    sendButton.disabled = false;
    setCrewBusy(null);
    renderChips();
  }
}

/** 图解是可选动作：点了才跑（后端 `/api/sessions/<id>/diagram`）。
 *
 *  入口现在只有输入框上方那一枚"画成图解"芯片（顶栏那个按钮 2026-09 合并掉了），
 *  所以这里直接收触发它的元素——跑的时候要把它禁掉，免得连点两次。 */
async function requestDiagram(trigger) {
  trigger.disabled = true;

  const bubble = appendAgentBubble("visual");
  bubble.setPhase("正在把刚才的讲解转成图");
  try {
    const { turn_id: turnId } = await post(`/api/sessions/${sessionId}/diagram`, {
      style_hint: "flowchart",
    });
    subscribe(turnId, {
      onStatus: (data) => {
        bubble.setPhase(data.status || "");
        setCrewBusy(agentForStatus(data.status));
      },
      onDiagram: (data) => bubble.setDiagram(data.code),
      onSaved: (data) => {
        bubble.setNote(`图解已存入 ${data.path}`);
        noteSaved(data);
      },
      onWarn: (data) => toast(data.message),
      // 很久没有新内容：在气泡上标一句，别让人以为是自己网断了。
      // 对话页不提供中止（课堂那边有），因为这里的失败由服务端看门狗兜底。
      onStall: (seconds) => bubble.setPhase(`已 ${seconds}s 没有新内容，可能卡住了`),
      onAlert: (data) => {
        bubble.setAlert(data.message);
        toast(data.message, "warn");
      },
      onDone: () => {
        bubble.setPhase("");
        trigger.disabled = false;
        setCrewBusy(null);
        scrollToBottom();
      },
      onError: (data) => {
        bubble.setError(data.message || "图解生成失败");
        trigger.disabled = false;
        setCrewBusy(null);
      },
    });
  } catch (err) {
    bubble.setError(err.message);
    trigger.disabled = false;
    setCrewBusy(null);
  }
}

// 章末测试：**点击时**再算地址。
// 原来是把 href 写在页面上、启动后再改——只要启动那一步取不到当前章，
// 链接就还是课程页本身，点下去"看起来打不开"。
document.getElementById("chapter-test")?.addEventListener("click", () => {
  const chapterId = (session && session.current_chapter && session.current_chapter.id)
    || (session && session.current_point && session.current_point.chapter_id);
  if (!chapterId) {
    toast("还没有进入任何章节：先在左栏选一节", "warn");
    return;
  }
  location.href = `/course/${courseId}/chapter/${chapterId}/test`;
});

sendButton.addEventListener("click", sendMessage);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
});

/**
 * 把"现在讲到哪一页"显示在进入课堂的入口上。
 *
 * 为什么需要：课堂与对话是两个页面，在课堂里讲了一轮、切回对话时，
 * 气泡流里看不出"刚才那一页"是第几页——用户的原话是"不清楚现在讲到哪里了"。
 * 所以对话页也要显示课堂的进度，并且点进去能**接着那一页**看（`?page=N`）。
 */
async function refreshClassLink() {
  const label = document.getElementById("class-progress");
  const link = document.getElementById("class-link");
  if (!label || !sessionId) return;

  // 先刷新"每个点讲到第几步"（它跟 deck 是两份数据，别因为一个失败拖累另一个）
  if (!streaming) {
    try {
      const fresh = await get(`/api/sessions/${sessionId}`);
      pointsProgress = new Map(
        (fresh.points_progress || []).map((item) => [item.title, item]),
      );
      renderRailIndex();
    } catch {
      /* 取不到就沿用旧的 */
    }
  }

  try {
    const data = await get(`/api/sessions/${sessionId}/deck`);
    const total = (data.pages || []).length;
    if (!total) {
      label.textContent = "进入课堂";
      return;
    }

    const at = (Number.isInteger(data.current_index) ? data.current_index : total - 1) + 1;
    label.textContent = `课堂 · 第 ${at}/${total} 页`;
    if (link) link.href = `/course/${courseId}/class?page=${at - 1}`;
  } catch {
    // 取不到就保持"进入课堂"，不该因为一个提示挡住页面
  }
}

/* ----------------------------------------------------------- 启动 */

async function boot() {
  if (!courseId) {
    toast("地址里没有课程编号", "error");
    return;
  }

  // 协作轨先立起来（四个节点是静态的），这样第一轮一开始就能亮
  renderCrew();

  // 首屏渲染前确保 KaTeX 就绪，否则公式会先以源码闪一下
  await ensureKatex().catch(() => {});

  try {
    const created = await post("/api/sessions", { course_id: courseId });
    sessionId = created.id;
    session = await get(`/api/sessions/${sessionId}`);
    // 先记下"每个点讲到第几步"，再渲染：进度尺的"讲到过"要用它标注
    pointsProgress = new Map(
      (session.points_progress || []).map((item) => [item.title, item]),
    );
    renderSidebar();
    renderMessages();
    renderChips();
    refreshAdaptation();
    // 入口上要显示"讲到第几页"：课堂与对话是两个页面，不显示就会迷路
    refreshClassLink();
  } catch (err) {
    toast(`课程打开失败：${err.message}`, "error");
  }
}

/** 首屏主动取一次"系统正在怎么适应你"。
 *
 *  授课回合的进度事件里也带这句话（每轮都会更新），但学生一刷新页面就没有事件了，
 *  所以首屏要自己取一次。取不到就静默留空——它只是提示，不该因此打断上课。
 */
async function refreshAdaptation() {
  try {
    const profile = await get("/api/profile");
    renderAdaptation(profile.adaptation || "");
  } catch {
    /* 提示取不到不影响上课 */
  }
}

boot();
