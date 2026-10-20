// 课堂页：一页一卡 + 一步一反馈。
//
// 这是课程下的**第二级页面**（/course/<id>/class），不是课程页里的一个视图。
// 理由见 `app/pages.py` 的 `classroom()`：课程页是两列栅格，课堂要的是整块屏幕，
// 把左栏藏起来并不会收回那一列 —— 第一版就是这样把卡片压成 296px 宽的。
//
// ## 这一页的节奏（用户定的）
//
//     点「继续讲」→ 讲师讲**一步**（可能被切成几页）→ 翻到这一步的最后一页
//     → 自动弹出反馈框 → 学生说一句 → 讲师接着讲下一步
//     → 所有步骤讲完后进入小测（P1，尚未实现）
//
// "一步"的判定刻意用**生成批次**（一次 /pages 或一次反馈产生的那些页），
// 而不是让前端去猜教案里的步骤——教案的 `current_step` 由复盘异步推进，
// 前端拿它做即时判断会慢半拍。

import { get, post, toast } from "../api.js";
import { subscribe } from "../sse.js";
import { escapeHtml, renderInline, renderMarkdown } from "../md.js";
import { stripOptionLabel } from "../quiz_ui.js";
import { ensureKatex } from "../latex.js";

// 地址形如 /course/3/class
const courseId = Number(location.pathname.split("/").filter(Boolean)[1]);

const cardEl = document.getElementById("cl-card");
const pointEl = document.getElementById("cl-point");
const posEl = document.getElementById("cl-pos");
const dotsEl = document.getElementById("cl-dots");
const hintEl = document.getElementById("cl-hint");
const stallEl = document.getElementById("cl-stall");
const prevBtn = document.getElementById("cl-prev");
const nextBtn = document.getElementById("cl-next");
const skipBtn = document.getElementById("cl-skip");
const genBtn = document.getElementById("cl-gen");
const fullBtn = document.getElementById("cl-full");
const askEl = document.getElementById("cl-ask");
const askSubEl = document.getElementById("cl-ask-sub");
const inputEl = document.getElementById("cl-input");
const sendBtn = document.getElementById("cl-send");
const askHintEl = document.getElementById("cl-ask-hint");

let sessionId = null;
let pages = [];
let index = -1;
// 最近一次生成产出的最后一页：翻到它就意味着"这一步讲完了"
let stepEnd = -1;
let busy = false;
// 当前这一轮的订阅句柄：停滞时可以主动中止
let activeTurn = null;
let plan = { total_steps: 0, current_step: 0, finished: false, stage: "lecturing" };
let point = { title: "", point_index: 0, total_points: 0 };
// 这一轮生成完了、还没收过反馈
let askPending = false;

/* ------------------------------------------------------------------ 数据 */

async function loadDeck({ jumpLast = false } = {}) {
  const data = await get(`/api/sessions/${sessionId}/deck`);
  pages = data.pages || [];
  plan = data.plan || plan;
  point = data.point || point;

  if (!pages.length) {
    index = -1;
  } else if (jumpLast) {
    index = pages.length - 1;
  } else {
    index = Math.min(Math.max(index, 0), pages.length - 1);
  }
  render();
}

/* ------------------------------------------------------------------ 渲染 */

function currentPage() {
  return pages[index] || null;
}

function renderDots() {
  dotsEl.innerHTML = "";
  if (!pages.length) return;
  const fragment = document.createDocumentFragment();
  pages.forEach((page, position) => {
    const dot = document.createElement("button");
    dot.type = "button";
    dot.className = "slide-dot";
    if (position === index) dot.classList.add("active");
    if (page.status === "skipped") dot.classList.add("skipped");
    dot.title = page.title || `第 ${position + 1} 页`;
    dot.addEventListener("click", () => {
      if (hintEl) hintEl.hidden = true;
      index = position;
      render();
    });
    fragment.appendChild(dot);
  });
  dotsEl.appendChild(fragment);
}

