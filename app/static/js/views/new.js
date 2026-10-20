// 新建课程：方向 → 内容来源 → 摸底 → 画像 → 大纲 → 进入课程。
//
// 原来这三件事分散在 index / intake / outline 三个页面（点一次跳一次），
// 现在合成一页内的五步：不再来回跳转，中途也不会因为刷新丢掉进度。
//
// 右侧那块看板（.intake-aside）回答的是另一个问题："**这门课现在长什么样**"。
// 它只摆已经定下来的事（方向、内容来源、摸底答了几项、画像、大纲规模），
// 没填的项如实说"还没填"——不预填、不猜，也不放"实时生成画像"这种动画：
// 画像要等摸底提交后由模型解析出来（见 /api/intake/answers）。

import { get, post, query, toast } from "../api.js";
import { subscribe } from "../sse.js";
import { escapeHtml, renderMarkdown } from "../md.js";

const steps = {
  direction: document.getElementById("step-direction"),
  source: document.getElementById("step-source"),
  intake: document.getElementById("step-intake"),
  profile: document.getElementById("step-profile"),
  outline: document.getElementById("step-outline"),
};

const stepsEl = document.getElementById("steps");
const asideRowsEl = document.getElementById("aside-rows");
const asideAdaptationEl = document.getElementById("aside-adaptation");

const DIFFICULTY = {
  easy: "简单",
  medium: "中等",
  hard: "偏难",
};

/** 能力水平的说法：界面上要用同一套措辞（"有一定基础"而不是"intermediate"）。 */
const ABILITY_TEXT = {
  beginner: "初学",
  intermediate: "有一定基础",
  advanced: "较熟练",
  unknown: "还没判断",
};

/** 内容来源的三种说法，与上面三个选项卡一一对应。 */
const MODE_TEXT = {
  "": "没有资料，由系统安排",
  strict: "严格按资料的顺序讲",
  reference: "资料作参考，缺的地方我补",
};

const STEP_ORDER = ["direction", "source", "intake", "profile", "outline"];

let courseId = query("course") ? Number(query("course")) : null;

// 最近一次拿到的画像（摸底解析完成后，或"继续完善"时读回来的）。
// 右侧看板与"画像"那一步共用它，避免两处各查一次。
let profileData = null;
// 大纲生成后的规模（章 / 知识点 / 预计总时长），由 renderOutline 填。
let outlineSummary = null;

function show(step) {
  for (const [name, el] of Object.entries(steps)) {
    el.classList.toggle("hidden", name !== step);
  }
  renderSteps(step);
  renderAside();
  window.scrollTo({ top: 0 });
}

/* ------------------------------------------------------- 步骤指示器 */

/** 高亮当前一步，并把走过的步标出来。 */
function renderSteps(current) {
  const currentIndex = STEP_ORDER.indexOf(current);
  for (const item of stepsEl.children) {
    const index = STEP_ORDER.indexOf(item.dataset.step);
    item.classList.toggle("is-current", index === currentIndex);
    item.classList.toggle("is-done", index < currentIndex);
  }
}

/* ------------------------------------------------------- 右侧看板 */

/** 一行：左边是什么、右边是内容。空值**如实说"还没填"**，
 *  留白会让人以为是界面没加载出来。 */
function asideRow(label, value) {
  const filled = Boolean(value);
  return `<div class="aside-row">
    <span class="aside-label">${escapeHtml(label)}</span>
    <span class="aside-value${filled ? "" : " faint"}">${escapeHtml(filled ? value : "还没填")}</span>
  </div>`;
}

/** 摸底问卷答了几项（问卷是动态生成的，所以每次都现数）。 */
function answeredCount() {
  let answered = 0;
  document.querySelectorAll("#intake-form textarea[data-key]").forEach((el) => {
    if (el.value.trim()) answered += 1;
  });
  return answered;
}

