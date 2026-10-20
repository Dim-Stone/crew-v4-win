// HTTP 客户端：统一处理 {ok, data, error} 信封。
// 后端所有接口都返回这个形状，所以错误处理只需要写一次。

async function request(path, { method = "GET", body } = {}) {
  // cache: "no-store" —— 接口响应绝不能被缓存：
  // "生成完再查一次"如果命中缓存，就会拿回旧数据，界面看起来像没生成成功
  const options = { method, headers: {}, cache: "no-store" };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }

  const resp = await fetch(path, options);
  let payload;
  try {
    payload = await resp.json();
  } catch {
    throw new Error(`服务器返回了非 JSON 内容（HTTP ${resp.status}）`);
  }

  if (!resp.ok || payload.ok === false) {
    throw new Error(payload.error || `请求失败（HTTP ${resp.status}）`);
  }
  return payload.data;
}

export const get = (path) => request(path);
export const post = (path, body) => request(path, { method: "POST", body });
export const put = (path, body) => request(path, { method: "PUT", body });
export const patch = (path, body) => request(path, { method: "PATCH", body });
export const del = (path) => request(path, { method: "DELETE" });

/** 读取 URL 查询参数。 */
export function query(name) {
  return new URLSearchParams(location.search).get(name);
}

/** 轻量的界面提示（不引入组件库）。 */
export function toast(message, kind = "info") {
  const el = document.createElement("div");
  el.className = `notice notice-${kind === "error" ? "error" : kind === "ok" ? "ok" : "warn"}`;
  el.style.position = "fixed";
  el.style.right = "20px";
  el.style.bottom = "20px";
  el.style.zIndex = "999";
  el.style.maxWidth = "420px";
  el.style.boxShadow = "var(--shadow)";
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 9000 : 4000);
}
