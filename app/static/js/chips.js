// 输入框上方的快捷追问：把"后端真的会当回事的话"做成可点的按钮。
//
// 这个文件里最要紧的一条是：**芯片里的句子必须是后端真认得、真会处理的说法**。
// 后端现在有两条现成的识别链：
//   · app/signals.py 的规则词表——"没懂""跟不上""多讲点"会被当成学习信号，
//     够强的还会改写画像、调整节奏（下一轮讲法因此变化）；
//   · services/teaching_service.py 的推进词表——"继续吧""下一节"才会翻页
//     （而且只在**这一节的教案已经讲完**时才真当翻页，见 wants_forward 的注释）。
// 所以芯片是"替你把这句话说出来"，点了等于打字，不多一条捷径。
//
// 也正因如此，这里**不摆"出题考考我""给我打分"**：出题测评是 README 里明确
// 暂缓的一期范围外能力，摆一个点下去没人接的按钮，比少一个按钮更伤。

/** 这一节还没讲完时的常规追问。 */
const BASE_CHIPS = [
  { text: "这里没懂，再讲细一点", hint: "会被当成「太难、听不懂」，之后讲得更细" },
  { text: "有点跟不上，慢一点", hint: "会被当成「讲得太快」，之后放慢节奏" },
  { text: "举个生活里的例子", hint: "换个说法再讲一遍" },
  { text: "多讲点更深的", hint: "会被当成「想再深入一点」" },
];

/** 教案已经讲完才多出来的那一个：这时候"继续吧"才是往下走的意思。 */
const NEXT_CHIP = { text: "继续吧", hint: "这一节讲完了，进入下一个知识点" };

/**
 * 此刻该摆哪几个快捷追问。
 *
 * @param {{hasPoint: boolean, lessonFinished: boolean}} state
 *   `hasPoint` 有没有当前知识点（全讲完时后端不再接消息，界面就不该摆芯片）；
 *   `lessonFinished` 这一节的教案讲完没有（决定"继续吧"要不要出现）。
 * @returns {{text: string, hint: string}[]} 空数组表示此刻不该摆
 */
export function buildChips({ hasPoint, lessonFinished } = {}) {
  // 知识点全讲完时，发消息会被后端挡下（"本课知识点已讲完"），
  // 摆一排点了也没用的芯片只会让人以为界面坏了。
  if (!hasPoint) return [];
  return lessonFinished ? [NEXT_CHIP, ...BASE_CHIPS] : [...BASE_CHIPS];
}
