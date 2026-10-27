'use strict';

/* ---------- icons (24px stroke set) ---------- */
const PATHS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  shield: '<path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.6-4.5M4 13a8 8 0 0 0 14.6 4.5"/><path d="M5 3v4h4M19 21v-4h-4"/>',
  radar: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><path d="M12 12l6-6"/>',
  sparkle: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 17l.7 1.8 1.8.7-1.8.7L19 22l-.7-1.8-1.8-.7 1.8-.7z"/>',
  upgrade: '<path d="M12 19V6M6 12l6-6 6 6"/><path d="M5 21h14"/>',
  more: '<circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/>',
  qr: '<rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><path d="M14 14h2v2M20 14v6h-6M17 17h3"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13 7l4 4"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  rotate: '<path d="M4 12a8 8 0 1 0 3-6.2"/><path d="M4 4v5h5"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
  alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17h.01"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  gauge: '<path d="M4 18a9 9 0 1 1 16 0"/><path d="M12 14l4-5"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
  server: '<rect x="4" y="4" width="16" height="7" rx="1.6"/><rect x="4" y="13" width="16" height="7" rx="1.6"/><path d="M8 7.5h.01M8 16.5h.01"/>',
  phone: '<rect x="7" y="2.5" width="10" height="19" rx="2.4"/><path d="M11 18.5h2"/>',
  laptop: '<rect x="4" y="5" width="16" height="11" rx="1.6"/><path d="M2 19h20"/>',
  route: '<circle cx="6" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M8 19h8a3.5 3.5 0 0 0 0-7H8a3.5 3.5 0 0 1 0-7h8"/>',
  layers: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>',
  android: '<path d="M5 17a7 7 0 0 1 14 0z"/><path d="M8 10.5 6.5 8M16 10.5 17.5 8"/><path d="M9.5 14h.01M14.5 14h.01"/>',
};
const icon = (name, size = 16) =>
  `<span class="i" style="width:${size}px;height:${size}px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${PATHS[name]}</svg></span>`;

/* ---------- state & helpers ---------- */
const S = { token: '', servers: [], version: '', current: null, status: {}, results: {}, busy: {}, menu: null };
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const gb = (b) => { const v = (b || 0) / 1e9; return (v >= 100 || v === Math.round(v) ? Math.round(v * 10) / 10 : v.toFixed(1)) + ' GB'; };
const pct = (a, b) => (b ? Math.min(100, (a / b) * 100) : 0);
const PALETTE = [['#6366f1', '#8b5cf6'], ['#0ea5e9', '#6366f1'], ['#f97316', '#ec4899'], ['#10b981', '#0ea5e9'],
  ['#ec4899', '#8b5cf6'], ['#f59e0b', '#f97316'], ['#14b8a6', '#22c55e'], ['#8b5cf6', '#d946ef']];
const hue = (name) => PALETTE[[...name].reduce((a, c) => a + c.charCodeAt(0), 0) % PALETTE.length];

