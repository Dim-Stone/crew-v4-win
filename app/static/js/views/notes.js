// 讲义页：左边列出工作区里的讲义，点一份在右边读。
//
// **只读**——这一页不提供删除、重命名、编辑。那些动作在文件系统里做更合适
// （用户本来就知道自己的文件在哪），在应用里再做一套，就会出现"两处都能改、
// 规则还不一样"的问题。这里只回答两件事：我攒了哪些讲义、某一份写了什么。
//
// 渲染复用 `md.js`：讲义里带公式与图解，用同一个渲染器才能保证它在这里
// 和课程页长得一样（那正是"公式能排出来"这件事的验收标准）。

import { get, query } from "../api.js";
import { escapeHtml, renderMarkdown } from "../md.js";

const listEl = document.getElementById("notes-list");
const viewEl = document.getElementById("notes-view");
const totalEl = document.getElementById("notes-total");

/**
 * "进来就打开这一份"用的查询参数：`/notes?path=高等数学/第05章-….md`。
 *
 * 它让一份讲义**可以被链接**（收藏、或者从别处跳进来），而不是只能靠手动点。
 * 注意它和 `/api/notes/content` 的 `path` 是同一个东西：相对工作区的路径，
 * 接口那边会自己挡住越界（见 `app/api/notes.py`）——前端不做第二套校验，
 * 免得多一份会走样的规则。
 */
const OPEN_PARAM = "path";

/** 现在选中的那份（相对工作区的路径），重渲染后要能把选中态还原。 */
let selected = null;

function showInView(html) {
  viewEl.innerHTML = html;
}

function faintParagraph(message) {
  const p = document.createElement("p");
  p.className = "faint";
  p.textContent = message;
  return p;
}

function renderList(payload) {
  totalEl.textContent = payload.total ? `${payload.total} 份` : "还没有";

  listEl.innerHTML = "";
  if (!payload.total) {
    listEl.appendChild(faintParagraph(
      payload.exists
        ? "工作区里还没有讲义——讲一节课就会自动写进来。"
        : `工作区还没有建起来：${payload.root}`,
    ));
    return;
  }

  for (const group of payload.courses) {
    const head = document.createElement("h2");
    head.className = "notes-course";
    head.textContent = group.course;
    listEl.appendChild(head);

    const ul = document.createElement("ul");
    ul.className = "notes-files";
    for (const file of group.files) {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "notes-file";
      button.dataset.path = file.path;   // 选中态靠它认，不靠"哪个按钮被点过"
      if (file.path === selected) button.classList.add("is-selected");
      button.innerHTML =
        `<span class="notes-file-name">${escapeHtml(file.name)}</span>`
        + `<span class="notes-file-meta">${escapeHtml(file.size_text)}`
        + ` · ${escapeHtml(file.updated_at)}</span>`;
      button.addEventListener("click", () => openNote(file.path));
      li.appendChild(button);
      ul.appendChild(li);
    }
    listEl.appendChild(ul);
  }
}

async function openNote(rel) {
  selected = rel;
  // 选中态记的是**路径**，不是"哪个按钮被点过"：这样列表重新渲染之后
  // （比如窗口缩放触发的重排）还能把它认出来。
  for (const el of listEl.querySelectorAll(".notes-file")) {
    el.classList.toggle("is-selected", el.dataset.path === rel);
  }

  showInView('<p class="faint">读取中…</p>');
  try {
    const note = await get(`/api/notes/content?path=${encodeURIComponent(rel)}`);
    showInView(
      `<div class="notes-head">`
      + `<span class="notes-title">${escapeHtml(note.name)}</span>`
      + `<span class="faint">${escapeHtml(note.path)}</span>`
      + `</div>`
      + `<article class="bubble-body notes-body">${renderMarkdown(note.text)}</article>`,
    );
  } catch (err) {
    showInView("");
    viewEl.appendChild(faintParagraph(err.message));
  }
}

async function boot() {
  try {
    renderList(await get("/api/notes"));
  } catch (err) {
    totalEl.textContent = "读取失败";
    listEl.innerHTML = "";
    listEl.appendChild(faintParagraph(err.message));
    return;
  }

  // 带 `?path=` 进来就直接打开那一份（链接/书签用）。路径不对时后端会拒，
  // 这里只是把它的错误照原样显示在右边——不额外判断"这份存不存在"。
  const wanted = query(OPEN_PARAM);
  if (wanted) await openNote(wanted);
}

boot();