function renderAside() {
  const rows = [];
  const direction = document.getElementById("direction").value.trim();
  const focus = document.getElementById("focus").value.trim();
  const mode = selectedMode();
  const path = materialPathEl().value.trim();
  const questionTotal = document.querySelectorAll("#intake-form textarea[data-key]").length;

  rows.push(asideRow("学习方向", direction));
  if (focus) rows.push(asideRow("这次的重点", focus));
  rows.push(asideRow("内容来源", MODE_TEXT[mode]));
  if (mode) rows.push(asideRow("资料文件夹", path));
  if (questionTotal) rows.push(asideRow("摸底", `已答 ${answeredCount()} / ${questionTotal} 项`));

  if (profileData && profileData.is_filled) {
    rows.push(asideRow(
      "现在的基础",
      ABILITY_TEXT[profileData.ability_level] || profileData.ability_level,
    ));
    if (profileData.purpose) rows.push(asideRow("学它的目的", profileData.purpose));
    if (profileData.style_preference) rows.push(asideRow("习惯的讲法", profileData.style_preference));
  }

  if (outlineSummary) {
    rows.push(asideRow(
      "大纲",
      `${outlineSummary.chapters} 章 · ${outlineSummary.points} 个知识点 · 约 ${outlineSummary.minutes} 分钟`,
    ));
  }

  asideRowsEl.innerHTML = rows.join("");
  // "系统正在怎么适应你"那一句：空串就让它空着（CSS 的 :empty 会隐藏）
  asideAdaptationEl.textContent = (profileData && profileData.adaptation) || "";
}

// 看板要"填什么就显示什么"。监听丢在 document 上：
// 摸底问卷的字段是拿到题目之后才生成的，逐个绑监听会在重建表单时失效。
for (const type of ["input", "change"]) {
  document.addEventListener(type, renderAside);
}

/* ------------------------------------------------------------ 1 方向 */

/* 第 1 步只收方向，把"建课程"推迟到第 2 步：
   "用不用资料"会写进课程记录，得等用户选完再建，
   否则中途改主意就会留下一条设置不对的课程。 */
document.getElementById("to-source").addEventListener("click", () => {
  if (!document.getElementById("direction").value.trim()) {
    toast("请先填写学习方向", "error");
    return;
  }
  show("source");
});

/* ------------------------------------------------------------ 2 内容来源 */

function selectedMode() {
  const checked = document.querySelector('input[name="material_mode"]:checked');
  return checked ? checked.value : "";
}

function materialPathEl() {
  return document.getElementById("material-path");
}

/** 只在选了"有资料"时才展开路径输入——没资料的人不该看到这一块。 */
function syncMaterialPicker() {
  document.getElementById("material-picker").classList.toggle("hidden", !selectedMode());
}

document.querySelectorAll('input[name="material_mode"]').forEach((radio) => {
  radio.addEventListener("change", syncMaterialPicker);
});

/** 回填已有课程的设置（从工作台"继续完善"进来时用）。 */
function applyMaterialMode(mode, path) {
  const target = mode || "";
  const radio = document.querySelector(`input[name="material_mode"][value="${target}"]`);
  if (radio) radio.checked = true;
  materialPathEl().value = path || "";
  syncMaterialPicker();
}

document.getElementById("preview-materials").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const box = document.getElementById("material-preview");
  const path = materialPathEl().value.trim();
  if (!path) {
    toast("请先填资料文件夹", "error");
    return;
  }

  button.disabled = true;
  button.textContent = "正在读…";
  box.innerHTML = "";
  try {
    box.innerHTML = renderPreview(await post("/api/materials/preview", { path }));
  } catch (err) {
    box.innerHTML = `<div class="notice notice-error">${escapeHtml(err.message)}</div>`;
  } finally {
    button.disabled = false;
    button.textContent = "先看看能读到什么";
  }
});

/** 把解析结果摆出来。用户在这一步最该知道的是"到底读到了什么"——
 *  一份扫描件、一个放错的文件夹，都会让后面的编排建在空地上。 */