async function api(method, path, body) {
  const r = await fetch('/api' + path, {
    method,
    headers: { 'X-Gowest-Token': S.token, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({ error: '本地服务没有响应，请确认终端里的 gowest 还在运行' }));
  if (!r.ok) throw new Error(data.error || '请求失败');
  return data;
}

function toast(text, bad = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (bad ? ' bad' : '');
  el.innerHTML = icon(bad ? 'alert' : 'shield') + `<span>${esc(text)}</span>`;
  $('#toasts').append(el);
  setTimeout(() => el.remove(), bad ? 6500 : 3200);
}

/* ---------- modal ---------- */
function modal(html, { wide = false } = {}) {
  const layer = $('#layer');
  layer.innerHTML = `<div class="modal${wide ? ' wide' : ''}" role="dialog">${html}</div>`;
  layer.hidden = false;
  const first = layer.querySelector('input,select');
  if (first) setTimeout(() => first.focus(), 30);
  return layer.firstElementChild;
}
function closeModal() { const layer = $('#layer'); layer.hidden = true; layer.innerHTML = ''; }
const head = (title, sub = '') =>
  `<div class="modal-head"><h3>${esc(title)}</h3><button class="btn ghost icon x" data-act="close">${icon('x')}</button></div>` +
  (sub ? `<div class="modal-body" style="padding-top:2px;padding-bottom:0"><p>${sub}</p></div>` : '');

function formModal(title, sub, fields, submitText, onSubmit) {
  const body = fields.map((f) => `
    <div class="field" ${f.span ? 'style="grid-column:1/-1"' : ''}>
      <label>${esc(f.label)}</label>
      ${f.options
        ? `<select name="${f.name}">${f.options.map(([v, t]) => `<option value="${v}" ${v === f.value ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>`
        : `<input name="${f.name}" type="${f.type || 'text'}" value="${esc(f.value || '')}" placeholder="${esc(f.placeholder || '')}" ${f.readonly ? 'readonly' : ''} autocomplete="off" spellcheck="false">`}
      ${f.hint ? `<div class="hint">${f.hint}</div>` : ''}
    </div>`).join('');
  const el = modal(`${head(title, sub)}<form class="modal-body form ${fields.length > 2 ? 'two' : ''}">${body}</form>
    <div class="modal-foot"><button class="btn" data-act="close">取消</button><button class="btn primary" data-submit>${esc(submitText)}</button></div>`);
  const form = $('form', el);
  const submit = async (e) => {
    e.preventDefault();
    const btn = $('[data-submit]', el);
    const values = Object.fromEntries(new FormData(form).entries());
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span>' + esc(submitText);
    try {
      await onSubmit(values);
    } catch (err) {
      toast(err.message, true);
      btn.disabled = false;
      btn.textContent = submitText;
    }
  };
  form.addEventListener('submit', submit);
  $('[data-submit]', el).addEventListener('click', submit);
}

function confirmModal(title, text, okText, danger, onOk) {
  const el = modal(`${head(title)}<div class="modal-body"><p>${text}</p></div>
    <div class="modal-foot"><button class="btn" data-act="close">取消</button>
    <button class="btn ${danger ? 'danger' : 'primary'}" data-ok>${esc(okText)}</button></div>`);
  $('[data-ok]', el).addEventListener('click', async (e) => {
    e.target.disabled = true;
    e.target.innerHTML = '<span class="spin"></span>' + esc(okText);
    try { await onOk(); } catch (err) { toast(err.message, true); e.target.disabled = false; e.target.textContent = okText; }
  });
}

/* ---------- data ---------- */
async function loadServers(select) {
  const data = await api('GET', '/servers');
  S.servers = data.servers;
  S.version = data.kit_version;
  if (select) S.current = select;
  if (!S.servers.find((s) => s.id === S.current)) S.current = S.servers[0]?.id || null;
  render();
  if (S.current && !S.status[S.current]) loadStatus(S.current);
}

async function loadStatus(id) {
  S.status[id] = { loading: true };
  render();
  try {
    S.status[id] = await api('GET', `/servers/${id}/status`);
  } catch (err) {
    S.status[id] = { error: err.message };
  }
  render();
}

/* ---------- views ---------- */
function render() {
  renderSide();
  const main = $('#main');
  if (!S.servers.length) { main.innerHTML = heroView(); return; }
  const server = S.servers.find((s) => s.id === S.current);
  main.innerHTML = serverView(server);
}

function dotClass(st) {
  if (!st || st.loading) return '';
  if (st.error) return 'bad';
  if (st.installed === false) return 'idle';
  return healthy(st) ? 'good' : 'bad';
}
const ESSENTIAL = ['la-vpn.service', 'la-vpn-subscription.service', 'la-vpn-subinfo.timer'];
const healthy = (st) => ESSENTIAL.every((n) => st.services?.[n] === 'active');

function renderSide() {
  $('#servers').innerHTML = S.servers.map((s) => `
    <button class="srv ${s.id === S.current ? 'on' : ''}" data-act="select" data-id="${s.id}">
      <span class="dot ${dotClass(S.status[s.id])}"></span>
      <span><b>${esc(s.name)}</b><small>${esc(s.ip)}</small></span>
    </button>`).join('') || '<div class="muted" style="padding:6px 10px;font-size:13px">还没有服务器</div>';
}

function heroView() {
  return `<div class="card hero">
    <div class="logo"><svg viewBox="0 0 64 64" width="36" height="36"><circle cx="15" cy="32" r="6" fill="#fff"/><path d="M15 32h32M36 19l13 13-13 13" stroke="#fff" stroke-width="7" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></div>
    <h1>把第一台服务器加进来</h1>
    <p>这个页面只在你的电脑上运行，通过 SSH 管理服务器。已经装好 gowest 的服务器会直接显示状态；空白服务器可以在这里一键安装。</p>
    <div class="steps">
      <div class="step"><em>1</em><b>准备服务器</b>空白 Ubuntu 24.04，能用 SSH 密钥登录</div>
      <div class="step"><em>2</em><b>开放端口</b>安全组放行 TCP/UDP 443（多人：20001-20032）和 TCP 80</div>
      <div class="step"><em>3</em><b>添加并安装</b>填 IP，点安装，手机扫码导入</div>
    </div>
    <button class="btn primary" data-act="add-server">${icon('plus')}添加服务器</button>
  </div>`;
}

function serverHead(server, st) {
  let pill = '<span class="chip"><span class="spin"></span>读取中</span>';
  if (st?.error) pill = '<span class="chip bad">连接失败</span>';
  else if (st?.installed === false) pill = '<span class="chip warn">未安装</span>';
  else if (st && !st.loading) pill = healthy(st)
    ? '<span class="chip good"><span class="pulse"></span>运行中</span>'
    : '<span class="chip bad">部分服务异常</span>';
  const installed = st && !st.loading && !st.error && st.installed !== false;
  const newer = installed && S.version && st.kit_version !== S.version;
  const busy = (k) => S.busy[server.id + k];
  const btn = (k, ic, text, primary) => `<button class="btn ${primary ? 'primary' : ''}" data-act="${k}" ${busy(k) ? 'disabled' : ''}>
    ${busy(k) ? '<span class="spin"></span>' : icon(ic)}${text}</button>`;
  return `<div class="head">
    <div>
      <h1>${esc(server.name)}</h1>
      <div class="chips">${pill}<span class="chip mono">${esc(server.ip)}</span>
        ${installed ? `<span class="chip">gowest ${esc(st.kit_version)}</span><span class="chip">sing-box ${esc(st.sing_box)}</span>` : ''}
        ${newer ? `<span class="chip warn">可升级到 ${esc(S.version)}</span>` : ''}</div>
    </div>
    <div class="actions">
      ${installed ? btn('check', 'radar', '检查连通') + btn('linecheck', 'route', '线路体检') + btn('unlock', 'sparkle', '解锁检测') + btn('upgrade', 'upgrade', '升级', newer) : ''}
      <button class="btn icon" data-act="refresh" title="刷新">${icon('refresh')}</button>
      <div class="menu-wrap"><button class="btn icon" data-act="menu" data-menu="server" title="更多">${icon('more')}</button>
        ${S.menu === 'server' ? `<div class="menu">
          <button data-act="remove-server">${icon('x')}从列表移除</button>
          ${installed ? `<button class="danger" data-act="rollback">${icon('trash')}卸载 gowest…</button>` : ''}</div>` : ''}
      </div>
    </div>
  </div>`;
}

function serverView(server) {
  const st = S.status[server.id];
  let body;
  if (!st || st.loading) body = skeleton();
  else if (st.error) body = `<div class="card" style="padding:26px 24px">
      <div style="display:flex;gap:14px;align-items:flex-start"><span class="mark fail">!</span>
      <div><b>无法读取这台服务器</b><p class="muted" style="margin:6px 0 16px">${esc(st.error)}</p>
      <button class="btn" data-act="refresh">${icon('refresh')}重试</button></div></div></div>`;
  else if (st.installed === false) body = installView(server);
  else body = dashboard(server, st);
  return serverHead(server, st) + body;
}

function skeleton() {
  const block = (h) => `<div class="skel" style="height:${h}px"></div>`;
  return `<div class="grid">${[1, 2, 3, 4].map(() => `<div class="card stat">${block(14)}<div style="height:16px"></div>${block(28)}<div style="height:10px"></div>${block(10)}</div>`).join('')}</div>
    <div class="card" style="padding:22px">${block(18)}<div style="height:18px"></div>${[1, 2, 3].map(() => block(34)).join('<div style="height:12px"></div>')}</div>`;
}

function installView(server) {
  return `<div class="card">
    <div class="card-head"><h2>在这台服务器上安装 gowest</h2><span class="sub">空白 Ubuntu 24.04 · 约 2–5 分钟</span></div>
    <form class="modal-body form two" id="install-form" style="padding:4px 22px 6px">
      <div class="field"><label>套餐带宽</label><input name="bandwidth" placeholder="100Mbps" value="100Mbps">
        <div class="hint">云厂商标注的带宽，如 30Mbps、1Gbps；以后可以改</div></div>
      <div class="field"><label>共用份数或名字</label><input name="users" placeholder="回车 = 1 份；如 3 或 alice,bob,carol">
        <div class="hint">多人时每人一个订阅链接，带宽忙时均分</div></div>
      <div class="field"><label>每月流量上限</label><input name="quota" placeholder="如 1TB；留空 = 不设置"></div>
      <div class="field"><label>超出上限后</label><select name="on_exceed"><option value="warn">只提醒</option><option value="stop">暂停代理到下个月</option></select></div>
      <div class="field"><label>节点标签</label><input name="prefix" value="LA" placeholder="LA"><div class="hint">区分多台服务器的节点名，如 LA、DC</div></div>
      <div class="field"><label>稳定性增强</label><select name="stable"><option value="on">开启：端口跳跃 + 备用协议 TUIC / AnyTLS（推荐）</option><option value="off">关闭：只用 Hy2 + VLESS，需要开的端口最少</option></select>
        <div class="hint">晚高峰运营商限速单个 UDP 端口时，端口跳跃能明显改善；以后可以随时开关</div></div>
    </form>
    <div class="banner" style="margin:10px 22px 0">${icon('alert')}<span>开始前请在云控制台的安全组放行：<b>一个人用</b> TCP/UDP 443、2443，UDP 24576-24639；<b>多人共用</b> TCP/UDP 20001-27000；以及订阅用的 TCP 80。关闭稳定性增强时只需 TCP/UDP 443（多人 20001-20032）和 TCP 80。订阅使用 HTTP，会明文传输订阅地址。</span></div>
    <div class="modal-foot"><button class="btn primary" data-act="install">${icon('server')}开始安装</button></div>
  </div>`;
}

function dashboard(server, st) {
  const t = st.traffic;
  const used = t.used_bytes;
  const p = pct(used || 0, t.quota_bytes);
  const paused = st.users.filter((u) => u.paused).length;
  const okServices = ESSENTIAL.filter((n) => st.services[n] === 'active').length;
  const fair = st.fair_share
    ? (st.fair_share_result === 'applied' ? '按链接公平分配 · 生效中' : '公平分配未生效：' + st.fair_share_result)
    : '一个人用 · Hy2 按带宽发送';
  const stats = `<div class="grid">
    <div class="card stat"><div class="label">${icon('activity')}本月流量</div>
      <div class="value">${used == null ? '<span class="muted" style="font-size:18px">统计中</span>' : gb(used).replace(' GB', '')}<small>${used == null ? '' : 'GB'}${t.quota_bytes ? ' / ' + gb(t.quota_bytes) : ''}</small></div>
      ${t.quota_bytes ? `<div class="bar ${p > 90 ? 'bad' : p > 75 ? 'warn' : ''}"><i style="width:${p}%"></i></div>` : ''}
      <div class="meta">${t.quota_bytes ? (t.on_exceed === 'stop' ? '超额后暂停' : '超额只提醒') : '未设上限'} · ${t.metering === 'outbound' ? '只计出方向' : '进 + 出'}${t.paused_month ? ' · <b style="color:var(--bad)">已暂停</b>' : ''}</div>
      <button class="btn ghost icon edit" data-act="edit-traffic" title="修改">${icon('edit')}</button></div>
    <div class="card stat"><div class="label">${icon('gauge')}带宽</div>
      <div class="value">${st.bandwidth_mbps}<small>Mbps</small></div><div class="meta">${esc(fair)}</div>
      <button class="btn ghost icon edit" data-act="edit-bandwidth" title="修改">${icon('edit')}</button></div>
    <div class="card stat"><div class="label">${icon('link')}订阅链接</div>
      <div class="value">${st.single ? 1 : st.users.length}<small>个</small></div>
      <div class="meta">${st.single ? '一个人用（端口 443）' : `${st.users.length - paused} 个正常${paused ? ` · <b style="color:var(--bad)">${paused} 个已暂停</b>` : ''}`}</div></div>
    <div class="card stat"><div class="label">${icon('cpu')}服务</div>
      <div class="value">${okServices}<small>/ ${ESSENTIAL.length} 正常</small></div>
      <div class="meta">代理 · 订阅 · 流量统计</div></div>
  </div>`;
  const warnings = st.warnings.length
    ? `<div class="banner">${icon('alert')}<span>${st.warnings.map(esc).join('<br>')}</span></div>` : '';
  return `<div class="stack">${stats}${warnings}${usersCard(st)}${optionsCard(st)}${resultsCards(server)}</div>`;
}

function usersCard(st) {
  if (st.single) {
    return `<div class="card"><div class="card-head"><h2>订阅</h2><span class="sub">一个人用：所有设备导入同一份订阅</span>
      <div class="right"><button class="btn primary" data-act="qr" data-user="">${icon('qr')}导入到设备</button></div></div></div>`;
  }
  const rows = st.users.map((u) => {
    const [a, b] = hue(u.name);
    const p = pct(u.used_bytes, u.quota_bytes);
    return `<tr>
      <td><div class="who"><span class="avatar" style="background:linear-gradient(135deg,${a},${b})">${esc(u.name[0])}</span>
        <div><b>${esc(u.name)}</b><small>端口 ${u.port}</small></div></div></td>
      <td class="usage">${u.quota_bytes ? `<div class="bar ${u.paused === 'quota' ? 'bad' : p > 80 ? 'warn' : ''}"><i style="width:${Math.max(p, 2)}%"></i></div>` : '<div class="bar empty"><i></i></div>'}
        <small>${gb(u.used_bytes)}${u.quota_bytes ? ' / ' + gb(u.quota_bytes) : ' · 不限额'}</small></td>
      <td>${u.cap_mbps ? u.cap_mbps + ' Mbps' : '<span class="muted">不限</span>'}</td>
      <td>${u.expire_date ? esc(u.expire_date) : '<span class="muted">长期</span>'}</td>
      <td>${u.paused ? `<span class="badge paused">${u.paused === 'quota' ? '超额已暂停' : '已到期'}</span>` : '<span class="badge">正常</span>'}</td>
      <td><div class="row-actions">
        <button class="btn ghost icon" data-act="qr" data-user="${esc(u.name)}" title="导入二维码">${icon('qr')}</button>
        <button class="btn ghost icon" data-act="edit-user" data-user="${esc(u.name)}" title="修改">${icon('edit')}</button>
        <div class="menu-wrap"><button class="btn ghost icon" data-act="menu" data-menu="user:${esc(u.name)}">${icon('more')}</button>
          ${S.menu === 'user:' + u.name ? `<div class="menu">
            <button data-act="reset-user" data-user="${esc(u.name)}">${icon('rotate')}重置链接</button>
            <button class="danger" data-act="delete-user" data-user="${esc(u.name)}">${icon('trash')}删除</button></div>` : ''}
        </div></div></td>
    </tr>`;
  }).join('');
  return `<div class="card">
    <div class="card-head"><h2>订阅链接</h2><span class="sub">每人一个链接 · 忙时均分、闲时拉满</span>
      <div class="right"><button class="btn primary" data-act="add-user">${icon('plus')}添加链接</button></div></div>
    <table class="users"><thead><tr><th>名字</th><th>本月用量</th><th>速度上限</th><th>到期</th><th>状态</th><th></th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

const OPTIONS = [
  ['hopping', 'Hy2 端口跳跃', '每 30 秒换一个 UDP 端口，绕开运营商对单个端口的限速；需要安全组放行一段 UDP 端口'],
  ['tuic', 'TUIC（备用，UDP）', '另一种 UDP 协议，Hy2 被针对时的退路'],
  ['anytls', 'AnyTLS（备用，TCP）', '另一种 TCP 协议，VLESS 被针对时的退路'],
  ['obfs', 'Hy2 混淆（salamander）', '把 Hy2 流量打乱成随机字节；关闭后看起来像普通 HTTP/3'],
];

function optionsCard(st) {
  const o = st.options || {};
  const on = { hopping: o.port_hopping, tuic: (o.extra_protocols || []).includes('tuic'),
    anytls: (o.extra_protocols || []).includes('anytls'), obfs: o.hy2_obfs };
  const rows = OPTIONS.map(([key, title, text]) => `<div class="opt">
      <div><b>${esc(title)}</b><small>${esc(text)}</small></div>
      <button class="switch ${on[key] ? 'on' : ''}" data-act="toggle" data-key="${key}" role="switch" aria-checked="${!!on[key]}"><i></i></button>
    </div>`).join('');
  return `<div class="card"><div class="card-head"><h2>线路与协议</h2><span class="sub">客户端更新订阅后生效 · 失败自动恢复</span></div>
    <div class="opts">${rows}</div>
    <div class="ports"><span class="muted">云安全组需要放行</span>${(st.cloud_ports || []).map((p) => `<span class="chip mono">${esc(p)}</span>`).join('')}</div></div>`;
}

function resultsCards(server) {
  const r = S.results[server.id] || {};
  const MARK = { true: ['ok', '✓'], false: ['fail', '✗'], null: ['na', '·'], ok: ['ok', '✓'], warn: ['warn', '!'], fail: ['fail', '✗'] };
  const row = (key, name, text) => { const [c, m] = MARK[key]; return `<div class="result"><span class="mark ${c}">${m}</span><b>${esc(name)}</b><span>${esc(text)}</span></div>`; };
  let html = '';
  if (r.check) html += `<div class="card"><div class="card-head"><h2>连通性检查</h2><span class="sub">从这台电脑访问服务器，看到的就是客户端看到的</span></div>
    <div class="results">${r.check.map((x) => row(String(x.ok), x.name, x.text)).join('')}</div></div>`;
  if (r.line) {
    const l = r.line, lat = l.latency, sp = l.speed;
    const level = { 'CN2 GIA': 'ok', '9929': 'ok', 'CMI': 'ok', 'CN2 GT': 'warn' }[l.route.key] || (l.route.key ? 'warn' : 'na');
    html += `<div class="card"><div class="card-head"><h2>线路体检</h2><span class="sub">从这台电脑直连服务器，不经过任何代理${l.peak ? ' · 晚高峰实测' : ' · 建议晚上 8–11 点再测一次'}</span></div>
      <div class="results">
        ${row(level, '线路', l.route.label + ' —— ' + l.route.verdict)}
        ${'avg' in lat ? row(lat.loss > 5 ? 'fail' : lat.loss > 0 || lat.jitter > 30 ? 'warn' : 'ok', '延迟', `平均 ${lat.avg} ms（${lat.min}–${lat.max}），抖动 ${lat.jitter} ms，丢包 ${lat.loss}%`) : row('fail', '延迟', '全部超时，代理端口连不上')}
        ${sp ? ('mbps' in sp ? row(sp.mbps >= 50 ? 'ok' : sp.mbps >= 10 ? 'warn' : 'fail', '下载', `${sp.mbps} Mbps（单连接，${sp.megabytes} MB）`) : row('fail', '下载', '失败：' + sp.error)) : ''}
      </div>
      ${l.route.path.length ? `<div class="hops">${l.route.path.map((h) => `<span class="chip mono">${esc(h)}</span>`).join('<span class="muted">›</span>')}</div>` : ''}</div>`;
  }
  if (r.unlock) html += `<div class="card"><div class="card-head"><h2>解锁检测</h2><span class="sub">结果代表服务器出口 IP，所有客户端都一样</span></div>
    <div class="ipline"><span>出口 IP <b class="mono">${esc(r.unlock.ip.ip)}</b></span><span>地区 <b>${esc(r.unlock.ip.country)} · ${esc(r.unlock.ip.city)}</b></span><span>运营商 <b>${esc(r.unlock.ip.org)}</b></span></div>
    <div class="results">${r.unlock.checks.map((x) => row(x.level, x.name, x.text)).join('')}</div></div>`;
  return html;
}

/* ---------- QR ---------- */
function qrSvg(text) {
  const q = qrcode(0, 'M');
  q.addData(text);
  q.make();
  return q.createSvgTag({ cellSize: 4, margin: 0, scalable: true });
}

async function showQr(user) {
  const el = modal(`${head('导入到设备' + (user ? ' · ' + user : ''))}<div class="modal-body"><div class="skel" style="height:240px"></div></div>`, { wide: true });
  let data;
  try {
    data = await api('GET', `/servers/${S.current}/urls?user=${encodeURIComponent(user)}`);
  } catch (err) { closeModal(); toast(err.message, true); return; }
  const tabs = [['ios', 'phone', 'iPhone'], ['android', 'android', 'Android'], ['desktop', 'laptop', '电脑']];
  const copy = (label, value) => `<div class="copyrow"><span class="lbl">${esc(label)}</span><code>${esc(value)}</code>
    <button class="btn icon" data-copy="${esc(value)}" title="复制">${icon('copy')}</button></div>`;
  const pane = {
    ios: `<div class="qr-wrap"><div class="qr">${qrSvg(data.files['nodes.txt'])}</div><div>
      <ol class="qr-steps"><li>打开 Shadowrocket，点首页右上角的扫码图标</li><li>扫描左边的二维码，添加节点订阅</li>
      <li>在"配置"里添加下面的分流配置并使用</li></ol>
      ${copy('日常分流', data.files['shadowrocket.conf'])}${copy('全公网', data.files['shadowrocket-us.conf'])}</div></div>`,
    android: `<div class="qr-wrap"><div class="qr">${qrSvg(data.clashmeta.normal)}</div><div>
      <ol class="qr-steps"><li>先安装 Clash Meta for Android</li><li>用手机相机扫左边的二维码，选择用 CMFA 打开</li>
      <li>保存配置后回首页点"已停止"启动</li></ol>
      ${copy('全公网', data.clashmeta.strict)}${copy('订阅地址', data.files['normal.yaml'])}</div></div>`,
    desktop: `<p style="margin-top:14px">在装好 Clash Verge Rev 的电脑上，点击链接即可导入；也可以复制订阅地址手动添加。</p>
      <div style="display:flex;gap:10px;margin:6px 0 8px"><a class="btn primary" href="${esc(data.clash.normal)}">${icon('laptop')}导入日常分流</a>
      <a class="btn" href="${esc(data.clash.strict)}">导入全公网</a></div>
      ${copy('日常分流', data.files['normal.yaml'])}${copy('全公网', data.files['strict.yaml'])}`,
  };
  const draw = (k) => {
    $('.modal-body', el).innerHTML = `<div class="seg">${tabs.map(([id, ic, t]) => `<button class="${id === k ? 'on' : ''}" data-tab="${id}">${t}</button>`).join('')}</div>
      ${pane[k]}<div class="secret">${icon('alert')}这些链接和二维码就是登录凭据，只发给本人，不要截图外传。</div>`;
    el.querySelectorAll('[data-tab]').forEach((b) => b.addEventListener('click', () => draw(b.dataset.tab)));
  };
  draw('ios');
}

/* ---------- jobs ---------- */
function runJob(jobId, title, server) {
  const el = modal(`${head(title)}<div class="modal-body"><div class="progress">${'<i></i>'.repeat(8)}</div>
    <div class="term" id="term"></div></div><div class="modal-foot"><span class="muted" id="job-state" style="margin-right:auto">进行中，请不要关闭终端里的 gowest……</span>
    <button class="btn" data-act="close" id="job-close">在后台继续</button></div>`, { wide: true });
  const term = $('#term', el);
  let next = 0;
  let stage = 0;
  const bars = el.querySelectorAll('.progress i');
  const tick = async () => {
    let d;
    try { d = await api('GET', `/jobs/${jobId}?from=${next}`); } catch (err) { toast(err.message, true); return; }
    next = d.next;
    for (const line of d.lines) {
      const m = line.match(/\[(开始|完成)\] (\d)\/8/);
      if (m) stage = Math.max(stage, Number(m[2]) - (m[1] === '开始' ? 0.5 : 0));
      const cls = /✗|ERROR|失败|错误/.test(line) ? 'err' : /✓|\[完成\]|完成/.test(line) ? 'ok' : /\[开始\]/.test(line) ? 'stage' : /^\s*$/.test(line) ? 'dim' : '';
      term.insertAdjacentHTML('beforeend', `<div class="${cls}">${esc(line) || '&nbsp;'}</div>`);
    }
    bars.forEach((b, i) => { b.className = i + 1 <= stage ? 'done' : i + 0.5 === stage ? 'run' : ''; });
    if (d.lines.length) term.scrollTop = term.scrollHeight;
    if (!d.done) { setTimeout(tick, 900); return; }
    $('#job-state', el).innerHTML = d.ok ? '<b style="color:var(--good)">已完成</b>' : '<b style="color:var(--bad)">没有成功，详情见上面的输出</b>';
    $('#job-close', el).textContent = '关闭';
    if (d.ok) bars.forEach((b) => { b.className = 'done'; });
    else bars.forEach((b) => { if (b.className === 'run' || (!b.className && b === bars[Math.ceil(stage) - 1])) b.className = 'fail'; });
    loadStatus(server);
  };
  tick();
}

/* ---------- actions ---------- */
const server = () => S.servers.find((s) => s.id === S.current);

async function busyRun(key, fn) {
  const id = S.current;
  S.busy[id + key] = true;
  render();
  try { await fn(id); } catch (err) { toast(err.message, true); }
  S.busy[id + key] = false;
  render();
}

const ACTIONS = {
  close: closeModal,
  select: (el) => { S.current = el.dataset.id; S.menu = null; render(); if (!S.status[S.current] || S.status[S.current].error) loadStatus(S.current); },
  refresh: () => loadStatus(S.current),
  menu: (el) => { S.menu = S.menu === el.dataset.menu ? null : el.dataset.menu; render(); },
  'add-server': () => formModal('添加服务器', '用 SSH 密钥登录（和你在终端里 ssh 一样）。已装好 gowest 的服务器会直接显示状态。', [
    { name: 'target', label: 'SSH 目标', placeholder: 'root@203.0.113.7', hint: '也可以填 ~/.ssh/config 里的别名', span: true },
    { name: 'name', label: '名称', placeholder: '洛杉矶' },
    { name: 'ip', label: '公网 IP', placeholder: '目标是 IP 时可留空' },
  ], '连接并添加', async (v) => {
    const added = await api('POST', '/servers', v);
    closeModal();
    toast('已添加 ' + added.name);
    S.status[added.id] = null;
    await loadServers(added.id);
  }),
  'remove-server': () => confirmModal('从列表移除', `只是从这个页面移除 <b>${esc(server().name)}</b>，服务器上的 gowest 不受影响，以后可以再加回来。`, '移除', true, async () => {
    await api('DELETE', '/servers/' + S.current);
    closeModal();
    S.menu = null;
    await loadServers();
  }),
  rollback: () => { S.menu = null; render(); formModal('卸载 gowest', '停止代理和订阅服务，撤销 gowest 做的网络和防火墙改动。密钥和配置会归档到服务器的 /var/lib/la-vpn-archives，可以恢复；APT 安装的软件包保留。所有人的订阅都会失效。', [
    { name: 'confirm', label: '输入这台服务器的 IP 确认', placeholder: server().ip, span: true },
  ], '确认卸载', async (v) => {
    await api('POST', `/servers/${S.current}/rollback`, v);
    closeModal(); toast('已卸载，配置已归档'); loadStatus(S.current);
  }); },
  check: () => busyRun('check', async (id) => {
    const r = await api('POST', `/servers/${id}/check`);
    S.results[id] = { ...(S.results[id] || {}), check: r.rows };
    toast(r.rows.every((x) => x.ok !== false) ? '端口和订阅都能从外部访问' : '有端口从外部连不上，请检查安全组', r.rows.some((x) => x.ok === false));
  }),
  linecheck: () => busyRun('linecheck', async (id) => {
    toast('正在体检：路由、延迟、下载，大约一分钟……');
    const r = await api('POST', `/servers/${id}/linecheck`);
    S.results[id] = { ...(S.results[id] || {}), line: r };
  }),
  toggle: (el) => {
    const st = S.status[S.current], o = st.options, key = el.dataset.key;
    const body = {};
    let text;
    if (key === 'hopping') { body.hopping = !o.port_hopping; text = body.hopping ? '开启 Hy2 端口跳跃' : '关闭 Hy2 端口跳跃'; }
    else if (key === 'obfs') { body.obfs = !o.hy2_obfs; text = body.obfs ? '开启 Hy2 混淆' : '关闭 Hy2 混淆（所有 Hy2 客户端都必须更新订阅后才能连上）'; }
    else {
      const set = new Set(o.extra_protocols);
      set.has(key) ? set.delete(key) : set.add(key);
      body.protocols = [...set];
      text = (set.has(key) ? '开启 ' : '关闭 ') + (key === 'tuic' ? 'TUIC' : 'AnyTLS');
    }
    const opening = body.hopping === true || (body.protocols && body.protocols.includes(key));
    confirmModal(text, `${opening ? '开启前请确认云安全组已放行对应端口（开启后这里会列出完整的端口清单）。' : ''}服务器会校验新配置，失败自动恢复。各客户端"更新订阅"后生效，更新前旧节点照常可用。`, '确认', false, async () => {
      await api('POST', `/servers/${S.current}/options`, body);
      closeModal(); toast('已更新'); loadStatus(S.current);
    });
  },
  unlock: () => busyRun('unlock', async (id) => {
    toast('正在检测，大约需要半分钟……');
    const r = await api('POST', `/servers/${id}/unlock`);
    S.results[id] = { ...(S.results[id] || {}), unlock: r };
  }),
  upgrade: () => confirmModal('升级服务器', `把服务器上的 gowest 升级为这台电脑上的版本（${esc(S.version)}）。密钥、端口和订阅地址都不变；校验失败会自动恢复原来的文件。`, '开始升级', false, async () => {
    const r = await api('POST', `/servers/${S.current}/upgrade`);
    runJob(r.job, '升级 gowest', S.current);
  }),
  install: async (el) => {
    const values = Object.fromEntries(new FormData($('#install-form')).entries());
    el.disabled = true;
    try {
      const r = await api('POST', `/servers/${S.current}/install`, values);
      runJob(r.job, '安装 gowest', S.current);
    } catch (err) { toast(err.message, true); }
    el.disabled = false;
  },
  'edit-traffic': () => {
    const t = S.status[S.current].traffic;
    formModal('整台服务器的月流量', '云厂商按整台服务器计费，这里的用量约等于所有人用量之和的两倍（服务器收发各算一次）。', [
      { name: 'quota', label: '每月上限', value: t.quota_bytes ? gb(t.quota_bytes).replace(' ', '') : '', placeholder: '如 1TB；留空 = 不设置' },
      { name: 'on_exceed', label: '超出后', value: t.on_exceed, options: [['warn', '只提醒'], ['stop', '暂停代理到下个月']] },
      { name: 'metering', label: '计量方式', value: t.metering, span: true, options: [['both', '进 + 出（大多数云厂商，偏保守）'], ['outbound', '只计出方向（套餐只按出流量计费时）']] },
    ], '保存', async (v) => { await api('POST', `/servers/${S.current}/traffic`, v); closeModal(); toast('已更新流量设置'); loadStatus(S.current); });
  },
  'edit-bandwidth': () => formModal('套餐带宽', '多人共用时立即生效；一个人用时客户端更新订阅后生效。订阅地址不变。', [
    { name: 'bandwidth', label: '带宽', value: S.status[S.current].bandwidth_mbps + 'Mbps', placeholder: '如 100Mbps、1Gbps' },
  ], '保存', async (v) => { await api('POST', `/servers/${S.current}/bandwidth`, v); closeModal(); toast('带宽已更新'); loadStatus(S.current); }),
  'add-user': () => formModal('添加链接', '新链接有独立的密钥和端口，其他人的订阅地址不变。', [
    { name: 'name', label: '名字', placeholder: 'dave', hint: '小写字母、数字或 -' },
    { name: 'cap', label: '速度上限', placeholder: '留空 = 不限，如 20Mbps' },
    { name: 'quota', label: '每月流量额度', placeholder: '留空 = 不限，如 100GB' },
    { name: 'expire', label: '到期日', type: 'date', hint: '留空 = 长期有效' },
  ], '添加', async (v) => {
    await api('POST', `/servers/${S.current}/users`, { action: 'add', ...v });
    closeModal();
    toast(`已添加 ${v.name}；如果安全组没放行 20001-20032，记得放行它的端口`);
    await loadStatus(S.current);
    showQr(v.name);
  }),
  'edit-user': (el) => {
    const u = S.status[S.current].users.find((x) => x.name === el.dataset.user);
    formModal('修改 ' + u.name, '修改立即生效，订阅地址不变。超额或到期时只停这一个链接。', [
      { name: 'cap', label: '速度上限', value: u.cap_mbps ? u.cap_mbps + 'Mbps' : '', placeholder: '留空 = 不限' },
      { name: 'quota', label: '每月流量额度', value: u.quota_bytes ? gb(u.quota_bytes).replace(' ', '') : '', placeholder: '留空 = 不限' },
      { name: 'expire', label: '到期日', type: 'date', value: u.expire_date, span: true },
    ], '保存', async (v) => {
      await api('POST', `/servers/${S.current}/users`, { action: 'set', name: u.name, ...v });
      closeModal(); toast('已保存'); loadStatus(S.current);
    });
  },
  'reset-user': (el) => { S.menu = null; const name = el.dataset.user; confirmModal('重置 ' + name + ' 的链接', `${esc(name)} 会得到新的密钥和订阅地址，旧链接立即失效，需要重新导入。其他人不受影响。`, '重置', true, async () => {
    await api('POST', `/servers/${S.current}/users`, { action: 'reset', name });
    closeModal(); toast('已重置，新的二维码如下'); await loadStatus(S.current); showQr(name);
  }); },
  'delete-user': (el) => { S.menu = null; const name = el.dataset.user; confirmModal('删除 ' + name, `删除后 ${esc(name)} 的链接立即失效，其他人的端口和订阅地址不变。`, '删除', true, async () => {
    await api('POST', `/servers/${S.current}/users`, { action: 'remove', name });
    closeModal(); toast('已删除 ' + name); loadStatus(S.current);
  }); },
  qr: (el) => showQr(el.dataset.user || ''),
};

document.addEventListener('click', (e) => {
  const copyBtn = e.target.closest('[data-copy]');
  if (copyBtn) {
    navigator.clipboard.writeText(copyBtn.dataset.copy).then(() => toast('已复制'), () => toast('复制失败，请手动选择', true));
    return;
  }
  const el = e.target.closest('[data-act]');
  if (!el) {
    if (S.menu && !e.target.closest('.menu')) { S.menu = null; render(); }
    if (e.target.id === 'layer') closeModal();
    return;
  }
  const fn = ACTIONS[el.dataset.act];
  if (fn) { e.preventDefault(); fn(el); }
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('#layer').hidden) closeModal(); });
document.querySelectorAll('.i[data-i]').forEach((el) => { el.outerHTML = icon(el.dataset.i); });

/* ---------- boot ---------- */
(function boot() {
  const fromHash = location.hash.slice(1);
  if (fromHash) {
    sessionStorage.setItem('gowest-token', fromHash);
    history.replaceState(null, '', location.pathname);
  }
  S.token = sessionStorage.getItem('gowest-token') || '';
  if (!S.token) {
    $('#main').innerHTML = '<div class="card hero"><h1>请从终端里的链接打开</h1><p>运行 <code>./remote.sh ui</code>，终端会显示一个带令牌的地址。</p></div>';
    return;
  }
  loadServers().catch((err) => {
    $('#main').innerHTML = `<div class="card hero"><h1>打不开</h1><p>${esc(err.message)}</p></div>`;
  });
})();
