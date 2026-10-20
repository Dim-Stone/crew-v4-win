// 章末测试页：出题 → 作答 → 判分 → 导出 Word。
//
// 与课堂里的小测共用同一套后端（判分规则、信号、题面结构），差别只有两点：
//   · 出题接口是 `/chapter-quiz`（按整章的知识点归因）；
//   · 判完会给"每个知识点"的得分，并可以导出 .docx。

import { get, post, toast } from "../api.js";
import { subscribe } from "../sse.js";
import { escapeHtml, renderInline, renderMarkdown } from "../md.js";
import { stripOptionLabel } from "../quiz_ui.js";
import { ensureKatex } from "../latex.js";

// 地址形如 /course/3/chapter/5/test
const parts = location.pathname.split("/").filter(Boolean);
const courseId = Number(parts[1]);
// 章末测试可以切成别的章：题目要按**那一章**的重难点出。
let chapterId = Number(parts[3]);

const titleEl = document.getElementById("ct-title");
const hintEl = document.getElementById("ct-hint");
const exportBtn = document.getElementById("ct-export");
const setupEl = document.getElementById("ct-setup");
const bodyEl = document.getElementById("ct-body");
const submitRow = document.getElementById("ct-submit-row");
const resultEl = document.getElementById("ct-result");
const resultRow = document.getElementById("ct-result-row");
const countEl = document.getElementById("ct-count");
const chapterEl = document.getElementById("ct-chapter");
const typesEl = document.getElementById("ct-types");
const startBtn = document.getElementById("ct-start");
const submitBtn = document.getElementById("ct-submit");
const skipBtns = [document.getElementById("ct-skip"), document.getElementById("ct-skip-2")];

let sessionId = null;
let quiz = { id: null, items: [] };

/* ------------------------------------------------------------------ 状态 */

function showSection(name) {
  setupEl.hidden = name !== "setup";
  bodyEl.hidden = name !== "questions" && name !== "loading";
  submitRow.hidden = name !== "questions";
  resultEl.hidden = name !== "result";
  resultRow.hidden = name !== "result";
  exportBtn.hidden = name !== "result";
}

/* ------------------------------------------------------------------ 出题 */

async function boot() {
  await ensureKatex().catch(() => {});
  try {
    const created = await post("/api/sessions", { course_id: courseId });
    sessionId = created.id;
    const session = await get(`/api/sessions/${sessionId}`);
    const chapters = (session.course.chapters || []).filter((one) => one.id);
    chapterEl.innerHTML = chapters
      .map((one) => `<option value="${one.id}">${escapeHtml(one.title)}</option>`)
      .join("");
    if (!chapters.some((one) => one.id === chapterId)) {
      // 地址里的章不在本课（或没给）：退回当前章，再退回第一章
      chapterId = (session.current_chapter && session.current_chapter.id)
        || (session.current_point && session.current_point.chapter_id)
        || (chapters[0] && chapters[0].id);
    }
    if (chapterId) chapterEl.value = String(chapterId);
    syncChapterTitle(chapters);
    showSection("setup");
  } catch (err) {
    hintEl.textContent = `打不开：${err.message}`;
  }
}

/** 标题与地址栏跟着选中的章走（刷新后还停在这一章）。 */
function syncChapterTitle(chapters) {
  const chapter = (chapters || []).find((one) => one.id === chapterId);
  titleEl.textContent = chapter
    ? `${chapter.title} · 章末测试`
    : "章末测试";
  if (chapterId) {
    history.replaceState(null, "", `/course/${courseId}/chapter/${chapterId}/test`);
  }
}

function readOptions() {
  return {
    count: Number(countEl.value) || 10,
    types: Array.from(typesEl.querySelectorAll("input:checked")).map((input) => input.value),
  };
}

