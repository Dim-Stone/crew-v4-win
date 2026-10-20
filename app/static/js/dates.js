// 日期选择器的纯逻辑：平移日期、拆成"年 / 月 / 日"、与 ISO 字符串互转。
//
// **为什么单独一个文件**：这段有三处容易错的边界（月末、年末、闰年），而它们全都
// 能用 Node 断言钉住（`tests/js/test_dates.mjs`）。如果塞在 dashboard.js 里，
// 它就得连着 DOM 一起跑——测不动，而错的时候症状是"切到某一天，东西不见了"。

/** 把一份日期按单位平移 `delta`（+1 是往后）。 */
export function shift(base, unit, delta) {
  const d = new Date(base.getFullYear(), base.getMonth(), base.getDate());

  if (unit === "day") {
    d.setDate(d.getDate() + delta);
    return d;
  }

  // **先挪到 1 号再改年月**，否则会踩 JS 的溢出：`setMonth` 遇到"2月31日"会
  // 顺延成 3月3日。用户点"下个月"想要的是"下个月的同一天，没有就取月末"。
  d.setDate(1);
  if (unit === "month") d.setMonth(d.getMonth() + delta);
  else if (unit === "year") d.setFullYear(d.getFullYear() + delta);
  else throw new Error(`不认识的日期单位：${unit}`);

  // 夹到那个月的最后一天（闰年的 2月29日 +1年 → 平年的 2月28日）
  const lastDay = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
  d.setDate(Math.min(base.getDate(), lastDay));
  return d;
}

/** `Date` → `YYYY-MM-DD`。
 *
 *  **不能用 `toISOString()`**：那是 UTC——在东八区，本地 `2026-10-07 00:00` 会被
 *  写成 `2026-10-06T16:00Z`，取日期部分就**少一天**。这个坑只在"当天零点附近"
 *  显形，而日期选择器正好天天用得到。 */
export function iso(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

/** `YYYY-MM-DD` → `Date`（本地零点）。格式不对、或者那天根本不存在，返回 null。 */
export function parse(text) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(text ?? "").trim());
  if (!m) return null;

  const year = Number(m[1]);
  const month = Number(m[2]) - 1;
  const day = Number(m[3]);
  const date = new Date(year, month, day);

  // `new Date(2026, 1, 31)` 不报错，它静默变成 3月3日——所以要把三个字段读回来
  // 核对一遍，否则 `.env` 里写错的那天会被"接受"成另一天。
  if (date.getFullYear() !== year || date.getMonth() !== month || date.getDate() !== day) {
    return null;
  }
  return date;
}

/** 今天的**本地零点**（用来把"今天"与带时分秒的 `new Date()` 分开）。 */
export function today() {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

/** 拆成三段给界面填数字。 */
export function parts(date) {
  return { year: date.getFullYear(), month: date.getMonth() + 1, day: date.getDate() };
}

const WEEKDAYS = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"];

/** 星期几的中文。 */
export function weekday(date) {
  return WEEKDAYS[date.getDay()];
}

/** 是不是今天（按年月日比，不按时刻）。 */
export function isToday(date) {
  const now = new Date();
  return date.getFullYear() === now.getFullYear()
    && date.getMonth() === now.getMonth()
    && date.getDate() === now.getDate();
}