function render() {
  const page = currentPage();
  const total = pages.length;

  pointEl.textContent = point.title
    ? `知识点 ${point.point_index}/${point.total_points} · ${point.title}`
    : "还没有进入知识点";
  posEl.textContent = page ? `第 ${index + 1}/${total} 页` : "—";

  if (!page) {
    cardEl.innerHTML = `
      <div class="slide-empty">
        <p>这一节还没有内容。</p>
        <p class="slide-empty-hint">点下面的「开始上课」，讲师会先讲第一步。</p>
      </div>`;
    prevBtn.disabled = true;
    nextBtn.disabled = true;
    skipBtn.disabled = true;
    genBtn.disabled = busy;
    genBtn.textContent = "开始上课";
    renderDots();
    return;
  }

  genBtn.textContent = "继续讲";
  // **不再单独渲染 `page.title`。**
  //
  // 页标题是从正文里那个 Markdown 标题抽出来的（`page_builder._title_of`），
  // 所以单独再画一个大标题会得到两个后果，截图里都出现了：
  //   ① 标题走的是 `escapeHtml()`，`$f(2x-1)$` 原样显示（公式没渲染）；
  //   ② 同一句话在页首出现两次（大标题 + 正文里那个标题）。
  // 正解是让正文自己的标题来当标题：Markdown 渲染器本来就会处理它里面的公式，
  // 字号交给下面的 CSS（`.slide .body h1/h2`）。`page.title` 只留给页点的提示文字。
  const labels = page.materials || [];
  const badges = labels.length
    ? `<div class="slide-materials">${labels
        .map((label) => `<span class="badge badge-accent">依据：${escapeHtml(label)}</span>`)
        .join("")}</div>`
    : "";

  cardEl.classList.toggle("is-skipped", page.status === "skipped");
  cardEl.innerHTML = `<div class="body">${renderMarkdown(page.content || "")}</div>${badges}`;

  prevBtn.disabled = index <= 0 || busy;
  nextBtn.disabled = busy;
  skipBtn.disabled = page.status === "skipped" || busy;
  genBtn.disabled = busy;
  renderDots();
  maybeAsk();
}

/** 到这一步的最后一页时，自动弹出反馈框。 */
function maybeAsk() {
  const page = currentPage();
  const atStepEnd = page && index === stepEnd;
  const finished = plan && plan.finished;

  // 这一步讲完、而且整个知识点也讲完了 → 该测验了。
  // 只在"刚讲完一轮"之后自动摆出来（`askPending`），刷新页面不会平白弹窗。
  if (finished && atStepEnd && askPending && !busy) {
    askEl.hidden = true;
    if (!quizPrompted) {
      quizPrompted = true;
      openQuiz();
    }
    return;
  }

  if (!askPending || !atStepEnd || busy || finished) {
    askEl.hidden = true;
    return;
  }
  askEl.hidden = false;
  askSubEl.textContent = plan && plan.total_steps
    ? `这一步讲完了（第 ${(plan.current_step || 0)} / ${plan.total_steps} 步）。听懂了吗？哪里还不清楚？`
    : "这一步讲完了。听懂了吗？哪里还不清楚？";
  askHintEl.textContent = "";
  inputEl.focus();
}

/* -------------------------------------------------------------- 生成一页 */

/** 跑一轮任务（/pages 或 /messages），把流式输出画在卡片里。
 *
 *  两个入口共用它，因为除了"发不发一条用户消息"之外完全一样：
 *  都要继承超时/取消/断线恢复，都要在结束后重新取一遍页。 */
