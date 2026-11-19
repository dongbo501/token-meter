"use strict";

const $ = (id) => document.getElementById(id);
const form = $("settings-form");
// Explicit label associations and input hints also help password managers.
document.querySelectorAll("label > input, label > select").forEach((input) => {
  input.id ||= "field-" + input.name;
  input.parentElement.htmlFor = input.id;
});
for (const name of ["base_url", "model", "api_key", "bark_url"]) {
  const input = form.elements[name];
  input.autocapitalize = "none";
  input.setAttribute("autocorrect", "off");
  input.spellcheck = false;
}
const icons = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  list: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9h1m3 0h6M7 14h1m3 0h6"/>',
  sliders:
    '<path d="M3 7h4m5 0h9M3 17h10m5 0h3"/><circle cx="9.5" cy="7" r="2.5"/><circle cx="15.5" cy="17" r="2.5"/>',
  calendar:
    '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4m10-4v4M3 10h18"/>',
  wallet:
    '<rect x="3" y="5" width="18" height="15" rx="2"/><path d="M21 11h-6v4h6M5 5l11-3"/>',
  chart: '<path d="M3 4v16h18M7 16v-4m5 4V6m5 10V9"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5m-18 4 9 5 9-5"/>',
  cache:
    '<path d="M5 8a8 8 0 0 1 14-2l2 3m0-5v5h-5M19 16a8 8 0 0 1-14 2l-2-3m0 5v-5h5"/>',
  activity: '<path d="M2 12h5l3-7 4 14 3-7h5"/>',
  arrow: '<path d="M4 12h15m-6-6 6 6-6 6"/>',
  edit: '<path d="m4 16-1 5 5-1L20 8l-4-4L4 16Zm10-10 4 4"/>',
  plug: '<path d="m7 3 3 3m4-3 3 3M5 9l7-7m-8 9 7-7 5 5-7 7-5-5Zm3 4-4 5"/>',
  bell: '<path d="M5 16c2-2 1-5 2-8a5 5 0 0 1 10 0c1 3 0 6 2 8l1 2H4l1-2Zm5 5h4"/>',
  play: '<path d="m7 4 13 8-13 8V4Z"/>',
  pause: '<path d="M8 5v14m8-14v14"/>',
  check: '<path d="m4 12 5 5L20 6"/>',
  inbox: '<path d="M3 14 6 5h12l3 9v5H3v-5Zm0 0h6l1 3h4l1-3h6"/>',
};
function icon(name) {
  const el = document.createElement("i");
  el.dataset.icon = name;
  el.setAttribute("aria-hidden", "true");
  el.innerHTML = '<svg viewBox="0 0 24 24">' + icons[name] + "</svg>";
  return el;
}
document
  .querySelectorAll("[data-icon]")
  .forEach((el) => el.replaceWith(icon(el.dataset.icon)));
const names = { overview: "用量总览", logs: "请求记录", settings: "运行设置" };
const plans = {
  setup: "待配置",
  paused: "已暂停",
  running: "运行中",
  tail: "预算余量不足",
  reached: "已达标",
  backoff: "限流等待",
  review: "待核对",
};
const statuses = {
  active: "进行中",
  unknown: "待核对",
  done: "已结算",
  failed: "请求失败",
};
const number = (n) =>
  new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(n);
const cash = (n, digits = 2) =>
  "$" +
  n.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
