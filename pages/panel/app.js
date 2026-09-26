const bridge = window.AstrBotPluginPage;

const els = {
  meta: document.getElementById("meta"),
  peak: document.getElementById("peak-note"),
  error: document.getElementById("error"),
  list: document.getElementById("conversations"),
  catalog: document.getElementById("catalog-body"),
  refresh: document.getElementById("refresh"),
  auto: document.getElementById("auto"),
};

let rate = 7.2;
let state = null;
let timer = null;

const cny = (usd) => `¥${(Number(usd || 0) * rate).toFixed(2)}`;
const usd = (v) => `$${Number(v || 0).toFixed(4)}`;

function limitText(v) {
  const n = Number(v || 0);
  return n > 0 ? cny(n) : "不限";
}

function pct(spent, limit) {
  const n = Number(limit || 0);
  if (n <= 0) return 0;
  return Math.min((Number(spent || 0) / n) * 100, 100);
}

function barClass(spent, limit, price) {
  const p = pct(spent, limit);
  const n = Number(limit || 0);
  if (n > 0 && Number(spent || 0) + Number(price || 0) > n) return "red";
  if (p >= 80) return "red";
  if (p >= 50) return "amber";
  return "";
}

function ago(sec) {
  const s = Number(sec || 0);
  if (s < 60) return `${s} 秒前`;
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return `${Math.floor(s / 86400)} 天前`;
}

function showError(message) {
  if (!message) {
    els.error.hidden = true;
    els.error.textContent = "";
    return;
  }
  els.error.hidden = false;
  els.error.textContent = message;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderModelRow(conv, m) {
  const row = el("div", "row");

  const top = el("div", "row-top");
  top.appendChild(el("span", "row-name", m.name));
  const bits = [`${m.used} 次`];
  if (m.price_usd > 0) {
    bits.push(`已花 ${cny(m.spent_usd)} / 上限 ${limitText(m.user_limit_usd)}`);
    bits.push(`剩 ${cny(Math.max(m.user_limit_usd - m.spent_usd, 0))}`);
  } else {
    bits.push("免费");
  }
  top.appendChild(el("span", "row-nums", bits.join(" · ")));
  row.appendChild(top);

  const bar = el("div", "bar");
  const fill = el("i");
  const cls = barClass(m.spent_usd, m.user_limit_usd, m.price_usd);
  if (cls) fill.className = cls;
  fill.style.width = `${pct(m.spent_usd, m.user_limit_usd).toFixed(1)}%`;
  bar.appendChild(fill);
  row.appendChild(bar);

  if (m.pool_limit_usd > 0) {
    const pool = el("div", "bar pool");
    const pfill = el("i");
    pfill.style.width = `${pct(m.pool_spent_usd, m.pool_limit_usd).toFixed(1)}%`;
    pool.appendChild(pfill);
    row.appendChild(pool);
    row.appendChild(
      el(
        "div",
        "card-meta",
        `本 bot 总池 ${cny(m.pool_spent_usd)} / ${limitText(m.pool_limit_usd)}（剩 ${cny(
          Math.max(m.pool_limit_usd - m.pool_spent_usd, 0),
        )}）`,
      ),
    );
  }

  const actions = el("div", "row-actions");
  const reset = el("button", "btn ghost", "重置该模型额度");
  reset.addEventListener("click", () => resetModel(conv, m, "conversation"));
  actions.appendChild(reset);

  const resetBot = el("button", "btn ghost", "重置本 bot 全部对话");
  resetBot.addEventListener("click", () => resetModel(conv, m, "bot"));
  actions.appendChild(resetBot);
  row.appendChild(actions);
  return row;
}

function renderConversation(conv) {
  const card = el("div", "card");

  const head = el("div", "card-head");
  head.appendChild(el("div", "card-title", conv.label));
  head.appendChild(el("div", "card-meta", `${ago(conv.last_seen_ago)}活跃`));
  card.appendChild(head);

  const badges = el("div", "badges");
  badges.appendChild(
    el("span", "badge current", conv.model ? `模型：${conv.model}` : "模型：未知"),
  );
  badges.appendChild(
    el("span", "badge", `思考强度：${conv.think || "未设置"}`),
  );
  if (conv.peak) badges.appendChild(el("span", "badge peak", conv.peak));
  badges.appendChild(el("span", "badge", `${conv.users} 人在用`));
  badges.appendChild(
    el("span", "badge", `合计已花 ${cny(conv.personal_total_usd)}`),
  );
  card.appendChild(badges);

  if (!conv.models.length) {
    card.appendChild(el("div", "card-meta", "今日还没有消费记录。"));
    return card;
  }
  for (const m of conv.models) card.appendChild(renderModelRow(conv, m));
  return card;
}

function renderCatalog(catalog) {
  els.catalog.textContent = "";
  for (const m of catalog) {
    const tr = document.createElement("tr");
    tr.appendChild(el("td", null, m.name));
    tr.appendChild(el("td", null, m.price_usd > 0 ? usd(m.price_usd) : "免费"));
    tr.appendChild(el("td", null, m.peak ? "有（2×）" : "—"));
    tr.appendChild(el("td", null, limitText(m.user_limit_usd)));
    tr.appendChild(el("td", null, limitText(m.pool_limit_usd)));
    tr.appendChild(el("td", null, m.think || "未设置"));
    els.catalog.appendChild(tr);
  }
}

function render(data) {
  state = data;
  rate = Number(data.rate || 7.2);
  els.meta.textContent = [
    `日期 ${data.date}`,
    `1$≈¥${rate.toFixed(2)}`,
    `每人每日总额 ${limitText(data.limits.user_total_usd)}`,
    `${data.conversations.length} 个对话`,
    data.opencode_only ? "仅 OpenCode 模型" : "全部提供商",
  ].join(" · ");
  els.peak.textContent = data.peak_note || "";

  els.list.textContent = "";
  if (!data.conversations.length) {
    els.list.appendChild(
      el("div", "card empty", "还没有记录到对话。让机器人先回复一条消息即可。"),
    );
  } else {
    for (const conv of data.conversations) els.list.appendChild(renderConversation(conv));
  }
  renderCatalog(data.catalog || []);
}

async function load() {
  try {
    const data = await bridge.apiGet("panel/overview");
    showError("");
    render(data);
  } catch (error) {
    showError(`加载失败：${error.message}`);
  }
}

async function resetModel(conv, m, scope) {
  const label =
    scope === "bot"
      ? `确定重置本 bot 上「${m.name}」在所有对话的用量？`
      : `确定重置这个对话在「${m.name}」上的用量？`;
  if (!window.confirm(label)) return;
  try {
    await bridge.apiPost("panel/reset", {
      provider_id: m.provider_id,
      umo: conv.umo,
      scope,
    });
    await load();
  } catch (error) {
    showError(`重置失败：${error.message}`);
  }
}

function setupAuto() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
  if (els.auto.checked) timer = setInterval(load, 10000);
}

els.refresh.addEventListener("click", load);
els.auto.addEventListener("change", setupAuto);

await bridge.ready();
await load();
setupAuto();
