// 设置页：环境配置（怎么连模型）+ 关于。
//
// 这一页是**本机窗口版**补上的。以前的 Qt 版有"环境配置"页，换成网页界面之后
// 只剩工作台/新建/课程三个页面，用户就再也没有地方填密钥了——所以它必须存在，
// 而不是"以后再说"。
//
// 有一条规则贯穿整页，改之前先读懂：**密钥的明文只朝一个方向走（用户 → 服务器）**。
// 服务器从不把明文发回来（GET 只给掩码），所以：
//   · 输入框里回填的是掩码；
//   · 用户不动它就直接保存 → 交上去的是掩码 → 服务器理解成"这次不动密钥"；
//   · 用户真要清掉它 → 那是"清除"按钮，走 DELETE，不是把输入框清空再保存。
// 这条规则让"只想改模型名"变成一个不可能弄丢密钥的操作。

import { del, get, post, put, toast } from "../api.js";
import { renderMarkdown, escapeHtml } from "../md.js";
import { apply as applyPreferences } from "../prefs.js";

const form = document.getElementById("llm-form");
const keyInput = document.getElementById("api-key");
const keyReveal = document.getElementById("key-reveal");
const keyClear = document.getElementById("key-clear");
const baseUrlInput = document.getElementById("base-url");
const modelInput = document.getElementById("model");
const modelList = document.getElementById("model-list");
const modelHint = document.getElementById("model-hint");
const temperatureInput = document.getElementById("temperature");
const saveBtn = document.getElementById("save-btn");
const saveNote = document.getElementById("save-note");
const keyStatus = document.getElementById("key-status");
const keyStatusNote = document.getElementById("key-status-note");
const envFileEl = document.getElementById("env-file");
const firstRunEl = document.getElementById("first-run");
const testBtn = document.getElementById("test-btn");
const testNote = document.getElementById("test-note");

/** 服务器回的上一份配置。用来判断"用户到底改没改密钥"。 */
let loaded = null;

/* ------------------------------------------------------------------ 读 */

function fill(data) {
  loaded = data;

  keyInput.value = data.api_key || "";          // 掩码；从未配置时是空串
  baseUrlInput.value = data.base_url || "";
  modelInput.value = data.model || "";
  temperatureInput.value = data.temperature ?? "";

  // 候选模型名填进 datalist：它是**建议**不是白名单（接口允许手输别的），
  // 所以用 datalist 而不是 <select>——后者的语义是"只能选这些"。
  // 同时用文字写一遍，因为 datalist 的下拉在部分 WebView 内核里不弹。
  const suggestions = data.suggested_models || [];
  modelList.innerHTML = "";
  for (const name of suggestions) {
    const option = document.createElement("option");
    option.value = name;
    modelList.appendChild(option);
  }
  if (modelHint) {
    modelHint.textContent = suggestions.length
      ? `可以手输别的名字。这个端点上实测可用的有：${suggestions.join(" / ")}`
      : "可以手输别的名字。";
  }

  // 输入框与"换一个"按钮回到初始状态（保存完、或者用户点了取消都要回）
  keyInput.placeholder = "sk-…";
  if (keyReveal) keyReveal.textContent = "换一个";

  if (data.api_key_set) {
    keyStatus.textContent = "密钥已配置";
    keyStatus.className = "badge badge-ok";
    keyStatusNote.textContent = `写入的键名：${data.api_key_field}`;
  } else {
    keyStatus.textContent = "还没配置密钥";
    keyStatus.className = "badge badge-warn";
    keyStatusNote.textContent = "填好密钥并保存之后，新建课程才能用";
  }

  // 没配密钥时把"第一次使用"那段展开：这是新用户唯一需要的三句话
  if (firstRunEl && data.api_key_set) firstRunEl.hidden = true;

  if (envFileEl && data.env_file) {
    envFileEl.textContent = data.env_file;
    envFileEl.title = data.env_exists
      ? "这个文件已经存在，保存时会就地修改（手写的注释会保留）"
      : "这个文件还不存在，保存时会新建";
  }
}

async function showFirstRunIfNeeded() {
  if (!firstRunEl || firstRunEl.hidden === false) return;
  try {
    const state = await get("/api/startup");
    if (state.first_run) firstRunEl.hidden = false;
  } catch {
    /* 拿不到启动状态就不提示，别把页面卡住 */
  }
}

async function load() {
  try {
    fill(await get("/api/settings/llm"));
  } catch (err) {
    keyStatus.textContent = "读取失败";
    keyStatus.className = "badge badge-err";
    keyStatusNote.textContent = err.message;
    saveBtn.disabled = true;
  }
}

