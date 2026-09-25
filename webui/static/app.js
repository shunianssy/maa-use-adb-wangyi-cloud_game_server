/* 网易云游戏远程控制台前端逻辑
 * 职责:
 *  - 维护 WebSocket 连接(自动重连)并接收状态/帧/回执
 *  - 渲染实时画面, 处理「点击」与「拖拽滑动」并把坐标归一化为游戏分辨率
 *  - 文本输入、启动/断开连接、操作日志
 */
(function () {
  "use strict";

  /* ---------- DOM 引用 ---------- */
  const $ = (id) => document.getElementById(id);
  const els = {
    statusBadge: $("statusBadge"),
    btnStart: $("btnStart"),
    btnExit: $("btnExit"),
    stageWrap: $("stageWrap"),
    stageImg: $("stageImg"),
    stagePlaceholder: $("stagePlaceholder"),
    placeholderText: $("placeholderText"),
    swipeHint: $("swipeHint"),
    kvStatus: $("kvStatus"),
    kvResolution: $("kvResolution"),
    kvAddr: $("kvAddr"),
    kvRemaining: $("kvRemaining"),
    textForm: $("textForm"),
    textInput: $("textInput"),
    logList: $("logList"),
  };

  /* ---------- 状态 ---------- */
  const state = {
    ws: null,
    connected: false,       // WebSocket 是否在线
    gameReady: false,       // 云游戏画面是否就绪(status==ok)
    width: 0,               // 游戏实际分辨率(用于坐标归一化)
    height: 0,
    reconnectDelay: 1000,
  };

  /* ---------- 工具函数 ---------- */
  function log(kind, msg) {
    const li = document.createElement("li");
    li.className = kind;
    li.textContent = msg;
    els.logList.prepend(li);
    // 限制日志条数, 避免 DOM 无限增长
    while (els.logList.children.length > 80) {
      els.logList.removeChild(els.logList.lastChild);
    }
  }

  function setBadge(text, dataState) {
    els.statusBadge.textContent = text;
    els.statusBadge.dataset.state = dataState;
  }

  function formatRemaining(sec) {
    if (sec === null || sec === undefined) return "—";
    const s = Number(sec);
    if (!Number.isFinite(s)) return "—";
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = Math.floor(s % 60);
    const mm = String(m).padStart(2, "0");
    const rr = String(r).padStart(2, "0");
    return h > 0 ? `${h} 小时 ${mm}:${rr}` : `${mm}:${rr}`;
  }

  /* ---------- WebSocket ---------- */
  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}/ws`;
  }

  function connect() {
    const ws = new WebSocket(wsUrl());
    state.ws = ws;

    ws.onopen = () => {
      state.connected = true;
      state.reconnectDelay = 1000;
      setBadge("已连接", "connecting"); // 游戏是否就绪需等待 status 消息
      log("sys", "已连接到服务器");
    };

    ws.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      handleMessage(msg);
    };

    ws.onclose = () => {
      if (state.ws !== ws) return; // 已被新连接取代
      state.connected = false;
      setBadge(state.gameReady ? "连接中断" : "未连接", "error");
      scheduleReconnect();
    };

    ws.onerror = () => {
      // 错误后 onclose 会触发重连, 这里无需额外处理
    };
  }

  function scheduleReconnect() {
    if (state.ws && (state.ws.readyState === WebSocket.CONNECTING || state.ws.readyState === WebSocket.OPEN)) {
      return;
    }
    // 指数退避重连, 上限 10s
    const delay = Math.min(state.reconnectDelay, 10000);
    state.reconnectDelay = state.reconnectDelay * 2;
    setTimeout(connect, delay);
  }

  function send(obj) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(obj));
    } else {
      log("fail", "未连接,命令未发送");
    }
  }

  /* ---------- 消息处理 ---------- */
  function handleMessage(msg) {
    switch (msg.type) {
      case "status":
        applyStatus(msg);
        break;
      case "frame":
        if (msg.image) {
          els.stageImg.src = "data:image/jpeg;base64," + msg.image;
          // 隐藏占位提示
          els.stagePlaceholder.hidden = true;
        }
        break;
      case "ack":
        applyAck(msg);
        break;
      default:
        break;
    }
  }

  function applyStatus(msg) {
    state.gameReady = msg.status === "ok";
    if (state.gameReady) {
      state.width = Number(msg.width) || 0;
      state.height = Number(msg.height) || 0;
      setBadge("游戏中", "connected");
      els.kvStatus.textContent = "就绪";
      els.kvResolution.textContent = `${state.width} × ${state.height}`;
      els.kvRemaining.textContent = formatRemaining(msg.remaining_time);
      els.btnStart.disabled = true;
      els.btnExit.disabled = false;
    } else {
      els.kvStatus.textContent = msg.status === "connecting" ? "连接中…" : "未连接";
      els.btnStart.disabled = false;
      els.btnExit.disabled = true;
      if (msg.status === "disconnected") {
        setBadge("未连接", "unconnected");
        // 无画面:恢复占位提示
        els.stagePlaceholder.hidden = false;
        els.placeholderText.textContent = "尚未连接,点击「启动云游戏」开始。";
      }
    }
    els.kvAddr.textContent = location.host;
  }

  function applyAck(msg) {
    const txt = msg.message || msg.command;
    if (msg.ok) {
      log("ok", txt || `${msg.command} 完成`);
    } else {
      log("fail", `${msg.command}: ${txt || "失败"}`);
    }
  }

  /* ---------- 命令操作 ---------- */
  function doStart() {
    log("sys", "正在启动云游戏…");
    send({ type: "start" });
  }

  function doExit() {
    log("sys", "正在断开云游戏…");
    send({ type: "exit" });
  }

  /* ---------- 坐标换算 ---------- */
  function imageToGame(evTargetOrTouch) {
    const rect = els.stageWrap.getBoundingClientRect();
    const clientX = (evTargetOrTouch.touches ? evTargetOrTouch.touches[0].clientX : evTargetOrTouch.clientX);
    const clientY = (evTargetOrTouch.touches ? evTargetOrTouch.touches[0].clientY : evTargetOrTouch.clientY);
    const px = clientX - rect.left;
    const py = clientY - rect.top;
    // 依据游戏分辨率换算(画面按 contain 适配)
    const gx = state.width ? Math.floor((px / rect.width) * state.width) : Math.floor(px);
    const gy = state.height ? Math.floor((py / rect.height) * state.height) : Math.floor(py);
    return {
      gx: Math.max(0, Math.min(state.width || gx, gx)),
      gy: Math.max(0, Math.min(state.height || gy, gy)),
      px,
      py,
    };
  }

  /* ---------- 点击(点击后立即发送, 不响应拖拽) ---------- */
  function handlePointerDown(e) {
    if (!state.gameReady) return;
    const { gx, gy } = imageToGame(e);
    send({ type: "click", x: gx, y: gy });
  }

  /* ---------- 拖拽滑动 ---------- */
  let dragStart = null; // {gx, gy, px, py}

  function beginDrag(e) {
    if (!state.gameReady) return;
    dragStart = imageToGame(e);
    els.swipeHint.hidden = false;
    els.swipeHint.style.left = dragStart.px + "px";
    els.swipeHint.style.top = dragStart.py + "px";
    els.swipeHint.style.width = "0px";
    els.swipeHint.style.height = "0px";
    e.preventDefault();
  }

  function moveDrag(e) {
    if (!dragStart) return;
    const cur = imageToGame(e);
    const left = Math.min(dragStart.px, cur.px);
    const top = Math.min(dragStart.py, cur.py);
    els.swipeHint.style.left = left + "px";
    els.swipeHint.style.top = top + "px";
    els.swipeHint.style.width = Math.abs(cur.px - dragStart.px) + "px";
    els.swipeHint.style.height = Math.abs(cur.py - dragStart.py) + "px";
    e.preventDefault();
  }

  function endDrag(e) {
    if (!dragStart) return;
    const cur = imageToGame(e);
    const dx = Math.abs(cur.gx - dragStart.gx);
    const dy = Math.abs(cur.gy - dragStart.gy);
    els.swipeHint.hidden = true;
    // 距离过小视为点击(已由 pointerdown 处理), 以免重复点击
    if (dx < 8 && dy < 8) {
      dragStart = null;
      return;
    }
    // 滑动时长按位移估算(300~600ms)
    const dist = Math.hypot(dx, dy);
    const duration = Math.max(150, Math.min(600, Math.round(dist)));
    send({ type: "swipe", x1: dragStart.gx, y1: dragStart.gy, x2: cur.gx, y2: cur.gy, duration });
    dragStart = null;
  }

  /* ---------- 一键长草(MAA) ---------- */
  const MAA_TASKS = [
    "awaken", "recruit", "infrast", "combat", "credit", "reward",
  ];
  let maaPollTimer = null;
  let coordEnabled = false;
  let appSettings = null;       // 后端已保存的设置(启动时加载)
  let saveTimer = null;         // 设置自动保存防抖计时器

  function getEnabledTasks() {
    return MAA_TASKS.filter((t) => {
      const box = els.taskToggle && els.taskToggle[t];
      return box ? box.checked : false;
    });
  }

  function setMaaStatus(state) {
    els.maaStatus.dataset.state = state;
    const map = { idle: "空闲", running: "执行中…", error: "出错", done: "完成" };
    els.maaStatus.textContent = map[state] || state;
    els.btnMaaRun.disabled = state === "running";
  }

  async function doMaaRun() {
    const tasks = getEnabledTasks();
    // 每周剿灭为独立开关: 勾选后并入任务列表
    if (els.annihilationEnabled && els.annihilationEnabled.checked) {
      tasks.push("annihilation");
    }
    if (!tasks.length) {
      log("fail", "请至少启用一个任务");
      return;
    }
    const options = collectRunOptions();
    saveSettings(null, { silent: true });   // 先落盘当前表单, 保证下次加载一致
    setMaaStatus("running");
    els.maaProgress.textContent = "";
    log("sys", `一键长草开始: ${tasks.join(", ")}`);
    try {
      const resp = await fetch("/maa/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tasks, options }),
      });
      const data = await resp.json();
      if (!data || data.status !== "ok") {
        setMaaStatus("error");
        log("fail", `启动失败: ${(data && data.message) || resp.status}`);
      }
    } catch (e) {
      setMaaStatus("error");
      log("fail", `请求失败: ${e.message}`);
    }
  }

  /* 从表单收集本次运行的作战选项 fight/annihilation */
  function collectRunOptions() {
    const medSel = (els.medicineSel && els.medicineSel.value) || "auto";
    let medicine = 0, medicineMode = "off";
    if (medSel === "auto") { medicineMode = "auto"; medicine = 999; }
    else if (medSel !== "off") { medicineMode = "num"; medicine = Number(medSel) || 0; }
    const anniAuto = !!(els.annihilationAuto &&
      els.annihilationAuto.value === "auto");
    return {
      fight: {
        stage: (els.fightStage && els.fightStage.value.trim()) || "",
        times: Number(els.fightTimes && els.fightTimes.value) || 5,
        series: Number(els.fightSeries && els.fightSeries.value) || 0,
        medicine_mode: medicineMode,
        medicine: medicine,
      },
      annihilation: {
        auto: anniAuto,
        times: anniAuto ? 999 : Number(els.annihilationAuto && els.annihilationAuto.value) || 4,
      },
      signin: { enabled: !!(els.signinEnabled && els.signinEnabled.checked) },
      award: {
        award: !!(els.awardAward && els.awardAward.checked),
        mail: !!(els.awardMail && els.awardMail.checked),
        recruit: !!(els.awardRecruit && els.awardRecruit.checked),
        orundum: !!(els.awardOrundum && els.awardOrundum.checked),
        mining: !!(els.awardMining && els.awardMining.checked),
        specialaccess: !!(els.awardSpecial && els.awardSpecial.checked),
      },
    };
  }

  /* 收集设置 patch: tasks 开关 / fight / annihilation / signin / daily */
  function collectSettingsPatch() {
    const opts = collectRunOptions();
    const tasks = {};
    MAA_TASKS.forEach((t) => {
      tasks[t] = !!(els.taskToggle && els.taskToggle[t] && els.taskToggle[t].checked);
    });
    return {
      tasks,
      fight: opts.fight,
      annihilation: {
        enabled: !!(els.annihilationEnabled && els.annihilationEnabled.checked),
        auto: opts.annihilation.auto,
        times: opts.annihilation.auto ? 4 : (opts.annihilation.times || 4),
      },
      signin: opts.signin,
      award: opts.award,
      daily: {
        enabled: !!(els.dailyEnabled && els.dailyEnabled.checked),
        time: (els.dailyTime && els.dailyTime.value) || "08:00",
      },
    };
  }

  /* 应用已保存设置到表单(页面加载/设置变化时) */
  function applySettings(s) {
    if (!s) return;
    appSettings = s;
    // 任务开关
    MAA_TASKS.forEach((t) => {
      const box = els.taskToggle && els.taskToggle[t];
      if (box) box.checked = !!(s.tasks && s.tasks[t]);
    });
    const f = s.fight || {};
    if (els.fightStage) els.fightStage.value = f.stage || "";
    if (els.fightTimes) els.fightTimes.value = f.times != null ? f.times : 5;
    if (els.fightSeries) els.fightSeries.value = f.series != null ? f.series : 0;
    // 理智药: mode 映射到下拉(兼容旧 medicine_enabled 字段)
    const mMode = f.medicine_mode != null ? f.medicine_mode
      : (f.medicine_enabled ? "num" : "off");
    if (els.medicineSel) {
      els.medicineSel.value = mMode === "auto" ? "auto"
        : mMode === "off" ? "off" : String(f.medicine != null ? f.medicine : 3);
    }
    const a = s.annihilation || {};
    if (els.annihilationEnabled) els.annihilationEnabled.checked = a.enabled !== false;
    if (els.annihilationAuto) {
      els.annihilationAuto.value = a.auto !== false ? "auto" : String(a.times != null ? a.times : 4);
    }
    if (els.signinEnabled) els.signinEnabled.checked = !!((s.signin || {}).enabled);
    // 领取奖励细分项
    const aw = s.award || {};
    [["awardAward", "award"], ["awardMail", "mail"], ["awardRecruit", "recruit"],
     ["awardOrundum", "orundum"], ["awardMining", "mining"],
     ["awardSpecial", "specialaccess"]].forEach(([id, key]) => {
      if (els[id]) els[id].checked = aw[key] !== false;
    });
    const d = s.daily || {};
    if (els.dailyEnabled) els.dailyEnabled.checked = !!d.enabled;
    if (els.dailyTime) els.dailyTime.value = d.time || "08:00";
    if (els.dailyNote) {
      els.dailyNote.textContent = s.last_daily_run
        ? `上次定时执行: ${s.last_daily_run}` : "尚未定时执行过";
    }
    syncFormStates();
  }

  /* 同步控件互斥: 作战/领奖设置可用性(依赖对应任务开关) */
  function syncFormStates() {
    const combatOn = !!(els.taskToggle && els.taskToggle.combat &&
      els.taskToggle.combat.checked);
    if (els.fightOptions) {
      const lists = els.fightOptions.querySelectorAll("input,select");
      lists.forEach((el) => {
        // 剿灭/签到/理智药独立于理智作战开关, 始终可编辑
        if (el === els.annihilationEnabled || el === els.annihilationAuto ||
            el === els.medicineSel || el === els.signinEnabled) return;
        el.disabled = !combatOn;
      });
    }
    // 领取奖励细分项: 勾选「领取奖励」后编辑; 全不勾选视为禁用整个 Award 任务
    const rewardOn = !!(els.taskToggle && els.taskToggle.reward &&
      els.taskToggle.reward.checked);
    if (els.awardOptions) {
      els.awardOptions.querySelectorAll("input").forEach((el) => {
        el.disabled = !rewardOn;
      });
    }
  }

  /* 设置自动保存(防抖 800ms), patch 为空时保存全部表单 */
  function saveSettings(patch, opts) {
    opts = opts || {};
    clearTimeout(saveTimer);
    const body = patch || collectSettingsPatch();
    saveTimer = setTimeout(async () => {
      try {
        const resp = await fetch("/maa/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (data && data.settings) appSettings = data.settings;
        if (!opts.silent) log("ok", "设置已保存");
      } catch (e) {
        if (!opts.silent) log("fail", `设置保存失败: ${e.message}`);
      }
    }, 800);
  }

  async function loadSettings() {
    try {
      const resp = await fetch("/maa/settings");
      const data = await resp.json();
      if (data && data.settings) applySettings(data.settings);
    } catch (e) {
      // 服务未就绪时静默, 表单保持默认值
    }
  }

  async function doMaaSignin() {
    log("sys", "发起网易云游戏签到…");
    try {
      const resp = await fetch("/maa/signin", { method: "POST" });
      const data = await resp.json();
      if (data && data.result && data.result.ok) {
        log("ok", `签到成功: ${data.result.endpoint}`);
      } else {
        const msg = (data && (data.message || (data.result && data.result.message))) || "签到失败";
        log("fail", msg);
      }
    } catch (e) {
      log("fail", `签到请求失败: ${e.message}`);
    }
  }

  async function doMaaStop() {
    log("sys", "请求停止任务…");
    try {
      await fetch("/maa/stop", { method: "POST" });
    } catch (e) {
      log("fail", `停止请求失败: ${e.message}`);
    }
  }

  /* 用后端状态轮询结果同步云游戏连接 UI(独立于 WS 消息) */
  function syncGameFromStatus(game) {
    if (!game || typeof game !== "object") return;
    const isOk = game.status === "ok";
    // 避免每 2 秒重复设置相同内容(减少无效 DOM 写入)
    if (isOk) {
      state.width = Number(game.width) || state.width;
      state.height = Number(game.height) || state.height;
      setBadge("游戏中", "connected");
      els.kvStatus.textContent = "就绪";
      els.kvResolution.textContent = `${state.width} × ${state.height}`;
      els.btnStart.disabled = true;
      els.btnExit.disabled = false;
      els.stagePlaceholder.hidden = true;
    } else {
      const connecting = game.status === "connecting";
      els.kvStatus.textContent = connecting ? "连接中…" : "未连接";
      els.btnStart.disabled = connecting;
      els.btnExit.disabled = true;
      setBadge(connecting ? "连接中" : "未连接", connecting ? "connecting" : "unconnected");
      // 仅当画面未显示时才恢复占位提示
      if (!state.width && !state.height) {
        els.stagePlaceholder.hidden = false;
        els.placeholderText.textContent = connecting
          ? "正在连接云游戏,请稍候…"
          : "尚未连接,点击「启动云游戏」开始。";
      }
    }
    els.kvAddr.textContent = location.host;
  }

  /* 渲染一键长草运行日志(从轮询结果刷新, 无数据时不改动 DOM) */
  function renderMaaLog(logArr) {
    if (!Array.isArray(logArr) || !els.maaLogList) return;
    // 快速比对: 长度与末条相同则跳过刷新
    const lastKey = logArr.length ? (logArr[logArr.length - 1] + "|" + logArr.length) : "";
    if (lastKey === (els.maaLogList.dataset.lastKey || "")) return;
    els.maaLogList.dataset.lastKey = lastKey;
    els.maaLogList.innerHTML = "";
    const frag = document.createDocumentFragment();
    logArr.forEach((line) => {
      const li = document.createElement("li");
      li.textContent = line;
      li.className = line.includes("失败") || line.includes("异常") ? "fail" : "sys";
      frag.appendChild(li);
    });
    els.maaLogList.appendChild(frag);
    // 自动滚动到底部显示最新日志
    els.maaLogList.scrollTop = els.maaLogList.scrollHeight;
  }

  /* 收起/展开底部控制面板, 释放画布空间给游戏画面 */
  function setPanelCollapsed(collapsed) {
    if (!els.appCol || !els.controlRow || !els.stage) return;
    els.appCol.classList.toggle("panel-collapsed", collapsed);
    els.controlRow.classList.toggle("control-hidden", collapsed);
    els.stage.classList.toggle("stage-full", collapsed);
    els.btnPanelToggle.hidden = collapsed;
    els.btnPanelRestore.hidden = !collapsed;
    els.btnPanelToggle.setAttribute("aria-expanded", String(!collapsed));
  }

  async function pollMaaStatus() {
    try {
      const resp = await fetch("/maa/status");
      if (!resp.ok) return;
      const s = await resp.json();
      // 用 /maa/status 随带的服务状态兜底同步云游戏连接 UI
      // 防止 WS 消息时序导致页面停留在「未连接」占位
      syncGameFromStatus(s.game);
      // 渲染一键长草日志(每次轮询刷新)
      renderMaaLog(s.log);
      // 展示日志落盘路径(便于远端下载/调试)
      if (els.maaLogPath && s.log_path) {
        els.maaLogPath.textContent = "日志: " + s.log_path;
      }
      if (s.state === "running") {
        setMaaStatus("running");
        els.maaProgress.textContent =
          `已完成 ${s.finished.length}/${s.total.length}` +
          (s.current ? ` · 正在: ${s.current}` : "");
      } else if (s.state === "error") {
        setMaaStatus("error");
        els.maaProgress.textContent = s.error || "";
        log("fail", `任务出错: ${s.error || "未知错误"}`);
      } else if (s.state === "idle") {
        setMaaStatus(s.finished.length ? "done" : "idle");
        els.maaProgress.textContent = s.finished.length
          ? `已执行 ${s.finished.length} 个任务 ${s.message || ""}`
          : "";
      }
    } catch (e) {
      // 服务未就绪时静默, 下次轮询再试
    }
  }

  /* ---------- 小工具 ---------- */
  function doScreencap() {
    const img = els.stageImg;
    if (!img.src || img.src.startsWith("data:image/jpeg;base64,") === false) {
      log("fail", "暂无画面可保存");
      return;
    }
    const a = document.createElement("a");
    a.href = img.src;
    a.download = `screencap_${Date.now()}.jpg`;
    a.click();
    log("ok", "截图已保存");
  }

  function doToggleCoord() {
    coordEnabled = !coordEnabled;
    els.coordNote.textContent = `坐标显示:${coordEnabled ? "开" : "关"}`;
  }

  /* ---------- 事件绑定 ---------- */
  function bindEvents() {
    els.btnStart.addEventListener("click", doStart);
    els.btnExit.addEventListener("click", doExit);

    // 一键长草开关的 DOM 引用
    els.taskToggle = {};
    document.querySelectorAll(".task-toggle").forEach((label) => {
      const task = label.dataset.task;
      const box = label.querySelector("input");
      if (task && box) els.taskToggle[task] = box;
    });
    els.btnMaaRun = $("btnMaaRun");
    els.btnMaaStop = $("btnMaaStop");
    els.btnMaaSignin = $("btnMaaSignin");
    els.maaStatus = $("maaStatus");
    els.maaProgress = $("maaProgress");
    els.maaLogList = $("maaLogList");
    els.maaLogPath = $("maaLogPath");
    // 作战设置与每日定时控件
    els.fightOptions = $("fightOptions");
    els.fightStage = $("fightStage");
    els.fightTimes = $("fightTimes");
    els.fightSeries = $("fightSeries");
    els.medicineSel = $("medicineSel");
    els.annihilationEnabled = $("annihilationEnabled");
    els.annihilationAuto = $("annihilationAuto");
    els.signinEnabled = $("signinEnabled");
    els.dailyEnabled = $("dailyEnabled");
    els.dailyTime = $("dailyTime");
    els.dailyNote = $("dailyNote");
    // 领取奖励细分项
    els.awardOptions = $("awardOptions");
    ["awardAward", "awardMail", "awardRecruit", "awardOrundum",
     "awardMining", "awardSpecial"].forEach((id) => { els[id] = $(id); });
    // 表单改动 → 联动 + 自动保存
    const formEls = [
      "fightStage", "fightTimes", "fightSeries", "medicineSel",
      "annihilationEnabled", "annihilationAuto", "signinEnabled",
      "awardAward", "awardMail", "awardRecruit", "awardOrundum",
      "awardMining", "awardSpecial",
      "dailyEnabled", "dailyTime",
    ];
    formEls.forEach((id) => {
      if (!els[id]) return;
      const evt = (els[id].tagName === "SELECT" || els[id].type === "checkbox") ? "change" : "input";
      els[id].addEventListener(evt, () => {
        syncFormStates();
        saveSettings();
      });
    });
    // 任务开关: 勾选即保存(含表单联动)
    Object.keys(els.taskToggle).forEach((t) => {
      els.taskToggle[t].addEventListener("change", () => {
        syncFormStates();
        saveSettings();
      });
    });
    els.btnScreencap = $("btnScreencap");
    els.btnCoord = $("btnCoord");
    els.coordNote = $("coordNote");
    els.btnMaaRun.addEventListener("click", doMaaRun);
    els.btnMaaStop.addEventListener("click", doMaaStop);
    els.btnMaaSignin.addEventListener("click", doMaaSignin);
    els.btnScreencap.addEventListener("click", doScreencap);
    els.btnCoord.addEventListener("click", doToggleCoord);
    // 收起/展开控制面板
    els.appCol = $("appCol");
    els.controlRow = $("controlRow");
    els.btnPanelToggle = $("btnPanelToggle");
    els.btnPanelRestore = $("btnPanelRestore");
    els.stage = $("stage");
    els.btnPanelToggle.addEventListener("click", () => setPanelCollapsed(true));
    els.btnPanelRestore.addEventListener("click", () => setPanelCollapsed(false));
    // 默认: 控制面板展开
    setPanelCollapsed(false);
    // 启动任务状态轮询
    maaPollTimer = setInterval(pollMaaStatus, 2000);

    // 文本输入
    els.textForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = els.textInput.value;
      if (!text) return;
      send({ type: "text", text });
      els.textInput.value = "";
    });

    // 指针设备:「点击 + 拖拽滑动」需区分移动端 touch 与桌面 pointer,
    // 统一用 Pointer Events(兼容触屏与鼠标), 避免双套逻辑。
    let moved = false;
    els.stageWrap.addEventListener("pointerdown", (e) => {
      moved = false;
      beginDrag(e);
    });
    els.stageWrap.addEventListener("pointermove", (e) => {
      if (dragStart) moved = true;
      moveDrag(e);
    });
    els.stageWrap.addEventListener("pointerup", (e) => {
      endDrag(e);
      // 未发生位移的按下视为点击
      if (!moved) handlePointerDown(e);
      moved = false;
    });
    els.stageWrap.addEventListener("pointercancel", () => {
      dragStart = null;
      els.swipeHint.hidden = true;
    });
  }

  /* ---------- 启动 ---------- */
  function init() {
    bindEvents();
    setBadge("未连接", "unconnected");
    els.btnExit.disabled = true;
    syncFormStates();
    loadSettings();   // 恢复已保存的设置(异步, 服务未就绪时保持默认)
    connect();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();