// 题目选项的小工具。
//
// 为什么单独一个文件：课堂小测和章末测试都要用，而且这条规则
// 一旦两边不一致，卷面就会出现 "A. A. 1" 这种重复编号（真实报过）。

//: 编号后面**必须**跟一个分隔符才算编号。
//: 只吃"空格"会把 "A 大于 B" 这种正常选项内容也削掉，所以不用 "\\s" 兜底。
const SEPARATOR = "[.、．:：,，)）]";

/**
 * 去掉选项文字里自带的编号，只保留内容。
 *
 * 服务端解析时已经剥过一次；这里是**第二道**——历史数据（改进之前存下的
 * 小测）仍然带着 "A."，而重新看旧卷子不应该需要重新出题。
 *
 * 两道匹配：先按"自己的 key"剥（`{"key":"A","text":"A. 1"}`），
 * 再按任意 A–D 剥一次（模型偶尔把编号写串，`{"key":"B","text":"A. 1"}`）——
 * 后者要求分隔符，避免误伤正文。
 */
export function stripOptionLabel(key, text) {
  const value = String(text ?? "").trim();
  const label = String(key ?? "").trim().toUpperCase().slice(0, 1);
  if (!value) return value;

  if (label) {
    const own = new RegExp(`^[（(\\s]*${label}[）)\\s]*${SEPARATOR}\\s*`, "i");
    const stripped = value.replace(own, "").trim();
    if (stripped && stripped !== value) return stripped;
  }

  const any = new RegExp(`^[（(\\s]*[A-Da-d][）)\\s]*${SEPARATOR}\\s*`);
  const stripped = value.replace(any, "").trim();
  return stripped || value;
}
