// 工作台：今天（日期 + 待办）+ 我的课程。
//
// 顺序是有意的：**先今天，再课程**。"我今天要做什么"比"我有哪些课"更常被先问起。
//
// 待办只做"显示 + 新增"（一期范围，见 README）：没有完成态、没有删除，
// 界面上也就不摆那两个按钮——点了没反应的控件比没有更糟。
// 画像那一块（"系统对你的理解"）曾经摆在两者之间，2026-09 按使用者要求撤掉了：
// 那几行字每门课都一样、又占掉屏幕高度，需要的画像信息在开课页与讲师讲法里都看得到。

import { del, get, patch, post, put, toast } from "../api.js";
import * as dates from "../dates.js";
import { escapeHtml } from "../md.js";

const coursesBox = document.getElementById("courses");
const summaryEl = document.getElementById("summary");
const overallEl = document.getElementById("overall");
const todoListEl = document.getElementById("todo-list");
const todoFormEl = document.getElementById("todo-form");
const todoInputEl = document.getElementById("todo-input");
const todoCountEl = document.getElementById("todo-count");

// 记住最后一次拉到的列表：删除课程时要拿它把课程信息拼进确认框。
// （比往按钮上塞一堆 data-* 属性更好读，也免得把标题塞进 HTML）
let lastCourses = [];

// 上一次各课程的大纲状态。用来发现"刚刚从规划中变成了别的"——
// 那正是用户想知道"我的课建好了没有"的时刻。
const knownStates = new Map();
const POLL_MS = 4000;
let pollTimer = null;

/* ---------------------------------------------------------------- 今天 */

/** 当前选中的日期（本地零点的 `Date`）。
 *
 *  **它是这一块的唯一状态**：待办和记事都按它取，切日期 = 改它 + 重拉两边。
 *  之所以不把日期塞进 DOM 再读回来：那样"当前是哪天"会有两个来源，
 *  一处没同步就会出现"显示的是今天、写进去的是昨天"。 */
let selected = dates.today();

const dateEls = {
  year: document.getElementById("date-year"),
  month: document.getElementById("date-month"),
  day: document.getElementById("date-day"),
};
const weekEl = document.getElementById("today-week");
const todayBtn = document.getElementById("date-today");
const noteEl = document.getElementById("note-text");
const noteStatusEl = document.getElementById("note-status");

/** 把选中日期画到界面上：三个数字 + 星期 + "回到今天"该不该出现。 */
function renderDate() {
  const p = dates.parts(selected);
  dateEls.year.textContent = p.year;
  dateEls.month.textContent = p.month;
  dateEls.day.textContent = p.day;

  const wd = dates.weekday(selected);
  weekEl.textContent = dates.isToday(selected) ? `${wd} · 今天` : wd;
  // 已经在今天了就别摆一个"点了没反应"的按钮
  todayBtn.hidden = dates.isToday(selected);
}

/** 换到另一天。
 *
 *  **顺序不能反**：先把没保存的记事落盘，再改 `selected`。反过来写的话，
 *  那次保存会带着**新**日期发出去——用户看到的是"我的记事跑到明天去了"。 */
async function goTo(date) {
  if (dates.iso(date) === dates.iso(selected)) return;
  await flushNote();
  selected = date;
  renderDate();
  await Promise.all([loadTodos(), loadNote()]);
}

document.getElementById("date-picker").addEventListener("click", (event) => {
  const button = event.target.closest(".date-step");
  if (!button) return;
  const { unit, step } = button.dataset;
  goTo(dates.shift(selected, unit, Number(step)));
});

todayBtn.addEventListener("click", () => goTo(dates.today()));

/* ---------------------------------------------------------------- 待办 */

/** 勾选框里的对勾（画出来的，不用 ☑ 这种字符：字形随字体变）。 */
const CHECK_ICON = `<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
  <path d="M3.5 8.5l3 3 6-7"/></svg>`;

/** 删掉这一条。用一个细"×"而不是课程卡上那个垃圾桶：
 *  待办又短又密，垃圾桶在那里显得太重。 */
const REMOVE_ICON = `<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
  <path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/></svg>`;

function todoRow(item) {
  return `
    <li class="todo-item${item.done ? " is-done" : ""}" data-id="${item.id}">
      <button class="todo-check" type="button" role="checkbox"
              aria-checked="${item.done}"
              aria-label="${item.done ? "取消完成" : "标记完成"}">${CHECK_ICON}</button>
      <span class="todo-text">${escapeHtml(item.text)}</span>
      <button class="todo-remove" type="button" aria-label="删掉这一条">${REMOVE_ICON}</button>
    </li>`;
}