async function runTurn(request) {
  if (busy) return;
  busy = true;
  askEl.hidden = true;
  askPending = false;
  // 记下"这一批从第几页开始"：生成完要**从第一页读起**，
  // 而不是直接跳到末页——一步可能被切成两三页，跳过去就等于漏读。
  const startCount = pages.length;
  render();

  cardEl.classList.remove("is-skipped");
  cardEl.innerHTML =
    '<div class="slide-streaming"><span class="spinner"></span> <span id="cl-phase">备课中</span></div>' +
    '<div class="body"></div>';
  const phaseEl = cardEl.querySelector("#cl-phase");
  const bodyEl = cardEl.querySelector(".body");

  let accumulated = "";
  let scheduled = false;
  const paint = () => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      if (!bodyEl) return;
      bodyEl.classList.add("cursor");
      bodyEl.innerHTML = renderMarkdown(accumulated);
      cardEl.scrollTop = cardEl.scrollHeight;
    });
  };

  const finish = () => {
    busy = false;
    sendBtn.disabled = false;
  };

  // 轮询兜底：**不能只依赖 SSE 的 done**。
  //
  // 实际故障：页面停在"讲课中"，但切到对话再回来就好了——说明那一轮早就成功、
  // 页也已经落库，只是这个页面没收到（或没处理成）done。既然 `/api/turns/<id>`
  // 能查到终态，就用它兜底：每 5 秒问一次，一旦终态就按同样的流程收尾。
  // 这比"猜为什么没收到"可靠：网络、代理缓冲、页面被挂起都可能吞掉最后一个事件。
  let pollTimer = null;
  const stopPoll = () => {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  };

  /** 收尾：重新取一遍页并渲染。失败时**显示出来**，而不是静默停在流式那一帧。 */
  const settle = async () => {
    finish();
    stopPoll();
    activeTurn = null;
    if (stallEl) stallEl.hidden = true;

    let loaded = false;
    for (let attempt = 0; attempt < 2 && !loaded; attempt += 1) {
      try {
        await loadDeck();
        loaded = true;
      } catch (err) {
        if (attempt === 1) {
          showReloadNotice(err);
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 400));
      }
    }
    if (!loaded) return;

    // 这一批的末页就是"这一步讲完"的位置。
    stepEnd = pages.length - 1;
    // **自动翻到最新一页**：用户点了「继续讲」，就该直接看到新讲的那一页，
    // 而不是停在旧页上让人以为没反应（这是用户明确报过的问题）。
    index = Math.max(0, stepEnd);
    askPending = true;

    // 一步可能被切成好几页：自动跳到末页会跳过前面几页，
    // 所以给一条提示，点一下就能回到这一步的第一页。
    const batch = stepEnd - startCount + 1;
    if (hintEl && batch > 1) {
      hintEl.hidden = false;
      hintEl.textContent = `这一步共 ${batch} 页，已到最后一页 · 点这里从第 1 页看起`;
      hintEl.onclick = () => {
        index = Math.max(0, startCount);
        hintEl.hidden = true;
        render();
        cardEl.scrollTop = 0;
      };
    } else if (hintEl) {
      hintEl.hidden = true;
    }

    render();
    cardEl.scrollTop = 0;
  };

  try {
    const { turn_id: turnId } = await request();
    activeTurn = subscribe(turnId, {
      onStatus: (data) => {
        if (phaseEl) phaseEl.textContent = data.status || "";
        if (stallEl) stallEl.hidden = true;
      },
      // 连接还在、但很久没有新内容：多半是上游卡住了。
      // 不只提示，还给中止入口——不能让人只能干等（这是用户的真实抱怨）。
      onStall: (seconds) => {
        if (!stallEl) return;
        stallEl.hidden = false;
        stallEl.textContent = `已 ${seconds} 秒没有新内容，可能卡住了 · 点这里中止`;
        stallEl.onclick = async () => {
          await activeTurn?.cancel();
          toast("已请求中止，等这一轮收尾");
        };
      },
      onDelta: (data) => { accumulated += data.text || ""; paint(); },
      onWarn: (data) => toast(data.message),
      onAlert: (data) => toast(data.message, "warn"),
      onDone: () => { settle(); },
      onError: (data) => {
        finish();
        stopPoll();
        activeTurn = null;
        if (stallEl) stallEl.hidden = true;
        cardEl.innerHTML =
          `<div class="notice notice-error">${escapeHtml(data.message || "这一页没有生成成功")}</div>`;
      },
    });

    pollTimer = setInterval(async () => {
      if (!busy) return;
      try {
        const info = await get(`/api/turns/${turnId}`);
        if (info.status === "done") {
          if (hintEl) hintEl.hidden = true;
          await settle();
        } else if (info.status === "failed" || info.status === "cancelled") {
          // 走 SSE 的 onError 通常已经处理过；这里兜住"事件没到"的情况
          if (busy) throw new Error(info.error || "这一轮没有成功");
        }
      } catch (err) {
        stopPoll();
        finish();
        cardEl.innerHTML = `<div class="notice notice-error">${escapeHtml(err.message)}</div>`;
      }
    }, 5000);
  } catch (err) {
    finish();
    stopPoll();
    cardEl.innerHTML = `<div class="notice notice-error">${escapeHtml(err.message)}</div>`;
  }
}