/**
 * 保存并测试：把"保存成功"和"密钥真的能用"分开验证。
 *
 * 只保存的话，用户唯一的反馈是"已保存"——而那只说明写进了文件。
 * 等一节课卡住再回来查，成本高得多。
 */
async function saveAndTest() {
  if (!testBtn) return;
  testBtn.disabled = true;
  if (testNote) {
    testNote.hidden = false;
    testNote.className = "hint";
    testNote.textContent = "保存中…";
  }

  const body = {
    api_key: keyInput.value,
    base_url: baseUrlInput.value,
    model: modelInput.value,
    temperature: temperatureInput.value,
  };

  try {
    fill(await put("/api/settings/llm", body));
    if (testNote) testNote.textContent = "正在连接模型端点…";

    const result = await post("/api/settings/llm/test", {
      api_key: keyInput.value,
      base_url: baseUrlInput.value,
    });

    if (testNote) {
      testNote.className = "hint hint-ok";
      const names = (result.models || []).slice(0, 3).join(" / ");
      testNote.textContent = result.model_found === false
        ? `密钥可用，但端点没列出「${result.model}」——检查一下模型名`
        : `连接成功，密钥可用${names ? `（可用模型：${names}…）` : ""}`;
    }
    if (firstRunEl) firstRunEl.hidden = true;
    toast("连接成功", "ok");
    refreshEnvBadge();
  } catch (err) {
    if (testNote) {
      testNote.className = "hint hint-err";
      testNote.textContent = err.message;
    }
    toast(err.message, "error");
  } finally {
    testBtn.disabled = false;
  }
}

/* ------------------------------------------------------------------ 写 */

async function save(event) {
  event.preventDefault();
  saveBtn.disabled = true;
  saveNote.textContent = "保存中…";

  // 密钥的处理是这一页的核心，只有三种情况：
  //   ① 输入框里还是**掩码**（用户没动）→ 原样交回去，服务器理解成"不改"；
  //   ② 输入框是**空的**且本来就没配密钥 → 也交空串，服务器一样理解成"不改"
  //      （它不把空串当删除，见 app/api/settings.py 里的说明）；
  //   ③ 用户敲了新东西 → 交新值。
  // 所以这里**不需要**任何"清空"分支——清空是另一个按钮。
  const body = {
    api_key: keyInput.value,
    base_url: baseUrlInput.value,
    model: modelInput.value,
    temperature: temperatureInput.value,
  };

  try {
    // 用服务器回读的那份回填，而不是"我以为保存进去的那份"：
    // 端点地址与模型名的默认值是在服务端补齐的，界面要显示**真正在用的值**。
    fill(await put("/api/settings/llm", body));
    saveNote.textContent = "已保存";
    toast("已保存，立刻生效", "ok");
    // 顶栏那个"未配置密钥"徽标得跟着变，否则用户会以为没保存上
    refreshEnvBadge();
  } catch (err) {
    saveNote.textContent = "";
    toast(err.message, "error");
  } finally {
    saveBtn.disabled = false;
    setTimeout(() => {
      if (saveNote.textContent === "已保存") saveNote.textContent = "";
    }, 3000);
  }
}

async function clearKey() {
  // 这一步是不可撤销的（值不留在任何地方），所以必须问一次。
  if (!window.confirm("清除这台机器上的 API 密钥？\n\n清除之后就不能新建课程或讲课了，直到重新填入。")) {
    return;
  }
  keyClear.disabled = true;
  try {
    fill(await del("/api/settings/llm/key"));
    toast("密钥已清除", "ok");
    refreshEnvBadge();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    keyClear.disabled = false;
  }
}

/* --------------------------------------------------------------- 杂项 */

