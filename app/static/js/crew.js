// 课程页顶部的"谁在动手"：把后端的阶段文字认成具体的智能体。
//
// 为什么是"认文字"而不是后端多推一个 `agent` 字段：
//   后端 status 事件里推的是**给人看的阶段文字**（"备课中""翻资料中""讲课中"
//   "画图中"，见 services/teaching_service.py），它同时是界面上那句提示的来源。
//   在界面这一层认这几个词，就不必为了显示而多改一份事件契约。
//
// 认不出来时返回 null，界面照旧什么都不高亮——**宁可安静，也不要编一个
// "某某智能体正在忙"出来**：观察者（observer）与备课教师的复盘其实发生在
// 这一轮讲完之后的后台线程里（teaching_service._schedule_after_turn），
// 前端根本收不到它们的实时状态，那些就只能不画。

/** 协作轨上的四个节点，顺序即显示顺序（讲解链路：备课 → 取资料 → 讲 → 画图）。 */
export const CREW = [
  {
    agent: "lesson-planner",
    name: "备课教师",
    hint: "先把这一节拆成几步，想好要点与预见的坑",
  },
  {
    agent: "material-reader",
    name: "资料读者",
    hint: "翻你给的资料，挑出这一轮该用的那几页",
  },
  {
    agent: "lecturer",
    name: "讲师",
    hint: "按教案讲，逐字推给你",
  },
  {
    agent: "visual",
    name: "图解师",
    hint: "把刚才的讲解画成图",
  },
];

// 顺序有讲究：更具体的说法排在前面。
// 例如"正在读取资料文件夹"只该命中资料读者，不该被"课"字带偏。
const STATUS_PATTERNS = [
  [/备课/, "lesson-planner"],
  [/资料/, "material-reader"],
  [/画图|图解/, "visual"],
  [/讲课/, "lecturer"],
];

/**
 * 阶段文字对应哪个智能体在动手。
 *
 * @param {string} status 后端 status 事件的 `status` 字段（人话阶段，如"备课中"）
 * @returns {string|null} 智能体标识（见 CREW）；认不出来时 null
 */
export function agentForStatus(status) {
  const text = String(status ?? "").trim();
  if (!text) return null;
  const hit = STATUS_PATTERNS.find(([pattern]) => pattern.test(text));
  return hit ? hit[1] : null;
}