function renderPreview(data) {
  const stats = data.stats;
  const items = data.files.map((file) => {
    let note;
    if (file.error) {
      note = `读不了：${escapeHtml(file.error)}`;
    } else if (!file.blocks) {
      note = "没有取到文字（可能是扫描件）";
    } else {
      note = `${file.blocks} 段`;
      if (file.scanned_pages) note += `，另有 ${file.scanned_pages} 处没文字`;
      if (file.truncated) note += "，内容较长只读了开头";
    }
    return `<li><span>${escapeHtml(file.name)}</span><span class="faint">${note}</span></li>`;
  }).join("");

  const parts = [`读到 ${stats.parsed} 份`];
  if (stats.failed) parts.push(`${stats.failed} 份读取失败`);
  if (stats.no_text) parts.push(`${stats.no_text} 份没有文字`);
  if (data.truncated_list) parts.push("文件很多，这里只看前 300 份");

  /* 读数不对的文件要分成两类，因为**下一步该做的事完全不同**：
       打不开     → 文件本身有问题，让用户检查或重新导出；
       没取到文字 → 多半是扫描件/图片，给一条**能照着走**的路。 */
  const broken = data.files.filter((file) => file.error);
  const noText = data.files.filter((file) => !file.error && !file.blocks);
  let advice = "";

  if (noText.length) {
    advice += `
      <div class="notice notice-warn">
        <strong>这 ${noText.length} 份没取到文字</strong>（多半是扫描件或整页图片）：
        ${noText.map((file) => escapeHtml(file.name)).join("、")}
        <br><br>
        建议先去 <strong>DeepSeek 官网或其它能读图的 AI 平台</strong>，
        把这些图片整理成 Word 或 Markdown 文档，再放回这个文件夹。
        那边可以直接读图、你还能核对改错；这里不支持图片，硬来也认得不准。
      </div>`;
  }
  if (broken.length) {
    advice += `
      <div class="notice notice-warn">
        这 ${broken.length} 份打不开，可能文件损坏或格式不对：
        ${broken.map((file) => escapeHtml(file.name)).join("、")}
      </div>`;
  }

  return `<div class="notice notice-ok">${parts.join("，")}。</div>
          <ul class="point-list material-files">${items}</ul>
          ${advice}`;
}

// 建课程放在这一步：到这里方向与资料模式都定了
document.getElementById("to-intake").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const direction = document.getElementById("direction").value.trim();
  if (!direction) {
    toast("请先填写学习方向", "error");
    show("direction");
    return;
  }

  const mode = selectedMode();
  const path = materialPathEl().value.trim();
  if (mode && !path) {
    toast("选了资料模式，请填一下资料文件夹", "error");
    return;
  }

  button.disabled = true;
  try {
    const course = await post("/api/courses", {
      direction,
      focus: document.getElementById("focus").value.trim(),
      material_mode: mode,
      material_path: mode ? path : "",
    });
    courseId = course.id;
    await loadQuestions();
    show("intake");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    button.disabled = false;
  }
});

/* ------------------------------------------------------------ 2 摸底 */

async function loadQuestions() {
  const questions = await get("/api/intake/questions");
  const form = document.getElementById("intake-form");
  form.innerHTML = "";

  for (const item of questions) {
    const field = document.createElement("div");
    field.className = "field";

    const label = document.createElement("label");
    label.setAttribute("for", `q-${item.key}`);
    label.textContent = item.label;

    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = item.question;

    const textarea = document.createElement("textarea");
    textarea.id = `q-${item.key}`;
    textarea.dataset.key = item.key;
    textarea.rows = 2;

    field.append(label, hint, textarea);
    form.appendChild(field);
  }

  // 题目数量定下来了，看板里"已答 n / N 项"才有分母
  renderAside();
}

document.getElementById("submit-intake").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const answers = {};
  document.querySelectorAll("#intake-form textarea[data-key]").forEach((el) => {
    const value = el.value.trim();
    if (value) answers[el.dataset.key] = value;
  });

  if (!Object.keys(answers).length) {
    toast("至少回答一项，画像才有依据", "error");
    return;
  }

  button.disabled = true;
  button.textContent = "整理中…";

  try {
    const { turn_id: turnId } = await post("/api/intake/answers", {
      course_id: courseId,
      answers,
    });
    subscribe(turnId, {
      onProfile: (data) => {
        profileData = data;
        renderProfile(data);
        renderAside();
      },
      onDone: () => {
        button.disabled = false;
        button.textContent = "重新提交";
        show("profile");
      },
      onError: (data) => {
        toast(data.message || "画像解析失败", "error");
        button.disabled = false;
        button.textContent = "提交";
      },
    });
  } catch (err) {
    toast(err.message, "error");
    button.disabled = false;
    button.textContent = "提交";
  }
});

/* ------------------------------------------------------------ 3 画像 */