/** 收尾之后取页失败：显示出来并给一个重试入口，别让人对着"讲课中"发呆。 */
function showReloadNotice(err) {
  reportClientError("classroom:reload", err);
  cardEl.innerHTML = `
    <div class="notice notice-warn">
      这一页已经生成，但刷新页面内容失败了：${escapeHtml(err && err.message ? err.message : "未知错误")}
    </div>
    <p class="slide-empty-hint">点下面的按钮重试；不行就刷新页面，内容已经保存好了。</p>
    <p><button type="button" id="cl-reload">重新载入</button></p>`;
  const button = cardEl.querySelector("#cl-reload");
  if (button) {
    button.addEventListener("click", async () => {
      try {
        await loadDeck({ jumpLast: true });
        stepEnd = pages.length - 1;
        index = Math.max(0, stepEnd);
        render();
      } catch (again) {
        toast(again.message, "error");
      }
    });
  }
}

function generateNextPage() {
  return runTurn(() => post(`/api/sessions/${sessionId}/pages`, {}));
}

function sendFeedback() {
  const content = inputEl.value.trim();
  if (!content) {
    // 空着也算"继续"：不强迫学生每步都说话
    inputEl.value = "";
    return runTurn(() => post(`/api/sessions/${sessionId}/pages`, {}));
  }
  inputEl.value = "";
  // intent=feedback：告诉后端"这不是随口提问，是这一步的反馈"，
  // 后端据此把教案推进到下一步（见 api/sessions.py 的说明）。
  return runTurn(() => post(`/api/sessions/${sessionId}/messages`, {
    content,
    intent: "feedback",
  }));
}

/* ------------------------------------------------------------------ 操作 */

function goPrev() {
  if (hintEl) hintEl.hidden = true;
  if (index > 0) {
    index -= 1;
    render();
    cardEl.scrollTop = 0;
  }
}

function goNext() {
  if (hintEl) hintEl.hidden = true;
  if (index < pages.length - 1) {
    index += 1;
    render();
    cardEl.scrollTop = 0;
    return;
  }
  // 已经是最后一页：这时候"下一页"的意思就是"这一步完了，说说你的反馈"
  if (!askEl.hidden) return;
  askPending = true;
  maybeAsk();
}

async function skipPage() {
  const page = currentPage();
  if (!page || busy) return;
  skipBtn.disabled = true;
  try {
    await post(`/api/sessions/${sessionId}/pages/${page.id}/skip`, {});
    if (index < pages.length - 1) {
      index += 1;
      await loadDeck();
    } else {
      await loadDeck();
      toast("这一页跳过了；点「继续讲」往下走");
    }
  } catch (err) {
    toast(err.message, "error");
    render();
  }
}

async function toggleFullscreen() {
  // 桌面版优先交给 pywebview 切窗口：WebView2 里 `requestFullscreen()` 常常
  // 被拒（不是用户手势 / 权限策略），而窗口全屏是真正有效的。
  const api = window.pywebview && window.pywebview.api;
  if (api && typeof api.toggle_fullscreen === "function") {
    try {
      await api.toggle_fullscreen();
      return;
    } catch {
      /* 落到浏览器的方案 */
    }
  }
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch {
    toast("这个窗口不支持系统全屏；课堂本身已经是整屏布局");
  }
}