function renderTodos(items) {
  if (!items.length) {
    todoListEl.innerHTML = '<li class="todo-empty">这一天还没有待办。</li>';
    todoCountEl.textContent = "";
    return;
  }
  todoListEl.innerHTML = items.map(todoRow).join("");
  refreshCount();
}

/** 计数从**当前 DOM** 读。
 *
 *  勾选和删除都是就地改 DOM（不重拉列表），所以计数也只能从 DOM 算，
 *  这样两边永远说的是同一件事。 */
function refreshCount() {
  const rows = [...todoListEl.querySelectorAll(".todo-item")];
  const left = rows.filter((row) => !row.classList.contains("is-done")).length;
  todoCountEl.textContent = rows.length ? `未完成 ${left} / 共 ${rows.length}` : "";
}

async function loadTodos() {
  try {
    const data = await get(`/api/todos?date=${dates.iso(selected)}`);
    renderTodos(data.items);
    // 换了日期就从第一条看起（列表按写下的先后排，头几条最近也最要紧）
    todoListEl.scrollTop = 0;
  } catch (err) {
    todoListEl.innerHTML =
      `<li class="notice notice-error">待办加载失败：${escapeHtml(err.message)}</li>`;
  }
}

todoFormEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = todoInputEl.value.trim();
  if (!text) return;

  const button = todoFormEl.querySelector("button");
  button.disabled = true;
  try {
    await post("/api/todos", { text, date: dates.iso(selected) });
    todoInputEl.value = "";
    // 重新拉一次，而不是就地追加：顺序与最终内容都以服务端为准
    await loadTodos();
    // 新加的那条落在**列表末尾**：列表满了它就在视野之外，不滚过去的话
    // 用户会以为没加上，然后又输一遍。
    todoListEl.scrollTop = todoListEl.scrollHeight;
  } catch (err) {
    toast(err.message, "error");
  } finally {
    button.disabled = false;
    todoInputEl.focus();
  }
});

/** 把勾选框的可见状态对齐到 `is-done`（类名是唯一事实，别处只读它）。 */
function syncCheck(row) {
  const done = row.classList.contains("is-done");
  const box = row.querySelector(".todo-check");
  box.setAttribute("aria-checked", String(done));
  box.setAttribute("aria-label", done ? "取消完成" : "标记完成");
}

// 勾选与删除走**事件委托**：列表每次都是整段重画的，
// 逐个挂监听会在重画时全部丢掉（症状是"点第一下有用、之后就没了"）。
//
// 两个动作都**原地改这一行**，不重拉整段列表——重拉会把 DOM 换掉，
// 用户点的那一行连同滚动位置一起跳走，而"接着勾下一条"是最常见的动作。
todoListEl.addEventListener("click", async (event) => {
  const row = event.target.closest(".todo-item");
  if (!row) return;
  const id = row.dataset.id;
  const wasDone = row.classList.contains("is-done");

  if (event.target.closest(".todo-check")) {
    // 先乐观地翻过来：点一下要立刻有反应
    row.classList.toggle("is-done", !wasDone);
    syncCheck(row);
    refreshCount();
    try {
      await patch(`/api/todos/${id}`, { done: !wasDone });
    } catch (err) {
      // 没存上就翻回去——界面上不能留下一个库里没有的状态
      row.classList.toggle("is-done", wasDone);
      syncCheck(row);
      refreshCount();
      toast(err.message, "error");
    }
    return;
  }

  if (event.target.closest(".todo-remove")) {
    try {
      await del(`/api/todos/${id}`);
      row.remove();                            // 只移掉这一行，其余的原地不动
      if (todoListEl.querySelector(".todo-item")) refreshCount();
      else renderTodos([]);
    } catch (err) {
      toast(err.message, "error");
    }
  }
});

/* ---------------------------------------------------------------- 记事 */

/** 上一次**成功写进库**的内容。用来判断"有没有真的变过"——
 *  切日期、失焦都会调保存，没变过就不该白发一次请求。 */
let noteSaved = "";
let noteTimer = null;

async function loadNote() {
  try {
    const note = await get(`/api/day-notes?date=${dates.iso(selected)}`);
    noteEl.value = note.text || "";
    noteSaved = noteEl.value;
    noteStatusEl.textContent = "";
  } catch (err) {
    noteStatusEl.textContent = `读取失败：${err.message}`;
  }
}

async function saveNote() {
  const text = noteEl.value;
  if (text === noteSaved) return;
  try {
    await put("/api/day-notes", { date: dates.iso(selected), text });
    noteSaved = text;
    noteStatusEl.textContent = "已保存";
  } catch (err) {
    // 保存失败要靠常驻文字说，不能只弹一下就没了——
    // 用户会以为自己刚写的东西还在库里。
    noteStatusEl.textContent = `没存上：${err.message}`;
    toast(`记事没存上：${err.message}`, "error");
  }
}