function renderProfile(profile) {
  const level = {
    beginner: "初学",
    intermediate: "有一定基础",
    advanced: "较熟练",
    unknown: "未判断",
  }[profile.ability_level] || profile.ability_level;

  document.getElementById("profile-box").innerHTML = `
    <div class="row-end" style="justify-content:flex-start">
      <span class="badge badge-accent">${escapeHtml(level)}</span>
      ${profile.purpose ? `<span class="badge">目的：${escapeHtml(profile.purpose)}</span>` : ""}
      ${profile.time_budget ? `<span class="badge">${escapeHtml(profile.time_budget)}</span>` : ""}
    </div>
    <div style="margin-top:12px">${renderMarkdown(profile.summary_md || "（没有生成摘要）")}</div>
    ${profile.style_preference
      ? `<p class="muted">讲法偏好：${escapeHtml(profile.style_preference)}</p>` : ""}
  `;
}

/* ------------------------------------------------------------ 4 大纲 */

let incomingOutline = null;

document.getElementById("generate-outline").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "规划中…";
  incomingOutline = null;

  try {
    const { turn_id: turnId } = await post(`/api/courses/${courseId}/outline`);
    subscribe(turnId, {
      onOutline: (data) => { if (data.outline) incomingOutline = data.outline; },
      onDone: () => {
        button.disabled = false;
        button.textContent = "重新生成";
        if (incomingOutline) {
          renderOutline(incomingOutline);
        } else {
          // 兜底：事件没带数据时才回查，且失败要能看见
          get(`/api/courses/${courseId}`)
            .then(renderOutline)
            .catch((err) => toast(`大纲已生成，但界面刷新失败：${err.message}`, "error"));
        }
        show("outline");
      },
      onError: (data) => {
        toast(data.message || "大纲生成失败", "error");
        button.disabled = false;
        button.textContent = "生成学习大纲";
      },
    });
  } catch (err) {
    toast(err.message, "error");
    button.disabled = false;
    button.textContent = "生成学习大纲";
  }
});

function renderOutline(course) {
  const box = document.getElementById("outline-box");
  const chapters = course.chapters || [];

  if (!chapters.length) {
    box.innerHTML = '<div class="empty">还没有大纲。</div>';
    return;
  }

  const total = chapters.reduce((sum, chapter) => sum + chapter.points.length, 0);
  box.innerHTML = `
    <p class="faint">${chapters.length} 章 · ${total} 个知识点</p>
  `;

  // 右侧看板也报一下规模：预计总时长把每个知识点的 est_minutes 加起来
  // （没有就是 0 分钟，如实显示，不估算）
  outlineSummary = {
    chapters: chapters.length,
    points: total,
    minutes: chapters.reduce(
      (sum, chapter) =>
        sum + chapter.points.reduce((inner, point) => inner + (Number(point.est_minutes) || 0), 0),
      0,
    ),
  };
  renderAside();

  chapters.forEach((chapter, index) => {
    const block = document.createElement("div");
    block.className = "section";
    block.innerHTML = `
      <h3>第 ${index + 1} 章　${escapeHtml(chapter.title)}</h3>
      ${chapter.summary ? `<p class="muted">${escapeHtml(chapter.summary)}</p>` : ""}
      <ul class="point-list" style="padding-left:0"></ul>
    `;

    const list = block.querySelector(".point-list");
    for (const point of chapter.points) {
      const item = document.createElement("li");
      item.innerHTML = `
        <span style="display:flex;gap:10px;align-items:baseline">
          <span style="flex:1">${escapeHtml(point.title)}
            ${point.summary ? `<div class="faint">${escapeHtml(point.summary)}</div>` : ""}
            ${sourcesHtml(point.sources, course.material_mode)}
          </span>
          <span class="badge">${DIFFICULTY[point.difficulty] || "中等"}</span>
          <span class="faint">约 ${point.est_minutes} 分钟</span>
        </span>`;
      list.appendChild(item);
    }
    box.appendChild(block);
  });

  document.getElementById("start-course").classList.remove("hidden");
}

document.getElementById("start-course").addEventListener("click", () => {
  location.href = `/course/${courseId}`;
});