async function startQuiz() {
  const options = readOptions();
  if (!options.types.length) {
    toast("至少选一种题型", "warn");
    return;
  }

  startBtn.disabled = true;
  hintEl.textContent = "";
  showSection("loading");
  bodyEl.innerHTML =
    '<div class="quiz-loading"><span class="spinner"></span>' +
    '<div class="quiz-loading-text">正在整理整章内容并出题…</div>' +
    '<div class="quiz-bar"><i></i></div></div>';

  try {
    const { turn_id: turnId } = await post(`/api/sessions/${sessionId}/chapter-quiz`, {
      chapter_id: chapterId,
      count: options.count,
      types: options.types,
    });

    subscribe(turnId, {
      onStatus: (data) => { hintEl.textContent = data.status || ""; },
      onQuiz: (data) => { if (data.quiz_id) loadQuiz(data.quiz_id); },
      onWarn: (data) => toast(data.message),
      onError: (data) => {
        startBtn.disabled = false;
        showSection("setup");
        hintEl.textContent = data.message || "出题失败";
      },
      onDone: async () => {
        startBtn.disabled = false;
        if (quiz.id) return;
        // 兜底：事件没到就从最近一份取（与课堂同一套做法）
        try {
          const data = await get(
            `/api/sessions/${sessionId}/chapter-quiz?chapter_id=${chapterId}`,
          );
          if (data.quiz && data.quiz.id) loadQuiz(data.quiz.id);
          else {
            showSection("setup");
            hintEl.textContent = "这次没有出成题，可以再试一次";
          }
        } catch (err) {
          showSection("setup");
          hintEl.textContent = err.message;
        }
      },
    });
  } catch (err) {
    startBtn.disabled = false;
    showSection("setup");
    hintEl.textContent = err.message;
  }
}

async function loadQuiz(quizId) {
  await ensureKatex().catch(() => {});
  const data = await get(`/api/quizzes/${quizId}`);
  quiz = { id: data.id, items: data.items || [] };
  hintEl.textContent = `共 ${quiz.items.length} 题`;
  if (data.status === "graded") {
    renderResult(data);
    return;
  }
  renderQuestions();
}

/* ---------------------------------------------------------------- 作答区 */

function renderQuestions() {
  showSection("questions");
  bodyEl.innerHTML = quiz.items.map((item, position) => `
    <div class="quiz-item" data-item="${escapeHtml(item.id)}" data-type="${escapeHtml(item.type)}">
      <div class="quiz-stem">${position + 1}. ${renderInline(item.stem || "")}</div>
      ${renderInput(item)}
    </div>`).join("");
}

function renderInput(item) {
  if (item.type === "fill") {
    return '<input type="text" class="quiz-fill" autocomplete="off" placeholder="在这里填">';
  }
  const kind = item.type === "multi_choice" ? "checkbox" : "radio";
  return `<div class="quiz-options">${(item.options || []).map((option) => `
    <label class="quiz-option">
      <input type="${kind}" name="ct-${escapeHtml(item.id)}" value="${escapeHtml(option.key)}">
      <span>${escapeHtml(option.key)}. ${renderInline(stripOptionLabel(option.key, option.text))}</span>
    </label>`).join("")}</div>`;
}

function collectAnswers() {
  const answers = {};
  for (const node of bodyEl.querySelectorAll(".quiz-item")) {
    const id = node.dataset.item;
    const type = node.dataset.type;
    if (type === "fill") {
      answers[id] = node.querySelector(".quiz-fill").value;
      continue;
    }
    const checked = Array.from(node.querySelectorAll("input:checked")).map((i) => i.value);
    answers[id] = type === "multi_choice" ? checked : (checked[0] || "");
  }
  return answers;
}

async function submitQuiz() {
  const answers = collectAnswers();
  const answered = Object.values(answers).some((value) =>
    Array.isArray(value) ? value.length : String(value || "").trim());
  if (!answered) {
    toast("还一道都没答", "warn");
    return;
  }

  submitBtn.disabled = true;
  try {
    const data = await post(`/api/quizzes/${quiz.id}/submit`, { answers });
    renderResult(data);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    submitBtn.disabled = false;
  }
}