/* ------------------------------------------------------------------ 小测 */

// 小测是课堂的第三个状态：讲授 → 反馈 → 小测。
//
// 三个区（设置 / 作答 / 结果）**同一时刻只显示一个**——靠 `showQuizSection()`
// 统一控制。第一版是各处分别设 `hidden`，结果 `.quiz-row{display:flex}` 把
// `[hidden]` 盖掉了，"交卷"和"再出一份"同时出现在屏幕上（用户截图里就是）。

const quizPanel = document.getElementById("cl-quiz-panel");
const quizTitleEl = document.getElementById("qz-title");
const quizHintEl = document.getElementById("qz-hint");
const quizSetupEl = document.getElementById("qz-setup");
const quizOpenHintEl = document.getElementById("qz-open-hint");
const quizResumeBtn = document.getElementById("qz-resume");
const quizBodyEl = document.getElementById("qz-body");
const quizSubmitRow = document.getElementById("qz-submit-row");
const quizResultEl = document.getElementById("qz-result");
const quizResultRow = document.getElementById("qz-result-row");
const quizCountEl = document.getElementById("qz-count");
const quizTypesEl = document.getElementById("qz-types");
const quizStartBtn = document.getElementById("qz-start");
const quizSubmitBtn = document.getElementById("qz-submit");
const quizSkipBtns = [document.getElementById("qz-skip"), document.getElementById("qz-skip-2")];

// 一次小测的临时状态：id + 题面（题面里没有答案，判完才有）
let quiz = { id: null, items: [], graded: false };
// 讲完之后自动弹一次设置（只弹一次，别在每次渲染时都弹）
let quizPrompted = false;
// 生成中的计时器（慢的时候要让人看见"还在动"）
let quizTimer = null;
let quizStartedAt = 0;

function showQuizSection(name) {
  quizSetupEl.hidden = name !== "setup";
  quizBodyEl.hidden = name !== "questions" && name !== "loading";
  quizSubmitRow.hidden = name !== "questions";
  quizResultEl.hidden = name !== "result";
  quizResultRow.hidden = name !== "result";
}

async function openQuiz() {
  quizPanel.hidden = false;
  showQuizSetup();
  quizCountEl.focus();
  await refreshResumeHint();
}

function closeQuiz() {
  stopQuizTimer();
  quizPanel.hidden = true;
}

function showQuizSetup() {
  showQuizSection("setup");
  quizTitleEl.textContent = "小测";
  quizHintEl.textContent = point.title ? `知识点：${point.title}` : "";
}

/** 上次的题还没做？摆在设置区上方，让人可以接着做（也省一次出题的钱）。 */
async function refreshResumeHint() {
  if (!quizOpenHintEl || !sessionId) return;
  quizOpenHintEl.hidden = true;
  try {
    const data = await get(`/api/sessions/${sessionId}/quiz`);
    const latest = data && data.quiz;
    if (!latest || latest.status !== "open") return;
    quizOpenHintEl.hidden = false;
    quizOpenHintEl.querySelector("span").textContent =
      `上次出的 ${latest.items.length} 道题还没做`;
    quizResumeBtn.dataset.quizId = String(latest.id);
  } catch {
    /* 取不到就不提示，不影响重新出题 */
  }
}

function readQuizOptions() {
  const count = Number(quizCountEl.value) || 5;
  const types = Array.from(quizTypesEl.querySelectorAll("input:checked"))
    .map((input) => input.value);
  return { count, types };
}

function startQuizTimer() {
  quizStartedAt = Date.now();
  const label = document.getElementById("qz-elapsed");
  stopQuizTimer();
  quizTimer = setInterval(() => {
    if (!label || !label.isConnected) return;
    const seconds = Math.floor((Date.now() - quizStartedAt) / 1000);
    label.textContent = `正在出题… ${seconds}s`;
  }, 1000);
}