const compact = (n) =>
  Intl.NumberFormat("en", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(n);
const label = (id, value) => {
  $(id).textContent = value;
};
let state,
  page,
  dirty = false,
  loading = false,
  logItems = [],
  logMore = false,
  logVersion = 0;
let saving = false,
  probing = false;
let resolveId,
  notice = "",
  noticeBad = false,
  noticeUntil = 0;
function tag(element, content, className) {
  const el = document.createElement(element);
  if (content != null) el.textContent = content;
  if (className) el.className = className;
  return el;
}
function announce(msg, bad = false) {
  notice = msg;
  noticeBad = bad;
  noticeUntil = Date.now() + 10000;
  banner();
}
function banner() {
  let msg = Date.now() < noticeUntil ? notice : "";
  let warn = Boolean(msg && noticeBad);
  if (!msg && state) {
    if (state.unknown) {
      msg = `有 ${state.unknown} 笔请求待核对。请在“请求记录”中按供应商账单补录用量。`;
      warn = true;
    } else if (state.status === "backoff") {
      msg = "供应商限流，预计 " + clock(state.retry) + " 后重试。";
      warn = true;
    } else if (state.status === "tail")
      msg = "剩余预算不足以发起下一笔请求。计划将在下个周期继续。";
    else if (state.note && !state.enabled && state.note !== "已手动暂停") {
      msg = state.note;
      warn = true;
    } else if (page === "settings" && (state.enabled || state.active))
      msg = "暂停计划并等待在途请求结算后，即可保存设置。";
  }
  $("message").hidden = !msg;
  $("message").classList.toggle("warn", warn);
  label("message", msg);
}
async function api(path, body) {
  const opts =
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": state.csrf_token,
          },
          body: JSON.stringify(body),
        };
  const resp = await fetch("/api/" + path, opts);
  if (resp.status === 401) throw Error("登录已失效，请刷新页面重新登录");
  const data = await resp.json();
  if (!resp.ok) throw Error(data.error || "请求失败");
  return data;
}
function clock(ts) {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: state.timezone,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).format(ts * 1000);
}
function dayName(day) {
  return ["日", "一", "二", "三", "四", "五", "六"][
    new Date(day + "T12:00:00Z").getUTCDay()
  ];
}
function renderChart(usd) {
  const data = state.days.map((d) => (usd ? d.usd : d.tokens));
  const max = Math.max(...data);
  const power = max ? 10 ** Math.floor(Math.log10(max)) : 1;
  const ceiling = max ? Math.ceil(max / power) * power : 1;
  label("axis-max", compact(ceiling));
  label("axis-mid", compact(ceiling / 2));
  label("chart-unit", usd ? "USD" : "TOKEN");
  label("chart-legend", usd ? "每日估算金额" : "每日结算 token");
  $("empty-chart").hidden = max > 0;
  $("bars").replaceChildren(
    ...state.days.map((d, i) => {
      const cell = tag(
        "div",
        null,
        "bar-cell" + (d.day === state.today ? " today" : ""),
      );
      const bar = tag("span", null, data[i] ? "bar" : "bar zero");
      bar.style.height = (data[i] / ceiling) * 100 + "%";
      const detail =
        d.day + " · " + (usd ? cash(data[i], 4) : number(data[i]) + " tokens");
      cell.title = detail;
      cell.setAttribute("aria-label", detail);
      cell.setAttribute("role", "img");
      cell.append(bar, tag("span", "周" + dayName(d.day), "day-label"));
      return cell;
    }),
  );
}
function renderTable(container, items, full) {
  const fields = full
    ? ["时间", "模型", "状态", "输入", "缓存", "输出", "估算美元", "详情"]
    : ["时间", "模型", "状态", "Token", "估算美元"];
  const wrap = tag("div", null, "table-wrap");
  wrap.tabIndex = 0;
  wrap.setAttribute("role", "region");
  wrap.setAttribute("aria-label", full ? "请求明细" : "最近请求");
  const table = tag("table");
  const header = table.createTHead().insertRow();
  for (const field of fields) {
    const th = tag("th", field);
    th.scope = "col";
    header.append(th);
  }
  const body = table.createTBody();
  if (!items.length) {
    table.classList.add("empty-table");
    const cell = body.insertRow().insertCell();
    cell.colSpan = fields.length;
    cell.className = "empty";
    const filtered = full && $("only-unknown").checked;
    cell.append(
      icon("inbox"),
      tag("b", filtered ? "没有待核对的请求" : "暂无请求记录"),
      tag(
        "span",
        filtered
          ? "需要核对的请求会出现在这里"
          : "启动计划后，可在这里查看每笔请求的用量",
      ),
    );
  }
  for (const item of items) {
    const row = body.insertRow();
    const time =
      (full
        ? new Intl.DateTimeFormat("zh-CN", {
            timeZone: state.timezone,
            month: "2-digit",
            day: "2-digit",
          }).format(item.ts * 1000) + " "
        : "") + clock(item.ts);
    const cell = tag("th", time);
    cell.scope = "row";
    cell.dataset.label = "时间";
    row.append(cell);
    function add(value) {
      const td = row.insertCell();
      td.dataset.label = fields[row.cells.length - 1];
      td.append(value);
      return td;
    }
    add(item.model);
    add(
      tag(
        "span",
        statuses[item.status],
        "pill" + (["unknown", "failed"].includes(item.status) ? " bad" : ""),
      ),
    );
    if (full)
      [item.prompt, item.cached, item.output].forEach((v) =>
        add(v == null ? "—" : number(v)),
      );
    else add(item.prompt == null ? "—" : number(item.prompt + item.output));
    add(item.usd == null ? "—" : cash(item.usd, 4));
    if (full) {
      const td = add(item.note || "—");
      td.className = "note";
      if (item.status === "unknown") {
        const btn = tag("button", "核对", "secondary tiny");
        btn.onclick = () => {
          resolveId = item.id;
          $("resolve-form").reset();
          label("resolve-error", "");
          $("resolve-dialog").showModal();
        };
        td.append(btn);
      }
    }
  }
  wrap.append(table);
  container.replaceChildren(wrap);
}
function updateMode() {
  const usd = form.elements.mode.value === "usd";
  form.querySelectorAll("[data-mode]").forEach((el) => {
    el.hidden = (el.dataset.mode === "usd") !== usd;
    el.querySelectorAll("input").forEach((input) => {
      input.disabled = el.hidden;
    });
  });
  const style = form.elements.api_style.value;
  $("output-field-label").hidden = style !== "chat";
  form.elements.output_field.disabled = style !== "chat";
  form.elements.max_output.min = style === "chat" ? "1" : "16";
  label(
    "protocol-help",
    style === "codex"
      ? "适用于仅允许编程客户端的供应商。使用官方 CLI 生成独立编程题，完整提示词计入预算。"
      : style === "responses"
        ? "直接使用 /v1/responses，供应商需允许脚本访问。"
        : "直接使用 /v1/chat/completions，需选择模型支持的输出上限参数。",
  );
  label(
    "plan-hint",
    usd
      ? "达到目标后停止派发。让支出上限略高于目标，可为最后一笔请求预留空间。"
      : "按输入与输出 token 合计控制。剩余预算不足以预留请求时，等到下一周期继续。",
  );
}
function render() {
  if (!state) return;
  const c = state.config,
    totals = state.totals,
    usd = c.mode === "usd";
  const display = (n) => (usd ? cash(n) : number(n));
  const ratio = state.target ? (state.spent / state.target) * 100 : 0;
  const pct = ratio >= 100 ? "100%" : Math.floor(ratio * 10) / 10 + "%";
  label("period", state.week + " — " + state.days.at(-1).day.slice(5));
  label("tz", state.timezone);
  label("plan-badge", plans[state.status]);
  label("spend-label", usd ? "估算金额 · USD" : "已结算用量 · TOKEN");
  label("spend", display(state.spent));
  label("goal", display(state.target));
  label("cap", display(state.cap));
  label("held", usd ? cash(state.held_usd, 4) : number(state.held_tokens));
  label("gauge-value", pct);
  $("gauge").setAttribute("aria-label", "已完成 " + pct);
  $("arc").style.strokeDasharray = Math.min(100, ratio) + " 100";
  label("token-total", number(totals.prompt + totals.output));
  label(
    "token-split",
    "输入 " + number(totals.prompt) + " · 输出 " + number(totals.output),
  );
  label(
    "cache-rate",
    totals.reported_input
      ? number((totals.cached / totals.reported_input) * 100) + "%"
      : "—",
  );
  label(
    "cache-detail",
    totals.reported_input
      ? "已报告缓存 " +
          number(totals.cached) +
          " / " +
          number(totals.reported_input) +
          " 输入"
      : "等待接口报告缓存明细",
  );
  $("active-total").replaceChildren(
    document.createTextNode(String(state.active)),
    tag("small", " / " + c.concurrency),
  );
  label("call-detail", "本周已结算 " + number(totals.count) + " 笔请求");
  label("plan-model", c.model || "待配置");
  label("plan-mode", usd ? "美元金额" : "Token 总量");
  label("plan-reset", state.next_week + " 00:00");
  label("plan-bark", state.has_bark ? "已启用" : "未配置");
  const action = $("exec-btn");
  action.hidden = page !== "overview";
  action.replaceChildren(
    icon(!state.ready ? "plug" : state.enabled ? "pause" : "play"),
    document.createTextNode(
      !state.ready ? "配置接口" : state.enabled ? "暂停计划" : "启动计划",
    ),
  );
  $("save-btn").hidden = page !== "settings";
  $("save-btn").disabled =
    saving || probing || state.enabled || state.active > 0;
  $("test-provider").disabled =
    probing || saving || !state.ready || state.enabled || state.active > 0;
  if (!dirty) {
    for (const [key, val] of Object.entries(c))
      if (form.elements[key]) form.elements[key].value = val ?? "";
    form.elements.api_key.value = "";
    form.elements.bark_url.value = "";
    form.elements.remove_bark.checked = false;
    updateMode();
  }
  form.elements.api_key.required = !state.ready;
  form.elements.api_key.placeholder = state.ready
    ? "已保存，留空沿用现有密钥"
    : "输入提供商的 API Key";
  form.elements.bark_url.placeholder = state.has_bark
    ? "已保存，留空沿用现有地址"
    : "https://api.day.app/设备Key";
  $("remove-bark-label").hidden = !state.has_bark;
  $("test-bark").disabled = !state.has_bark;
  const last = state.last_notice;
  label(
    "delivery",
    state.pending_notices
      ? `待投递 ${state.pending_notices} 条${last?.error ? " · " + last.error : ""}`
      : last?.delivered
        ? "最近送达 " + clock(last.delivered)
        : "",
  );
  if (page === "overview") {
    renderChart(usd);
    renderTable($("recent-table"), state.recent, false);
  }
  banner();
}
async function loadLogs(append = false) {
  const version = ++logVersion;
  const before = append && logItems.length ? logItems.at(-1).id : 0;
  const result = await api(
    "logs?unknown=" + Number($("only-unknown").checked) + "&before=" + before,
  );
  if (version !== logVersion || page !== "logs") return;
  logItems = append ? logItems.concat(result.rows) : result.rows;
  logMore = result.more;
  renderTable($("full-table"), logItems, true);
  $("more-logs").hidden = !logMore;
}
async function refresh() {
  if (loading) return;
  loading = true;
  try {
    state = await api("state");
    label("online", "服务在线");
    render();
    if (
      page === "logs" &&
      logItems.length <= 40 &&
      !$("resolve-dialog").open &&
      !document.activeElement?.closest("#full-table")
    )
      await loadLogs();
  } catch (e) {
    label("online", "连接异常");
    announce(e.message, true);
  } finally {
    loading = false;
  }
}
function route() {
  const hash = location.hash.slice(1);
  page = Object.hasOwn(names, hash) ? hash : "overview";
  for (const key of Object.keys(names)) $(key).hidden = key !== page;
  document.querySelectorAll("[data-nav]").forEach((a) => {
    if (a.dataset.nav === page) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  label("page-title", names[page]);
  document.title = names[page] + " · Token Meter";
  render();
  if (page === "logs" && state)
    loadLogs().catch((e) => announce(e.message, true));
}
$("exec-btn").onclick = async () => {
  if (!state.ready) {
    location.hash = "settings";
    return;
  }
  const button = $("exec-btn");
  button.disabled = true;
  try {
    await api(state.enabled ? "pause" : "start", {});
    noticeUntil = 0;
    await refresh();
  } catch (e) {
    announce(e.message, true);
  } finally {
    button.disabled = false;
  }
};
form.oninput = () => {
  dirty = true;
  updateMode();
};
form.onsubmit = async (e) => {
  e.preventDefault();
  if (saving) return;
  saving = true;
  $("save-btn").disabled = true;
  const data = Object.fromEntries(new FormData(form));
  data.remove_bark = form.elements.remove_bark.checked;
  try {
    await api("settings", data);
    dirty = false;
    await refresh();
    announce("设置已保存");
  } catch (err) {
    announce(err.message, true);
  } finally {
    saving = false;
    render();
  }
};
$("test-provider").onclick = async () => {
  if (probing) return;
  probing = true;
  render();
  announce("正在测试已保存接口，请等待用量结算…");
  try {
    const result = await api("probe", {});
    announce(
      `连接成功 · 输入 ${result.prompt} · 输出 ${result.output} · 缓存 ${result.cached ?? "未报告"} token${result.warning ? " · " + result.warning : ""}`,
      Boolean(result.warning),
    );
  } catch (e) {
    announce(e.message, true);
  } finally {
    probing = false;
    await refresh();
    render();
  }
};
$("only-unknown").onchange = () =>
  loadLogs().catch((e) => announce(e.message, true));
$("more-logs").onclick = () =>
  loadLogs(true).catch((e) => announce(e.message, true));
$("test-bark").onclick = async () => {
  try {
    await api("bark-test", {});
    announce("测试通知已加入投递队列");
    await refresh();
  } catch (e) {
    announce(e.message, true);
  }
};
$("cancel-resolve").onclick = () => $("resolve-dialog").close();
$("resolve-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("resolve", {
      id: resolveId,
      ...Object.fromEntries(new FormData(e.target)),
    });
    $("resolve-dialog").close();
    await refresh();
    await loadLogs();
    announce("用量已按账单结算");
  } catch (err) {
    label("resolve-error", err.message);
  }
};
window.addEventListener("hashchange", () => {
  route();
  $("page-title").focus();
});
route();
refresh();
setInterval(() => {
  if (!document.hidden) refresh();
}, 4000);