/* ---------------------------------------------------------------- 结果区 */

function renderResult(data) {
  showSection("result");
  const items = data.items || [];
  const aggregate = data.aggregate || {};
  const percent = Math.round((data.score || 0) * 100);

  const points = Object.entries(aggregate)
    .map(([pointId, bucket]) => ({ pointId, ...bucket }))
    .sort((a, b) => (a.score ?? 0) - (b.score ?? 0));

  const weak = points.filter((one) => (one.score ?? 0) < 0.6);

  resultEl.innerHTML = `
    <div class="quiz-score"><strong>${percent}%</strong>
      <span class="quiz-hint">答对 ${items.filter((one) => one.correct).length} / ${items.length}</span></div>
    ${points.length ? `
      <div class="ct-points">
        <h3>按知识点</h3>
        ${points.map((one) => `
          <div class="ct-point ${one.score < 0.6 ? "bad" : "ok"}">
            <span>${escapeHtml(one.title || `知识点 ${one.pointId}`)}</span>
            <span class="quiz-hint">${one.right}/${one.total} · ${Math.round(one.score * 100)}%</span>
          </div>`).join("")}
      </div>` : ""}
    ${weak.length ? `<p class="notice notice-warn">需要回看：${weak.map((one) => escapeHtml(one.title)).join("、")}</p>` : ""}
    <ol class="quiz-review">
      ${items.map((item, position) => `
        <li class="quiz-review-item ${item.correct ? "ok" : "bad"}">
          <div class="quiz-review-head">
            <span class="quiz-mark">${item.correct ? "✓" : "✗"}</span>
            <span>第 ${position + 1} 题</span>
          </div>
          <div class="quiz-review-body">
            <div>你的答案：${escapeHtml(Array.isArray(item.response) ? item.response.join("、") : String(item.response ?? "（空）"))}</div>
            <div>正确答案：${escapeHtml((item.answer || []).join("、"))}</div>
            ${item.explain ? `<div class="quiz-explain">${renderMarkdown(item.explain)}</div>` : ""}
          </div>
        </li>`).join("")}
    </ol>`;
}

/* ------------------------------------------------------------------ 事件 */

startBtn?.addEventListener("click", startQuiz);
submitBtn?.addEventListener("click", submitQuiz);
skipBtns.forEach((button) => button?.addEventListener("click", async () => {
  if (!quiz.id) return;
  try {
    const data = await post(`/api/quizzes/${quiz.id}/skip`);
    renderResult(data);
  } catch (err) {
    toast(err.message, "error");
  }
}));
chapterEl?.addEventListener("change", () => {
  chapterId = Number(chapterEl.value);
  // 换章等于换一张卷子：丢掉当前这份，回到设置
  quiz = { id: null, items: [] };
  hintEl.textContent = "";
  showSection("setup");
  syncChapterTitle(Array.from(chapterEl.options).map((one) => ({
    id: Number(one.value),
    title: one.textContent,
  })));
});

document.getElementById("ct-again")?.addEventListener("click", () => {
  quiz = { id: null, items: [] };
  hintEl.textContent = "";
  showSection("setup");
});
document.getElementById("ct-back")?.addEventListener("click", () => {
  location.href = `/course/${courseId}`;
});
exportBtn?.addEventListener("click", () => {
  if (!quiz.id) return;
  // 直接触发下载：后端组织成 .docx（问卷 + 答案页）
  location.href = `/api/quizzes/${quiz.id}/export.docx`;
});

window.addEventListener("unhandledrejection", (event) => {
  // 桌面版没有控制台：把页面异常写进服务端日志，别再"点不开"却查不到原因
  try {
    fetch("/api/client-log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        where: "chapter_test",
        message: String((event.reason && (event.reason.stack || event.reason.message)) || event.reason),
      }),
    });
  } catch {
    /* 日志失败不影响页面 */
  }
});

boot();