function stopQuizTimer() {
  if (quizTimer) {
    clearInterval(quizTimer);
    quizTimer = null;
  }
}

/** 生成中：不确定进度的横条 + 已等秒数。 */
function showQuizLoading(text) {
  showQuizSection("loading");
  quizBodyEl.innerHTML = `
    <div class="quiz-loading">
      <span class="spinner"></span>
      <div class="quiz-loading-text" id="qz-elapsed">${escapeHtml(text || "正在出题… 0s")}</div>
      <div class="quiz-bar"><i></i></div>
    </div>`;
  startQuizTimer();
}

function showQuizError(message) {
  stopQuizTimer();
  showQuizSection("setup");
  quizHintEl.textContent = message || "出题没有成功，可以再试一次";
  quizStartBtn.disabled = false;
}

async function startQuiz() {
  const options = readQuizOptions();
  if (!options.types.length) {
    toast("至少选一种题型", "warn");
    return;
  }

  quizStartBtn.disabled = true;
  quiz.id = null;
  quiz.items = [];
  showQuizLoading("正在出题… 0s");
  quizTitleEl.textContent = "小测";
  quizHintEl.textContent = point.title ? `知识点：${point.title}` : "";

  try {
    const page = currentPage();
    const { turn_id: turnId } = await post(`/api/sessions/${sessionId}/quiz`, {
      count: options.count,
      types: options.types,
      page_id: page ? page.id : null,
    });

    subscribe(turnId, {
      onStatus: (data) => {
        const label = document.getElementById("qz-elapsed");
        if (label && data.status) label.dataset.status = data.status;
      },
      onQuiz: (data) => {
        // 题目在这儿才拿到（事件里只带 quiz_id，题面走接口取）
        if (data.quiz_id) loadQuiz(data.quiz_id);
      },
      onWarn: (data) => toast(data.message),
      onError: (data) => {
        showQuizError(data.message || "出题失败");
      },
      onDone: async () => {
        quizStartBtn.disabled = false;
        if (quiz.id) return;          // 已通过 quiz 事件拿到题
        // 兜底：quiz 事件没赶上（比如断线重连），用会话接口取最近一份
        try {
          const data = await get(`/api/sessions/${sessionId}/quiz`);
          if (data && data.quiz && data.quiz.id) {
            loadQuiz(data.quiz.id);
          } else {
            showQuizError("这次没有出成题，可以再试一次");
          }
        } catch (err) {
          showQuizError(err.message);
        }
      },
    });
  } catch (err) {
    showQuizError(err.message);
  }
}

async function loadQuiz(quizId) {
  stopQuizTimer();
  // 题目里常有公式：渲染前确保 KaTeX 就绪，否则会先闪一段源码
  await ensureKatex().catch(() => {});
  const data = await get(`/api/quizzes/${quizId}`);
  quiz = { id: data.id, items: data.items || [], graded: data.status === "graded" };

  if (data.status === "graded") {
    // 刷新过之后又点"小测"：直接显示上一次的结果，而不是重出一份
    quizHintEl.textContent = "上一次的小测结果";
    renderQuizResult(data);
    return;
  }

  quizTitleEl.textContent = `小测 · ${quiz.items.length} 题`;
  quizHintEl.textContent = point.title ? `知识点：${point.title}` : "";
  renderQuizQuestions();
}

function renderQuizQuestions() {
  showQuizSection("questions");
  quizBodyEl.innerHTML = quiz.items.map((item, position) => {
    const index = position + 1;
    return `
      <div class="quiz-item" data-item="${escapeHtml(item.id)}" data-type="${escapeHtml(item.type)}">
        <div class="quiz-stem">${index}. ${renderInline(item.stem || "")}</div>
        ${renderQuizInput(item)}
      </div>`;
  }).join("");
}

