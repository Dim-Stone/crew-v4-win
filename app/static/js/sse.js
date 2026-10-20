// SSE 客户端：订阅一个 Turn 的增量输出，并在断线后**先查状态再决定**。
//
// 约定的事件类型（与 app/services/runtime.py 对应）：
//   snapshot 连接建立时补发的已有内容（防止连晚了丢开头）
//   status   阶段提示（"思考中" / "规划中" / "画图中"）
//   delta    文本增量（逐字产出）
//   diagram  图解代码
//   sources  这一轮依据的资料出处（带 labels，界面显示"依据：…"）
//   profile  画像数据
//   outline  大纲生成完成
//   saved    这一轮落盘的讲义
//   done     正常结束（带完整文本）
//   error    失败或被取消（带 message）
//
// ## 这一版修的是"断线就报失败"
//
// 旧实现在连接出错时直接 `finished = true` + `close()` + 报"连接中断了，请重试"。
// 问题在于：**服务端可能已经做完了**——后台任务照常跑、结果照常落库，
// 只是那条 SSE 断了。用户看到"失败"，于是重复点一次，白花一次模型调用。
//
// 现在的处理顺序是：
//
//     连接出错 → 查 GET /api/turns/<id>
//                 ├─ done            → 直接当成功，把 result 交给 onDone
//                 ├─ failed/cancelled→ 交真实原因给 onError
//                 └─ pending/running → 退避重连（服务端会重新补发快照与已有正文）
//
// 重连是**重新建立一条 SSE**，不是依赖浏览器原生的自动重连：后者不知道
// "这个任务已经结束了、不该再连"，会在 done 之后继续重试。

/** 最多重连几次。超过就如实告诉用户"重试失败"，而不是无限转圈。 */
const MAX_RETRIES = 5;

/** 退避上限（毫秒）。第一次 1s，之后翻倍，最多 8s。 */
const MAX_BACKOFF_MS = 8000;

/** 多少毫秒没有任何事件就提醒"可能卡住了"。 */
const STALL_AFTER_MS = 30000;

export function subscribe(turnId, handlers = {}) {
  let source = null;
  let finished = false;
  let retries = 0;
  let closed = false;
  let stallTimer = null;
  let stallFired = false;
  let lastEventAt = Date.now();

  const noteActivity = () => {
    lastEventAt = Date.now();
    stallFired = false;
  };

  /**
   * 停滞提醒：服务端"连接还在、但很久没有新事件"时告诉界面一声。
   *
   * 为什么要它：真实的故障是流式生成停在半路——连接没断、也没报错，
   * 界面就一直停在"讲课中"。服务端那边有停滞看门狗，但前端先一步提醒，
   * 学生才知道"不是我在等，是它卡住了"，并且可以主动中止。
   * 只提醒**一次**（`stallFired`），否则每 3 秒弹一次会变成骚扰。
   */
  function startStallWatch() {
    stopStallWatch();
    lastEventAt = Date.now();
    stallTimer = setInterval(() => {
      if (finished || closed) return;
      const idle = Date.now() - lastEventAt;
      if (idle >= STALL_AFTER_MS && !stallFired) {
        stallFired = true;
        handlers.onStall?.(Math.round(idle / 1000));
      }
    }, 3000);
  }

  function stopStallWatch() {
    if (stallTimer) {
      clearInterval(stallTimer);
      stallTimer = null;
    }
  }

  const emitOnce = (name, data) => {
    if (finished) return;
    finished = true;
    stopStallWatch();
    closeSource();
    handlers[name]?.(data);
  };

  function closeSource() {
    if (source) {
      source.close();
      source = null;
    }
  }

  function bind(type, fn) {
    if (!fn) return;
    source.addEventListener(type, (event) => {
      let data = {};
      try {
        data = event.data ? JSON.parse(event.data) : {};
      } catch {
        data = { raw: event.data };
      }
      // 收到任何事件都算"还在动"：停滞判定只看有没有动静
      noteActivity();
      fn(data);
    });
  }

  function connect() {
    if (finished || closed) return;
    source = new EventSource(`/api/turns/${turnId}/stream`);
    startStallWatch();

    bind("snapshot", handlers.onSnapshot);
    bind("status", handlers.onStatus);
    bind("delta", handlers.onDelta);
    bind("diagram", handlers.onDiagram);
    bind("profile", handlers.onProfile);
    bind("outline", handlers.onOutline);
    bind("saved", handlers.onSaved);
    bind("sources", handlers.onSources);
    bind("progress", handlers.onProgress);
    bind("warn", handlers.onWarn);
    bind("alert", handlers.onAlert);
    // 小测出好了：带 quiz_id，前端据此去取题面（题面比正文大，不走事件体）
    bind("quiz", handlers.onQuiz);

    bind("done", (data) => emitOnce("onDone", data));
    bind("error", (data) => emitOnce("onError", data));

    source.addEventListener("error", (event) => {
      if (finished || closed) return;
      closeSource();          // 先关掉这条，避免与重连的那条并存

      // 服务端主动发来的 error 事件带 data；连接层错误没有 data。
      if (event && event.data) {
        try {
          emitOnce("onError", JSON.parse(event.data));
          return;
        } catch {
          /* 落到下面的恢复流程 */
        }
      }
      recover();
    });
  }

  /**
   * 连接断了之后：先问后端"这个任务到底怎么了"，再决定是收尾还是重连。
   *
   * 这一步是整份文件存在的理由——把"网络断了"与"任务失败了"区分开。
   */
  async function recover() {
    if (finished || closed) return;

    try {
      const resp = await fetch(`/api/turns/${turnId}`, { cache: "no-store" });
      if (resp.ok) {
        const payload = await resp.json();
        const data = payload?.data || {};
        if (data.status === "done") {
          emitOnce("onDone", { text: data.result || "" });
          return;
        }
        if (data.status === "failed" || data.status === "cancelled") {
          emitOnce("onError", { message: data.error || "任务已中断" });
          return;
        }
        // pending / running：还在跑，值得重连
      }
    } catch {
      /* 状态查询也失败：当作网络问题，继续重试 */
    }

    if (retries >= MAX_RETRIES) {
      emitOnce("onError", {
        message: `与服务器的连接中断了，已重试 ${MAX_RETRIES} 次。任务可能仍在后台运行，刷新页面可以看到结果。`,
      });
      return;
    }

    retries += 1;
    handlers.onRetry?.(retries);
    const delay = Math.min(1000 * 2 ** (retries - 1), MAX_BACKOFF_MS);
    setTimeout(connect, delay);
  }

  connect();

  // 返回一个可主动关闭的句柄：页面离开、或用户放弃等待时调用。
  return {
    close() {
      closed = true;
      finished = true;
      stopStallWatch();
      closeSource();
    },

    /**
     * 主动中止这一轮。
     *
     * 界面在"停滞提醒"里给这个入口：等服务端自己的停滞阈值到点，
     * 用户要干等好几分钟。取消是**请求**语义——worker 下一次产出时才停，
     * 但服务端会同时把任务收尾（`runtime.cancel` + `sweep_stalls` 兜底）。
     */
    async cancel() {
      try {
        await fetch(`/api/turns/${turnId}/cancel`, { method: "POST" });
      } catch {
        /* 取消失败就让它继续跑，服务端看门狗兜底 */
      }
    },
  };
}