/** 资料出处。
 *
 *  有出处 → 列出来，学生可以回原文核对；
 *  空数组 → 在**资料模式下这是有含义的**："这一节不在你资料里，是我补的"。
 *          没走资料模式的课程留空是正常的，不必标——满屏"无来源"只会变成噪音。
 */
function sourcesHtml(sources, mode) {
  const list = Array.isArray(sources) ? sources : [];
  const labels = list
    .map((item) => {
      if (typeof item === "string") return item; // 旧格式兜底
      const file = item && item.file ? String(item.file) : "";
      const where = item && item.locator ? String(item.locator) : "";
      return where ? `${file} ${where}`.trim() : file;
    })
    .filter(Boolean);

  if (labels.length) {
    return `<div class="faint">依据：${labels.map(escapeHtml).join("；")}</div>`;
  }
  if (mode) return '<div class="faint">这一节不在你的资料里，是我补充的</div>';
  return "";
}

/* ------------------------------------------------------------ 启动 */

if (courseId) {
  /* 从工作台"继续完善"进来：先把已有设置读回来。
     不这么做的话，重新生成大纲时资料模式就丢了——
     用户以为还在按资料讲，其实已经悄悄退回自由编排。 */
  get(`/api/courses/${courseId}`)
    .then((course) => {
      document.getElementById("direction").value = course.direction || "";
      document.getElementById("focus").value = course.focus || "";
      applyMaterialMode(course.material_mode, course.material_path);
      return routeExisting(course);
    })
    .catch((err) => toast(err.message, "error"));
} else {
  show("direction");
}

// 看板先铺一次：进来时"还没填"的项也要看得见，而不是等用户动一下才出现
renderAside();
renderSteps(currentVisibleStep());

/** 现在露在外面的是哪一步（只用于首屏那一瞬间的指示器高亮，
 *  之后每次切换都会由 show() 校正）。 */
function currentVisibleStep() {
  return STEP_ORDER.find((name) => !steps[name].classList.contains("hidden")) || "direction";
}

/** 已有的课程该落到哪一步。
 *
 *  **这里原来是硬编码跳到摸底问卷的**：用户点一次"继续完善"，就得把
 *  "基础/目的/兴趣/风格/节奏"重填一遍——看起来就像上次做的全白费了。
 *  实际上问卷答案早就存进画像里了，根本不需要再问。
 *
 *  现在按课程的真实状态分流：
 *    已就绪   → 直接进课程（已经没什么可"完善"的了）
 *    在规划   → 停在"生成大纲"那一步并显示进度，而不是让用户再点一次
 *    画像已填 → 跳过摸底，直接到最后一步
 *    其余     → 才走摸底
 */
async function routeExisting(course) {
  if (course.status === "ready") {
    location.href = `/course/${course.id}`;
    return;
  }

  const profile = await get("/api/profile").catch(() => null);
  if (!profile || !profile.is_filled) {
    await loadQuestions();
    show("intake");
    return;
  }

  profileData = profile; // 看板与"画像"那一步共用这一份，不各查一次
  renderProfile(profile);
  show("profile");
  watchOutline(course);
}

/** 已经在规划中时，别诱使（更别允许）用户再点一次生成。
 *
 *  进度从**数据库**读（`Turn.stage`），所以刷新页面、换个浏览器回来，
 *  看到的东西是一样的——这正是"离开页面也不丢"的实现方式。
 */
function watchOutline(course) {
  if (course.outline_state !== "running") return;

  const button = document.getElementById("generate-outline");
  const stageText = (stage) => (stage ? `正在规划：${stage}` : "正在规划…");

  button.disabled = true;
  button.textContent = stageText(course.outline_stage);

  const timer = setInterval(async () => {
    let fresh;
    try {
      fresh = await get(`/api/courses/${courseId}`);
    } catch {
      return; // 轮询失败不打断，下一次再来
    }

    if (fresh.outline_state === "running") {
      button.textContent = stageText(fresh.outline_stage);
      return;
    }

    clearInterval(timer);
    button.disabled = false;
    if (fresh.outline_state === "failed") {
      button.textContent = "重新生成大纲";
      toast("上次规划没成功，可以再试一次", "error");
      return;
    }
    button.textContent = "生成学习大纲";
    toast("大纲已经生成好了", "ok");
    renderOutline(fresh);
    show("outline");
  }, 4000);
}