function renderQuizInput(item) {
  if (item.type === "fill") {
    return `<input type="text" class="quiz-fill" autocomplete="off"
                   placeholder="在这里填（只填一个数或式子）">`;
  }
  const kind = item.type === "multi_choice" ? "checkbox" : "radio";
  const name = `quiz-${item.id}`;
  return `<div class="quiz-options">${(item.options || []).map((option) => `
    <label class="quiz-option">
      <input type="${kind}" name="${escapeHtml(name)}" value="${escapeHtml(option.key)}">
      <span>${escapeHtml(option.key)}. ${renderInline(stripOptionLabel(option.key, option.text))}</span>
    </label>`).join("")}</div>`;
}

function collectAnswers() {
  const answers = {};
  for (const node of quizBodyEl.querySelectorAll(".quiz-item")) {
    const id = node.dataset.item;
    const type = node.dataset.type;
    if (type === "fill") {
      answers[id] = node.querySelector(".quiz-fill").value;
      continue;
    }
    const checked = Array.from(node.querySelectorAll("input:checked"))
      .map((input) => input.value);
    answers[id] = type === "multi_choice" ? checked : (checked[0] || "");
  }
  return answers;
}

async function submitQuiz() {
  const answers = collectAnswers();
  const filled = Object.values(answers).some((value) =>
    Array.isArray(value) ? value.length > 0 : String(value || "").trim() !== "");
  if (!filled) {
    toast("还一道都没答", "warn");
    return;
  }

  quizSubmitBtn.disabled = true;
  try {
    const data = await post(`/api/quizzes/${quiz.id}/submit`, { answers });
    renderQuizResult(data);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    quizSubmitBtn.disabled = false;
  }
}

function renderQuizResult(data) {
  showQuizSection("result");

  const graded = data.items || [];
  const total = graded.length || 1;
  const right = graded.filter((one) => one.correct).length;
  const percent = Math.round(((data.score != null ? data.score : right / total) || 0) * 100);

  const weak = (data.weak_steps || []).map((step) => `第 ${Number(step) + 1} 步`);
  quizTitleEl.textContent = "小测结果";
  quizHintEl.textContent = weak.length ? `薄弱：${weak.join("、")}` : "没有明显薄弱的地方";

  if (data.skipped) {
    quizResultEl.innerHTML =
      '<div class="notice notice-warn">这次跳过了小测：不记成绩，也不算通过。</div>';
    return;
  }

  quizResultEl.innerHTML = `
    <div class="quiz-score"><strong>${percent}%</strong>
      <span class="quiz-hint">答对 ${right} / ${total}</span></div>
    <ol class="quiz-review">
      ${graded.map((item, position) => {
        const mark = item.correct ? "✓" : "✗";
        const cls = item.correct ? "ok" : "bad";
        const given = Array.isArray(item.response)
          ? (item.response.length ? item.response.join("、") : "（空）")
          : (String(item.response ?? "").trim() || "（空）");
        return `<li class="quiz-review-item ${cls}">
          <div class="quiz-review-head">
            <span class="quiz-mark">${mark}</span>
            <span>第 ${position + 1} 题</span>
            ${item.step != null ? `<span class="quiz-hint">对应第 ${Number(item.step) + 1} 步</span>` : ""}
          </div>
          <div class="quiz-review-body">
            <div>你的答案：${escapeHtml(given)}</div>
            <div>正确答案：${escapeHtml((item.answer || []).join("、"))}</div>
            ${item.explain ? `<div class="quiz-explain">${renderMarkdown(item.explain)}</div>` : ""}
          </div>
        </li>`;
      }).join("")}
    </ol>`;
}

async function skipQuiz() {
  if (!quiz.id) {
    // 还没出题就跳过：只关闭面板（没有可记录的"跳过"）
    closeQuiz();
    return;
  }
  try {
    const data = await post(`/api/quizzes/${quiz.id}/skip`);
    renderQuizResult(data);
  } catch (err) {
    toast(err.message, "error");
  }
}