/** 顶栏徽标由 theme.js 负责拉取；这里只负责"让它重新拉一次"。 */
function refreshEnvBadge() {
  const badge = document.getElementById("env");
  if (!badge) return;
  // theme.js 的更新逻辑挂在页面加载时那一次 fetch 上，没有对外暴露函数。
  // 与其把 theme.js 改成模块（它必须在首屏之前跑，见 base.html），
  // 不如在这里直接把同一件事做一遍——就三行。
  fetch("/api/health", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then((res) => {
      if (!res || !res.ok || !res.data) return;
      badge.textContent = res.data.llm_ready ? res.data.model : "未配置密钥";
      badge.className = res.data.llm_ready ? "badge badge-ok" : "badge badge-warn";
    })
    .catch(() => {
      /* 顶栏只是提示，失败就不动它 */
    });
}

async function loadAbout() {
  const versionEl = document.getElementById("about-version");
  try {
    const info = await get("/api/settings/about");
    versionEl.textContent = info.version;
    renderLocations(document.getElementById("locations"), info);
  } catch {
    versionEl.textContent = "读取失败";
  }
}

/** "文件位置"那一块：程序 / 数据 / 讲义 / 数据库 / 日志。 */
function renderLocations(el, info) {
  if (!el) return;
  // 便携用法下数据目录与程序目录是同一个，那时**明说**"同一个目录"：
  // 同一串路径出现两遍，会让人以为哪里配错了。
  const sameAsProject = info.data_dir === info.project_dir;
  const rows = [
    ["程序", info.project_dir],
    ["数据", sameAsProject ? `${info.data_dir}（与程序同一个目录）` : info.data_dir],
    ["讲义", info.lecture_dir],
    ["数据库", info.database],
    ["日志", info.log_file],
  ];
  el.innerHTML = rows
    .map(([label, value]) =>
      `<div class="fact"><dt>${escapeHtml(label)}</dt>`
      + `<dd>${escapeHtml(String(value ?? ""))}</dd></div>`)
    .join("");
}

/* --------------------------------------------------------------- 绑定 */

if (keyReveal) {
  // 这个按钮叫"换一个"，它做的也是"换一个"——**不是"显示密钥"**。
  // 服务器从不回发明文（GET 只给掩码），所以"显示"本身没有东西可显示：
  // 把掩码从 password 变成 text 依然只看到 •••。真正有用的动作是
  // "把输入框清空，我直接粘一个新的"，所以按钮就做这件事。
  keyReveal.addEventListener("click", () => {
    const isMasked = loaded && loaded.api_key_set && keyInput.value === loaded.api_key;

    if (isMasked) {
      keyInput.value = "";
      keyInput.placeholder = "在这里粘贴新的密钥";
      keyInput.focus();
      keyReveal.textContent = "取消";
      saveNote.textContent = "粘贴新密钥后点保存";
      return;
    }
    // 再点一次 = 放弃这次修改，把掩码填回去
    fill(loaded);
    keyInput.placeholder = "sk-…";
    keyReveal.textContent = "换一个";
    saveNote.textContent = "";
  });
}

if (keyClear) keyClear.addEventListener("click", clearKey);
if (form) form.addEventListener("submit", save);

testBtn?.addEventListener("click", saveAndTest);

load();
showFirstRunIfNeeded();
loadAbout();

/* ------------------------------------------------------- 界面偏好（第二块） */

// 这一块与上面那块的分工：上面是"怎么连模型"（改错了功能不可用），
// 这里是"怎么看界面"（改错了只是难看）。所以两者的失败处理不同——
// 这里读不到就静默用默认值，绝不弹红条：字号没生效是小事。

const prefsForm = document.getElementById("prefs-form");
const prefsSave = document.getElementById("prefs-save");
const prefsReset = document.getElementById("prefs-reset");
const prefsNote = document.getElementById("prefs-note");
const prefsLegacy = document.getElementById("prefs-legacy");

/** 每个档位组渲染在哪个容器里。键名与接口返回的 `choices` 一致。 */
const PREF_GROUPS = ["font_size", "content_width", "line_height", "density", "reduce_motion"];

/** 上一次从服务器读到的完整偏好视图。用来判断"改没改"和"回填"。 */
let prefsView = null;

function renderPrefGroup(key, options, selected) {
  const box = document.getElementById(`prefs-${key}`);
  if (!box) return;
  box.innerHTML = "";

  for (const option of options) {
    // 用 radio + label 而不是下拉框：这几个档位都只有 3–4 个，
    // 平铺出来能一眼看到全部选项和它们的差别；下拉框要把它们藏起来。
    const id = `pref-${key}-${String(option.value)}`;
    const label = document.createElement("label");
    label.className = "choice";
    label.htmlFor = id;

    const input = document.createElement("input");
    input.type = "radio";
    input.name = `pref-${key}`;
    input.id = id;
    // value 一律写成字符串：`data-*` 与 DOM 属性都是字符串，
    // 数字/布尔的还原靠下面的 `readPrefValue` 按 key 处理。
    input.value = String(option.value);
    input.checked = String(option.value) === String(selected);
    label.appendChild(input);

    const body = document.createElement("span");
    body.className = "choice-body";
    body.innerHTML = `<strong>${escapeHtml(option.label)}</strong>`
      + `<span class="choice-note">${escapeHtml(option.hint)}</span>`;
    label.appendChild(body);

    box.appendChild(label);
  }
}

function renderPrefs(view) {
  prefsView = view;
  for (const key of PREF_GROUPS) {
    renderPrefGroup(key, view.choices[key] || [], view.values[key]);
  }

  if (prefsLegacy) {
    const legacy = view.legacy_keys || [];
    if (legacy.length) {
      // 旧版（Qt 界面）的键不再生效。**必须说出来**——否则用户会以为
      // "我的设置被吃了"，而其实那几项在这个界面里根本不存在。
      prefsLegacy.textContent =
        `这个文件里还有 ${legacy.length} 项旧版（Qt 界面）的设置，它们已经不再生效：`
        + legacy.join("、")
        + "。它们不会影响现在的界面，下次保存时也不会被改动。";
      prefsLegacy.classList.remove("hidden");
    } else {
      prefsLegacy.classList.add("hidden");
    }
  }
}

/** 从表单里读出五个值，还原成正确的类型。 */
function readPrefs() {
  const values = {};
  for (const key of PREF_GROUPS) {
    const checked = prefsForm.querySelector(`input[name="pref-${key}"]:checked`);
    const raw = checked ? checked.value : null;

    if (key === "reduce_motion") {
      // 这里**只有 true / false 两个可能**，所以直接比字符串。
      // 不用 `Boolean(raw)`——那会把字符串 "false" 变成 true（经典坑）。
      values[key] = raw === "true";
    } else if (key === "density") {
      values[key] = raw;
    } else if (key === "line_height") {
      values[key] = raw === null ? null : parseFloat(raw);
    } else {
      values[key] = raw === null ? null : parseInt(raw, 10);
    }
  }
  return values;
}

async function loadPrefs() {
  try {
    renderPrefs(await get("/api/preferences"));
  } catch {
    if (prefsNote) prefsNote.textContent = "读取失败，这次用默认外观";
  }
}

async function savePrefs(event) {
  event.preventDefault();
  prefsSave.disabled = true;
  prefsNote.textContent = "保存中…";
  try {
    const view = await put("/api/preferences", readPrefs());
    renderPrefs(view);
    // 立刻生效：把新值写给 CSS 变量，不用刷新页面。
    // 走 prefs.js 的 apply 而不是这里自己设——**值的解释只有一处**，
    // 两处各写一遍的话，"紧凑密度乘 0.8"这种规则迟早会分叉。
    applyPreferences(view.values);
    prefsNote.textContent = "已保存";
    toast("界面已更新", "ok");
  } catch (err) {
    prefsNote.textContent = "";
    toast(err.message, "error");
  } finally {
    prefsSave.disabled = false;
    setTimeout(() => {
      if (prefsNote.textContent === "已保存") prefsNote.textContent = "";
    }, 3000);
  }
}

async function resetPrefs() {
  if (!window.confirm("把界面设置恢复成默认？\n\n字号、宽度、间距、动效都会回到刚装好的样子。")) {
    return;
  }
  prefsReset.disabled = true;
  try {
    const view = await post("/api/preferences/reset", {});
    renderPrefs(view);
    applyPreferences(view.values);
    toast("已恢复默认", "ok");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    prefsReset.disabled = false;
  }
}

if (prefsForm) prefsForm.addEventListener("submit", savePrefs);
if (prefsReset) prefsReset.addEventListener("click", resetPrefs);
loadPrefs();

/* ------------------------------------------------------------ 我的画像（第三块） */

// 只读。编辑入口是新建课程向导（那一页本来就要问这些问题），
// 所以这里不重复做一个编辑器——同一个东西有两个编辑入口，
// 迟早会出现"我到底在哪一页改的那个值"说不清的情况。

async function loadProfile() {
  const box = document.getElementById("profile-body");
  if (!box) return;
  try {
    const profile = await get("/api/profile");
    if (!profile.is_filled) {
      box.className = "faint";
      box.textContent = "还没有画像——新建一门课，先答几个问题，这里就会有了。";
      return;
    }

    const rows = [
      ["基础", profile.ability_level],
      ["目的", profile.purpose],
      ["兴趣", profile.interests],
      ["讲法偏好", profile.style_preference],
      ["时间预算", profile.time_budget],
    ].filter(([, value]) => value && String(value).trim());

    // summary_md 是 Markdown（模型写的）。用同一个渲染器保证与讲义一致：
    // 在这里自己拼字符串的话，公式与列表会长得跟别处不一样。
    box.className = "";
    box.innerHTML =
      `<dl class="fact-list">${rows.map(([label, value]) =>
        `<div class="fact"><dt>${escapeHtml(label)}</dt>`
        + `<dd>${escapeHtml(String(value))}</dd></div>`).join("")}</dl>`
      + (profile.summary_md
        ? `<div class="profile-summary bubble-body">${renderMarkdown(profile.summary_md)}</div>`
        : "");
  } catch {
    box.className = "faint";
    box.textContent = "画像读取失败";
  }
}

loadProfile();