/** 立刻把它保存掉（不等防抖那 1.2 秒）。切日期、离开页面前调它。 */
function flushNote() {
  if (noteTimer) {
    clearTimeout(noteTimer);
    noteTimer = null;
  }
  return saveNote();
}

noteEl.addEventListener("input", () => {
  noteStatusEl.textContent = "…";
  clearTimeout(noteTimer);
  // 停手 1.2 秒再发：一边打字一边发会把每句话都变成一次 PUT
  noteTimer = setTimeout(() => {
    noteTimer = null;
    saveNote();
  }, 1200);
});

// 关窗/刷新前尽力补一次。**不保证送达**（浏览器不等异步请求），
// 但能覆盖"刚好在防抖窗口里关掉"这种最常见的情况。
addEventListener("beforeunload", () => {
  if (noteTimer) flushNote();
});

/* ---------------------------------------------------------------- 课程 */

/** 删除图标（细线 stroke，与整站"用线表达"的做法一致）。
 *  不用 unicode 字符当图标：字形随字体变，位置也压不准。 */
const TRASH_ICON = `<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true" focusable="false">
  <path d="M3 4.6h10M6.6 4.6V3h2.8v1.6M4.6 4.6l.6 8.1h5.6l.6-8.1"
        fill="none" stroke="currentColor" stroke-width="1.2"
        stroke-linecap="round" stroke-linejoin="round"/>
</svg>`;

/** 课程卡片上的"大纲状态"徽标。
 *
 *  阶段文字（"读资料 3/12"）来自数据库里的 Turn.stage，所以**离开页面再回来
 *  也看得到**——进度不是只活在那条 SSE 里。
 *  只保留这一种徽标：内容来源（严格按资料 / 资料作参考）那种曾在每张卡片上
 *  各占一行，几张卡片因此高矮不齐，2026-09 按使用者要求撤掉了。 */
function outlineBadge(course) {
  if (course.outline_state === "running") {
    const stage = course.outline_stage ? `：${escapeHtml(course.outline_stage)}` : "…";
    return `<span class="badge badge-accent">正在规划${stage}</span>`;
  }
  if (course.outline_state === "failed") {
    return '<span class="badge badge-warn">上次规划失败</span>';
  }
  return "";
}

/** 发现某门课"刚规划完"，就地提醒一次。
 *
 *  为什么需要它：用户很可能点了生成就回工作台等，而那时页面上
 *  什么都不会动——不提醒的话，他只能一遍遍点进来确认。 */
function noteStateChanges(courses) {
  for (const course of courses) {
    const before = knownStates.get(course.id);
    const now = course.outline_state;

    if (before === "running" && now !== "running") {
      if (now === "ready") {
        toast(`「${course.title}」的大纲已建立完毕，可以开始学习了`, "ok");
      } else if (now === "failed") {
        toast(
          `「${course.title}」的大纲没能生成：${course.outline_error || "原因不明"}`,
          "error",
        );
      }
    }
    knownStates.set(course.id, now);
  }
}

/** 有课在规划时才轮询——没有在跑的就别一直打接口。 */
function schedulePoll() {
  clearTimeout(pollTimer);
  if (!lastCourses.some((course) => course.outline_state === "running")) return;
  pollTimer = setTimeout(reload, POLL_MS);
}

document.getElementById("new-course").addEventListener("click", () => {
  location.href = "/new";
});

/** 主按钮指向哪、叫什么，取决于课程现在处在什么状态。
 *
 *  正在规划时给的是"查看进度"而不是"继续完善"——后者会让人以为
 *  上次的活儿白干了（而其实它还在后台跑着）。 */
function primaryAction(course) {
  if (course.outline_state === "running") {
    return `<a class="button" href="/new?course=${course.id}">查看进度</a>`;
  }
  if (course.outline_state === "failed") {
    return `<a class="button" href="/new?course=${course.id}">重新规划</a>`;
  }
  return course.status === "ready"
    ? `<a class="button primary" href="/course/${course.id}">继续学习</a>`
    : `<a class="button" href="/new?course=${course.id}">继续完善</a>`;
}

/** 规划中的课程不给删：后台线程还在往它里面写章节。 */
function deleteButton(course) {
  if (course.can_delete === false) {
    return `<button class="icon-btn" disabled title="正在生成大纲，等它结束再删"
                    aria-label="删除课程">${TRASH_ICON}</button>`;
  }
  return `<button class="icon-btn danger" data-delete="${course.id}"
                  title="删除课程" aria-label="删除课程">${TRASH_ICON}</button>`;
}