quizStartBtn?.addEventListener("click", startQuiz);
quizSubmitBtn?.addEventListener("click", submitQuiz);
quizSkipBtns.forEach((button) => button?.addEventListener("click", skipQuiz));
quizPanel?.querySelector("#qz-close")?.addEventListener("click", closeQuiz);
quizPanel?.querySelector("#qz-back")?.addEventListener("click", closeQuiz);
quizPanel?.querySelector("#qz-export")?.addEventListener("click", () => {
  if (quiz.id) location.href = `/api/quizzes/${quiz.id}/export.docx`;
});
quizPanel?.querySelector("#qz-again")?.addEventListener("click", () => {
  quiz = { id: null, items: [], graded: false };
  showQuizSetup();
  refreshResumeHint();
});
quizResumeBtn?.addEventListener("click", () => {
  const quizId = Number(quizResumeBtn.dataset.quizId);
  if (quizId) loadQuiz(quizId);
});
document.getElementById("cl-quiz")?.addEventListener("click", openQuiz);

/* ------------------------------------------------------------------ 事件 */

prevBtn.addEventListener("click", goPrev);
nextBtn.addEventListener("click", goNext);
skipBtn.addEventListener("click", skipPage);
genBtn.addEventListener("click", generateNextPage);
sendBtn.addEventListener("click", sendFeedback);
fullBtn.addEventListener("click", toggleFullscreen);

inputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendFeedback();
  }
});

document.addEventListener("keydown", (event) => {
  const tag = (event.target && event.target.tagName || "").toLowerCase();
  if (tag === "textarea" || tag === "input") return;

  if (event.key === "ArrowLeft") { event.preventDefault(); goPrev(); return; }
  if (event.key === "ArrowRight") { event.preventDefault(); goNext(); return; }
  if (event.key.toLowerCase() === "f") { event.preventDefault(); toggleFullscreen(); return; }
  if (event.key === "Escape") {
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
      return;
    }
    location.href = `/course/${courseId}`;
  }
});

/* ------------------------------------------------------------------ 启动 */

async function boot() {
  if (!courseId) {
    toast("地址里没有课程编号", "error");
    return;
  }
  await ensureKatex().catch(() => {});
  try {
    const created = await post("/api/sessions", { course_id: courseId });
    sessionId = created.id;
    // 从对话页跳过来时会带 `?page=N`：直接停在那个人刚才看到的那一页，
    // 而不是又跳回最后一页。这是"两个页面之间不迷路"的关键一步。
    const raw = new URLSearchParams(location.search).get("page");
    const wanted = raw === null ? null : Number(raw);

    await loadDeck({ jumpLast: wanted === null || Number.isNaN(wanted) });
    if (wanted !== null && !Number.isNaN(wanted) && pages.length) {
      index = Math.min(Math.max(0, wanted), pages.length - 1);
      render();
    }
    // 打开时如果已经有页，就把"最后一页"当作这一步的末页——但**不**立刻弹反馈：
    // 刚进来还没读，先让人看。
    stepEnd = pages.length - 1;
  } catch (err) {
    cardEl.innerHTML =
      `<div class="notice notice-error">课堂打不开：${escapeHtml(err.message)}</div>`;
  }
}

/**
 * 把页面侧的异常报回服务端日志。
 *
 * 桌面版没有控制台，用户能提供的信息只有"卡住了"。有了这条通道，
 * 下次再出现"停在讲课中"时，instance/v4_win.log 里会有真正的堆栈。
 */
function reportClientError(where, error) {
  const message = String((error && (error.stack || error.message)) || error || "");
  try {
    fetch("/api/client-log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ where, message: message.slice(0, 2000) }),
    });
  } catch {
    /* 连这个都失败就算了，不能因为日志把页面搞坏 */
  }
}

window.addEventListener("unhandledrejection", (event) => {
  reportClientError("classroom:unhandled", event.reason);
});

boot();
