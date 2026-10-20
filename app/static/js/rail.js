// 进度尺「讲到过」列表的纯逻辑：只算"哪一段、标什么"，不碰 DOM。
//
// 为什么把它单独放一个文件：
//   这段算法是"定位准不准"的根——早先版本用**知识点名**当条目身份，
//   而同一个知识点会被切成好几段（讲 A → 跳去 B → 又回 A），
//   于是 `querySelector` 只命中第一段，列表里几个同名条目全跳到同一个地方。
//   抽出来之后可以用 node 直接测（`tests/js/test_rail.mjs`），
//   视图里剩下那点 DOM 代码不需要也不可能这样测。

/**
 * 按对话里段落的出现顺序算出列表条目。
 *
 * @param {string[]} titles 各段落的知识点名，**按出现顺序**（同名的会出现多次）
 * @returns {{sectionIndex: number, title: string, order: number, label: string}[]}
 *   `sectionIndex` 是该段落的唯一序号（跳转认它，不认名字）；
 *   `order` 是这个知识点第几次被讲到；`label` 是对应的显示文案
 *   （第 2 次起带「第 N 次」，否则列表里几行一模一样，用户分不清点哪条）。
 */
export function buildIndexEntries(titles) {
  const seen = new Map();
  return titles.map((title, index) => {
    const order = (seen.get(title) || 0) + 1;
    seen.set(title, order);
    return {
      sectionIndex: index,
      title,
      order,
      label: order > 1 ? `${title} · 第 ${order} 次` : title,
    };
  });
}