function courseCard(course) {
  const percent = course.total_points
    ? Math.round((course.done_points / course.total_points) * 100)
    : 0;
  // 只有大纲状态一种徽标（见 outlineBadge 的说明）
  const badges = outlineBadge(course);

  return `
    <article class="course-card">
      <div class="card-top">
        <h3 class="card-title">
          <a href="/course/${course.id}">${escapeHtml(course.title)}</a>
        </h3>
        ${deleteButton(course)}
      </div>

      ${badges ? `<div class="card-badges">${badges}</div>` : ""}

      <div class="card-progress">
        <span class="num">${course.done_points}</span>
        <span class="slash">/</span>
        <span class="num">${course.total_points}</span>
        <span class="card-progress-label">个知识点已讲</span>
        <span class="faint">· ${course.chapters} 章</span>
      </div>
      <div class="progress-line"><i style="width:${percent}%"></i></div>

      <div class="card-foot">
        ${primaryAction(course)}
        <span class="faint num">${percent}%</span>
      </div>
    </article>`;
}

function renderCourses(courses) {
  lastCourses = courses;

  if (!courses.length) {
    summaryEl.textContent = "";
    overallEl.classList.add("hidden");
    coursesBox.innerHTML = `
      <div class="empty">
        <strong>还没有课程</strong>
        新建一门，系统会先问几个问题，再据此安排学习路径。
      </div>`;
    return;
  }

  const totals = courses.reduce(
    (acc, course) => {
      acc.points += course.total_points;
      acc.done += course.done_points;
      return acc;
    },
    { points: 0, done: 0 },
  );
  summaryEl.textContent =
    `${courses.length} 门课程 · 共 ${totals.points} 个知识点，已讲过 ${totals.done} 个`;

  // 全部课程合起来的比例（单门课的进度在各张卡片上）
  if (totals.points) {
    overallEl.classList.remove("hidden");
    overallEl.querySelector("i").style.width =
      `${Math.round((totals.done / totals.points) * 100)}%`;
  } else {
    overallEl.classList.add("hidden");
  }

  coursesBox.innerHTML = `<div class="cards">${courses.map(courseCard).join("")}</div>`;
}

function reload() {
  get("/api/courses")
    .then((courses) => {
      // 先比对状态再渲染：渲染是"现在长什么样"，
      // 比对是为了发现"刚刚变了"——那是该提醒用户的时刻。
      noteStateChanges(courses);
      renderCourses(courses);
      schedulePoll();
    })
    .catch((err) => {
      summaryEl.textContent = "";
      coursesBox.innerHTML = `<div class="notice notice-error">课程加载失败：${escapeHtml(err.message)}</div>`;
      toast(err.message, "error");
    });
}

// 删除是不可恢复的——数据库记录和本地讲义文件一起没。
// 所以**先在确认框里把后果写具体**，而不是删完再告诉用户删了什么：
// "3 章 27 个知识点" 比一句"确定删除吗"有用得多。
// 用事件委托：卡片每次渲染都是新的，逐个绑监听会在重渲染后失效。
coursesBox.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-delete]");
  if (!button) return;

  const course = lastCourses.find((c) => String(c.id) === button.dataset.delete);
  if (!course) return;

  const agreed = confirm(
    `删除课程「${course.title}」？\n\n` +
      `会一并删掉：\n` +
      `· ${course.chapters} 章、${course.total_points} 个知识点的学习记录\n` +
      `· 这门课的全部对话记录\n` +
      `· 本地讲义目录（notes/${course.title}/）\n\n` +
      `删除后无法恢复。`,
  );
  if (!agreed) return;

  button.disabled = true;
  try {
    const result = await del(`/api/courses/${course.id}`);
    // 讲义目录没删掉（比如文件被占用）必须如实说，不能笼统报"删除成功"
    if (result.notes_error) {
      toast(`已删除「${result.title}」，但讲义目录没能清理：${result.notes_error}`, "warn");
    } else {
      toast(`已删除「${result.title}」`, "ok");
    }
    reload();
  } catch (err) {
    button.disabled = false;
    toast(err.message, "error");
  }
});

renderDate();
loadTodos();
loadNote();
reload();

/* --------------------------------------------------------- 首次使用提示 */

/** 没配密钥时提示一次；用户点"知道了"之后就不再提（记在本地）。 */
async function maybeShowFirstRun() {
  const banner = document.getElementById("first-run-banner");
  if (!banner) return;
  if (localStorage.getItem("crew.firstRunDismissed") === "1") return;

  try {
    const state = await get("/api/startup");
    if (!state.first_run) return;
    banner.hidden = false;
    document.getElementById("first-run-dismiss")?.addEventListener("click", () => {
      banner.hidden = true;
      localStorage.setItem("crew.firstRunDismissed", "1");
    });
  } catch {
    /* 启动状态拿不到就不提示：首页本身要能用 */
  }
}

maybeShowFirstRun();
