/* 标书智能体 · LangChain 版 前端（原生 JS，无构建步骤） */
/* 只给企业管理员看的页签：普通成员看不到入口，直接改 URL 也会被挡回主流程。
   后端本来就有权限校验，这里只是不让一线写标员被配置台淹没。 */
const ADMIN_TABS = new Set(['core', 'usage', 'settings']);

const S = { pid: '', project: null, projects: [], mode: 'demo', model: '', models: null, modelReg: null, testResult: null, kbCount: 0, framework: '', brain: null, checkResult: null, optResult: null, uploadTarget: '', coreCatalog: null, coreResult: null, coreKey: '', corePayload: '', autoStatus: null, versions: [], usage: null, privacy: null, budget: null, locate: null, settings: null, settingsDraft: null, reqTable: null, brainRuns: [], activeTab: 'chat', drafts: {}, me: null, isOwner: false, org: null, orgNew: false,
  kbList: [], kbCounts: null, kbQ: '', kbFilter: 'all', kbEdit: '', kbOpen: '', kbNew: false, kbNewKind: 'material', kbChapOpen: '', kbChap: {}, matNew: false, docOpen: '',
  secId: '', secHint: '', secDirty: false, secTimer: null, secConflict: null, secCandidate: null,
  saveChain: null, composeJob: null, genBusy: '', genTitle: '',
  outlineMem: null, bidImport: false, optJob: null, issueFilter: 'all', samples: [],
  outlinePlan: {}, composeResult: null, onlyEmpty: false };
const CHAT = { messages: [], pending: null, uploadHint: null, uploadFail: null,
  pendingText: '', attachNote: '', recent: [], sending: false,
  uploading: false, uploadingName: '',
  resumed: new Set(), restored: 0, _attachCache: '' };
const ATTACH_LABEL = { tender: '招标文件', draft: '已有竞标书', material: '我提供的资料', kb: '企业素材库' };
const UI_KEY = 'bidagent.ui.v1';

/* 待发送附件属于「输入框里的编辑态」：发送失败、登录过期、刷新页面都不该丢。
   按项目存 sessionStorage（关掉标签页即清除），只放解析结果，不放 File 对象。 */
function attachKey(pid) { return 'bidagent.attach.v1.' + (pid || 'none'); }
function syncAttach() {
  try {
    const k = attachKey(S.pid);
    const payload = JSON.stringify({ p: CHAT.pending || null, h: CHAT.uploadHint || null,
      n: CHAT.attachNote || '', r: (CHAT.recent || []).slice(-3) });
    if (payload === CHAT._attachCache) return;
    if (CHAT.pending || (CHAT.recent || []).length) sessionStorage.setItem(k, payload);
    else sessionStorage.removeItem(k);
    CHAT._attachCache = payload;
  } catch (_) { /* 超大文件存不下时只放弃刷新恢复，不影响本次发送 */ }
}
function restoreAttach(pid) {
  CHAT.pending = null; CHAT.uploadHint = null; CHAT.attachNote = '';
  CHAT.recent = []; CHAT.pendingText = ''; CHAT._attachCache = '';
  try {
    const raw = sessionStorage.getItem(attachKey(pid));
    if (!raw) return;
    const d = JSON.parse(raw);
    if (!d) return;
    if (d.p && typeof d.p.text === 'string') CHAT.pending = d.p;
    CHAT.uploadHint = d.h || null;
    CHAT.attachNote = d.n || '';
    CHAT.recent = Array.isArray(d.r) ? d.r : [];
    CHAT._attachCache = raw;
  } catch (_) { /* 缓存坏了就当没有附件 */ }
}
/* 服务端只有真正执行了入库步骤才算「收到文件」；用途反问、材料闸门、
   模型规划漏掉入库步骤、接口异常，都一律把附件留在输入框里。 */
const STORE_FLOWS = ['flow.set_tender', 'flow.import_draft', 'flow.add_material', 'flow.add_kb'];
function attachmentStored(r, att) {
  if (!att || !r) return false;
  // 服务端现在会明确回答「这次到底写没写进资料库」，优先认这个信号，
  // 不再靠执行轨迹 / 结果卡片去猜，避免 200 但没入库被当成成功。
  if (r.attachment_stored === true) return true;
  if (r.attachment_stored === false) return false;
  if (r.need_confirm || r.need_content || r.action === 'need_confirm' || r.action === 'need_content') return false;
  if ((r.trace || []).some(t => t && t.ok && STORE_FLOWS.includes(t.tool))) return true;
  if (r.action === 'attachment') return true;   // 旧兼容路径：只传文件、没下指令
  return (r.cards || []).some(c => c && c.title === '入库结果'
    && JSON.stringify(c.rows || []).includes(String(att.filename || '')));
}

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const nl = (s) => esc(s).replace(/\n/g, '<br>');

/* ---------------- 界面状态：只重渲当前页签、草稿不丢、刷新能回到原处 ---------------- */
function loadUI() {
  try { return JSON.parse(localStorage.getItem(UI_KEY) || '{}') || {}; }
  catch (_) { return {}; }
}
function saveUI(patch) {
  try { localStorage.setItem(UI_KEY, JSON.stringify({ ...loadUI(), ...(patch || {}) })); }
  catch (_) { /* 隐私模式下不写，不影响使用 */ }
}
function draftKey(el) {
  return el.dataset.draftKey || el.id ||
         ((el.dataset.role || el.name || '') + ':' + (el.dataset.sid || el.dataset.cap || ''));
}
function captureDraft(boxId) {
  const box = document.getElementById(boxId); if (!box) return;
  const d = {};
  box.querySelectorAll('input, textarea, select').forEach(el => {
    const k = draftKey(el); if (!k || k === ':') return;
    d[k] = el.type === 'checkbox' ? el.checked : el.value;
  });
  S.drafts[boxId] = d;
}
function restoreDraft(boxId) {
  const box = document.getElementById(boxId), d = S.drafts[boxId];
  if (!box || !d) return;
  box.querySelectorAll('input, textarea, select').forEach(el => {
    const k = draftKey(el);
    if (!k || !(k in d)) return;
    if (el.type === 'checkbox') {
      if (el.checked !== d[k]) { el.checked = d[k]; el.dataset.dirty = '1'; }
    } else if (el.value !== d[k]) {
      el.value = d[k]; el.dataset.dirty = '1';
    }
  });
}
function renderInto(boxId, html) {
  const skip = !!S.skipDraft;
  S.skipDraft = false;
  if (skip) S.drafts[boxId] = {}; else captureDraft(boxId);
  const box = document.getElementById(boxId);
  if (!box) return;
  box.innerHTML = html;
  if (!skip) restoreDraft(boxId);
}
function renderTab(tab) {
  const map = {
    overview: renderOverview, materials: renderMaterials, kb: renderKbTab,
    requirements: renderRequirements, write: renderWrite,
    check: renderCheck, core: renderCore, usage: renderUsage,
    settings: renderSettings,
  };
  const fn = map[tab]; if (!fn) return;
  // 启动阶段项目还没加载：只切页签样式，等 loadState 后再渲染内容（知识库/设置与项目无关）
  if (!S.project && tab !== 'settings' && tab !== 'kb') return;
  const boxId = 'tab-' + tab;
  captureDraft(boxId);
  const r = fn();
  if (r && typeof r.then === 'function') r.then(() => restoreDraft(boxId));
  else restoreDraft(boxId);
}
async function switchTab(tab, remember = true) {
  // 管理员页只对管理员开放；boot 阶段身份未加载（S.me 为空）时不拦，loadState 后再校正
  if (ADMIN_TABS.has(tab) && S.me && !S.isOwner) {
    toast('这一页只有企业管理员能用');
    return;
  }
  // 离开正文页前必须「确认写完」再走：以前只发起保存、不等结果，切页会丢稿
  if (S.activeTab === 'write' && tab !== 'write' && !(await secFlush())) {
    toast('正文还没保存成功，先处理保存或冲突提示再切页');
    return;
  }
  S.activeTab = tab;
  $$('.tab[data-tab]').forEach(x => x.classList.toggle('on', x.dataset.tab === tab));
  const moreBtn = $('#moreBtn'), menu = $('#moreMenu');
  const picked = menu && menu.querySelector(`[data-tab="${tab}"]`);
  if (moreBtn) {
    moreBtn.classList.toggle('on', !!picked);
    moreBtn.textContent = picked ? picked.textContent : '管理';
  }
  if (menu) menu.hidden = true;
  $$('.panel').forEach(p => p.classList.toggle('on', p.id === 'tab-' + tab));
  renderTab(tab);
  if (remember) saveUI({ tab });
}
function askBrain(text) {
  // 页签按钮不再自己发业务指令：替用户向大脑说同一句话，记忆 / 轨迹 / 配置都走同一条链
  switchTab('chat');
  if (CHAT.sending) return toast('上一句还在执行，等它跑完再发');
  return sendChat(text, '');
}
async function loadBrainRuns() {
  if (!S.pid) return;
  try { S.brainRuns = (await api('/api/brain/history?pid=' + encodeURIComponent(S.pid) + '&limit=6')).runs || []; }
  catch (_) { S.brainRuns = []; }
}

async function api(path, method = 'GET', body) {
  const opt = { method };
  if (body !== undefined) { opt.headers = { 'Content-Type': 'application/json' }; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let j = {};
  try { j = await r.json(); } catch (_) { /* ignore */ }
  if (r.status === 401) {
    if (!location.pathname.startsWith('/login')) location.replace('/login.html');
    throw new Error('登录已过期，请重新登录');
  }
  if (!r.ok) {
    // 本地服务没起来时，预览网关会返回纯文本 502；给可操作的中文提示，别只报状态码
    const offline = [502, 503, 504].includes(r.status);
    const msg = offline ? '本地服务没响应，请确认「启动.bat」已运行，再刷新页面重试'
      : (typeof j.detail === 'string' ? j.detail
        : (j.error || j.detail?.msg || `请求失败 ${r.status}`));
    const err = new Error(msg);
    // 保存冲突要靠 status / body 判断，不能只靠一句话
    err.status = r.status; err.body = j;
    throw err;
  }
  return j;
}

/* ---------------- 应用内确认 / 输入弹窗 ----------------
   浏览器原生对话框会被 iframe 沙箱、或用户勾选的
   「阻止此页面创建更多对话框」静默拦截，表现就是点了按钮毫无反应。
   全部改用页面内弹窗：不依赖浏览器权限，返回 Promise 由调用方 await。
   取消时确认框返回 false、输入框返回 null，与原生的语义保持一致。 */
let _dlgResolve = null;
let _dlgIsInput = false;

function _dlgClose(value) {
  const box = $('#uiDialog');
  if (box) box.hidden = true;
  const fn = _dlgResolve; _dlgResolve = null;
  if (fn) fn(value);
}

function _dlgCancel() {
  const inp = $('#dlgInput');
  _dlgClose(inp && !inp.hidden ? null : false);
}

function _dlgOk() {
  const inp = $('#dlgInput');
  _dlgClose(inp && !inp.hidden ? inp.value : true);
}

function _dlgOpen(opt) {
  return new Promise(resolve => {
    // 同一时刻只允许一个弹窗；前一个未关闭时按「取消」收尾，避免 Promise 悬挂
    if (_dlgResolve) {
      const prev = _dlgResolve; _dlgResolve = null;
      prev(_dlgIsInput ? null : false);
    }
    _dlgResolve = resolve;
    _dlgIsInput = !!opt.input;
    const box = $('#uiDialog');
    $('#dlgTitle').textContent = opt.title || '请确认';
    $('#dlgText').textContent = opt.message || '';
    const inp = $('#dlgInput');
    inp.hidden = !opt.input;
    if (opt.input) inp.value = opt.value ?? '';
    const ok = $('#dlgOk');
    ok.textContent = opt.okText || '确定';
    ok.classList.toggle('danger', !!opt.danger);
    box.hidden = false;
    (opt.input ? inp : ok).focus();
    if (opt.input) inp.select();
  });
}

function uiConfirm(message, opt = {}) {
  return _dlgOpen({ ...opt, message, title: opt.title || '请确认' });
}

function uiPrompt(message, value = '', opt = {}) {
  return _dlgOpen({ ...opt, message, input: true, value, title: opt.title || '请输入' });
}

function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(t._timer); t._timer = setTimeout(() => t.classList.remove('show'), 2600);
}

function busy(btn, on, text) {
  if (!btn) return;
  if (on) { btn._old = btn.textContent; btn.textContent = text || '处理中…'; btn.disabled = true; }
  else { btn.textContent = btn._old || btn.textContent; btn.disabled = false; }
}

function poll(jid, onProgress, onDone) {
  const tick = async () => {
    let j;
    try { j = await api('/api/jobs/' + jid); } catch (e) { toast(e.message); return; }
    onProgress(j);
    if (j.status === 'running') setTimeout(tick, 450); else onDone(j);
  };
  tick();
}

function levelChip(level) {
  const cls = level === '高' ? 'high' : level === '中' ? 'mid' : 'low';
  return `<span class="chip ${cls}">${esc(level)}风险</span>`;
}

/* ---------------- 启动 ---------------- */
async function boot() {
  const ui = loadUI();
  const q = new URLSearchParams(location.search);
  S.activeTab = q.get('tab') || ui.tab || 'chat';
  S.forcePid = q.get('pid') || '';
  CHAT.guideHidden = !!ui.guideHidden;
  $$('.tab[data-tab]').forEach(t => t.onclick = () => switchTab(t.dataset.tab));
  const moreBtn = $('#moreBtn');
  if (moreBtn) moreBtn.onclick = (e) => {
    e.stopPropagation();
    const m = $('#moreMenu'); if (m) m.hidden = !m.hidden;
  };
  document.addEventListener('click', (e) => {
    const wrap = $('#moreWrap'), m = $('#moreMenu');
    if (wrap && m && !wrap.contains(e.target)) m.hidden = true;
    const adv = $('#advSettings');
    if (adv && adv.open && !adv.contains(e.target)) adv.open = false;
    const um = $('#userMenu');
    if (um && um.open && !um.contains(e.target)) um.open = false;
  });
  $$('#modeSwitch button').forEach(b => b.onclick = () => setMode(b.dataset.mode));
  $('#btnModels').onclick = openModels;
  $('#modelSelect').onchange = (e) => setActiveModel(e.target.value);
  document.addEventListener('change', onModelChange);
  document.addEventListener('change', onSettingsChange);
  // 正文编辑：实时字数 + 停手 1.5 秒自动保存
  document.addEventListener('input', (e) => {
    const t = e.target;
    if (!t || !t.dataset) return;
    if (t.dataset.role === 'secText') {
      const n = t.value.length;
      const w1 = $('#edWords'), w2 = $('#edWords2');
      if (w1) w1.textContent = n.toLocaleString();
      if (w2) w2.textContent = n.toLocaleString();
      S.secDirty = true;
      setEdState('未保存');
      if (S.secTimer) clearTimeout(S.secTimer);
      S.secTimer = setTimeout(() => { saveSecNow(); }, 1500);
    }
    if (t.dataset.role === 'secHint') S.secHint = t.value;
    if (t.dataset.role === 'projQ') {
      S.projPanel = { ...(S.projPanel || {}), q: t.value };
      renderProjectsPanel();
    }
  });
  document.addEventListener('click', (e) => {
    if (e.target.id === 'modelModal') closeModels();
    if (e.target.id === 'projectsModal') { $('#projectsModal').hidden = true; }
  });
  $('#btnNewProject').onclick = newProject;
  $('#fileInput').onchange = onFilePicked;
  document.addEventListener('keydown', (e) => {
    if (e.target.id === 'chatInput' && e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault(); ACT.send();
    }
  });
  switchTab(S.activeTab, false);
  await loadState();
}

async function loadState() {
  const st = await api('/api/state');
  S.me = st.me || null; S.isOwner = !!st.is_owner;
  S.isPlatformAdmin = !!st.is_platform_admin;
  S.projects = st.projects; S.mode = st.mode; S.model = st.model; S.models = st.models || null;
  S.archivedCount = st.archived_count || 0;
  S.kbCount = st.kb_count; S.framework = st.framework; S.brain = st.brain || null;
  S.privacy = st.privacy || null;
  S.configScope = st.config_scope || null;
  S.modelHealth = st.model_health || null; S.modelUsable = st.model_usable !== false;
  try { S.coreCatalog = await api('/api/core/catalog'); } catch (_) { S.coreCatalog = null; }
  try { S.settings = await api('/api/settings'); } catch (_) { S.settings = null; }
  try { S.embedding = await api('/api/embedding'); } catch (_) { S.embedding = null; }
  await loadSamples();
  renderHeader(); renderSidebar(); renderUser();
  applyRoleView();
  if (S.privacy && !S.privacy.acknowledged) openPrivacy();
  $('#frameworkLine').textContent = S.framework;
  if (S.brain) $('#brainLine').textContent =
    `工具目录 ${S.brain.count} 项：可执行 ${S.brain.executable} / 需模型 ${S.brain.needs_model} / 占位 ${S.brain.placeholder} · 配置后可自动挑选 ${S.brain.selectable ?? S.brain.plannable} 项（停用/限用 ${S.brain.configured_out || 0} 项） · ${S.brain.planner}`;
  const ui = loadUI();
  const target = S.projects.find(p => p.id === (S.forcePid || ui.pid)) || S.projects[0];
  if (target) await selectProject(target.id, false);
}

/* 角色视图：普通成员只看主流程四个页签；配置、用量、运行记录不出现在眼前。
   功能与后端接口都保留，管理员照常可见可用。 */
function applyRoleView() {
  const admin = !!S.isOwner;
  $$('[data-admin]').forEach(el => {
    el.hidden = !admin;
    el.style.display = admin ? '' : 'none';
  });
  if (!admin && ADMIN_TABS.has(S.activeTab)) switchTab('chat', false);
}

function renderHeader() {
  $$('#modeSwitch button').forEach(b => b.classList.toggle('on', b.dataset.mode === S.mode));
  const mh = S.modelHealth;
  const broken = S.mode === 'live' && mh && mh.status === 'error';
  const list = (S.models && S.models.list) || [];
  const act = list.find(m => m.id === ((S.models && S.models.active) || ''));
  const nameEl = $('#modelName');
  nameEl.textContent = broken ? `AI 模型没连上 · ${mh.kind}` : (act ? act.label : S.model);
  nameEl.classList.toggle('model-warn', !!broken);
  nameEl.title = broken
    ? `${mh.hint}\n最近一次失败：${mh.detail}\n来源：${mh.source || '模型调用'}`
    : (act ? `${act.label} · ${act.model || ''}` : (S.model || ''));
  const pv = $('#btnPrivacy');
  if (pv) {
    const on = !!(S.privacy && S.privacy.outbound_allowed);
    const txt = $('#netText');
    if (txt) txt.textContent = on ? '联网：开' : '联网：关';
    pv.classList.toggle('danger', !on);
    pv.title = on ? '隐私与联网设置（已开启，点击管理）' : '已关闭联网，全部只在本机处理（点击开启）';
  }
  const sel = $('#modelSelect');
  if (!sel) return;
  sel.innerHTML = (list.length ? '<option value="" disabled>选择真实模型…</option>' : '<option value="">未接入模型</option>')
    + list.map(m => `<option value="${esc(m.id)}">${esc(m.label || m.model)}</option>`).join('');
  sel.value = (S.models && S.models.active) || '';
  sel.classList.toggle('dim', S.mode !== 'live');

  const meta = $('#modelMeta'), mdot = $('#modelDot');
  if (meta) {
    let text, cls = '';
    if (S.mode !== 'live') { text = '本地演示：不调用模型，结果固定可复现'; cls = 'off'; }
    else if (broken) { text = (mh.hint || '模型没连上') + '，可点「管理模型」检查'; cls = 'bad'; }
    else if (!list.length) { text = '还没接入真实模型，点「管理模型」添加'; cls = 'off'; }
    else { text = `已就绪，共 ${list.length} 个可选`; }
    meta.textContent = text;
    if (mdot) mdot.className = 'adv-dot ' + cls;
  }
}

function renderSidebar() {
  const list = S.projects || [];
  const shown = list.slice(0, 8);
  const hidden = list.length - shown.length;
  $('#projectList').innerHTML = (shown.map(p => `
    <div class="project-item ${p.id === S.pid ? 'on' : ''}" data-act="selectProject" data-pid="${esc(p.id)}">
      <div class="pi-name">${esc(p.name)}</div>
      <div class="pi-sub">${esc((p.created_at || '').slice(5, 16))}</div>
      <button class="pi-more" data-act="openProjects" data-pid="${esc(p.id)}" title="归档 / 收进知识库 / 删除">···</button>
    </div>`).join('')
    || `<div class="empty">还没有项目<br><button class="btn mini" data-act="newProjectBtn">新建一个</button></div>`)
    + (hidden > 0 ? `<button class="pj-more-link" data-act="openProjects">还有 ${hidden} 个项目，查看全部</button>` : '');
  const n = list.length + (S.archivedCount || 0);
  const all = $('#btnAllProjects');
  if (all) all.textContent = n > 8 ? `全部 ${n}` : '全部';
}

async function selectProject(pid, remember = true) {
  // 换项目前先把当前项目的正文写完：以前直接改 pid，未保存的稿会跟着状态一起清掉
  if (S.pid && pid !== S.pid && !(await secFlush())) {
    toast('当前标书有内容没保存成功，先处理保存或冲突提示再切换项目');
    renderSidebar();
    return;
  }
  S.pid = pid;
  S.project = await api('/api/projects/' + pid);
  await loadAutoStatus(); await loadVersions(); await loadUsage(); await loadOutlinePlan(); await loadBrainRuns(); await loadQuality(); await loadReqTable();
  S.checkResult = null; S.optResult = null; S.drafts = {};
  S.secId = ''; S.secHint = ''; S.secDirty = false; S.composeJob = null;
  S.secConflict = null; S.saveChain = null;
  S.genBusy = ''; S.genTitle = ''; S.outlineMem = null; S.bidImport = false;
  S.optJob = null; S.issueFilter = 'all';
  // 记住上次在这本标书里改到第几章，下次进来直接回到那一章
  S.secId = loadUI()['sec:' + pid] || '';
  if (S.secTimer) { clearTimeout(S.secTimer); S.secTimer = null; }
  if (remember) saveUI({ pid });
  let restored = [];
  try {
    const hist = await api('/api/chat/messages?pid=' + encodeURIComponent(pid) + '&limit=30');
    restored = (hist.messages || []).map(m => ({
      role: m.role === 'user' ? 'user' : 'agent',
      text: m.text || '', cards: (m.meta && m.meta.cards) || [], sources: [],
      restored: true,
    // 附件属于输入框里的编辑态，历史上旧的「[上传文件]」独立记录不再显示成一条消息
    })).filter(m => m.text && !(m.role === 'user' && /^\[上传文件\]\s/.test(m.text)));
  } catch (_) { restored = []; }
  CHAT.messages = restored.length ? restored : [{
    role: 'agent',
    reply: `已进入「${S.project.name}」。把文件丢进来，或者直接说一句要做什么，我会自己看现状、一步步做完，再把结果给你。`,
    cards: [{ type: 'list', title: '可以直接说', items: [
      '把这份招标文件变成投标书', '帮我检查一下标书有没有废标风险',
      '按问题把标书改好并复查', '生成技术偏离表', '下载 Word',
    ] }],
    sources: [],
  }];
  CHAT.restored = restored.length;
  restoreAttach(pid);   // 刷新 / 重新登录回来，未发送的附件仍挂在这个项目的输入框里
  renderSidebar(); renderAll();
  await resumePendingJobs();
}

async function loadAutoStatus() {
  if (!S.pid) return;
  try { S.autoStatus = await api('/api/core/auto/status?pid=' + encodeURIComponent(S.pid)); }
  catch (_) { S.autoStatus = null; }
}

async function loadQuality() {
  if (!S.pid) return;
  try { S.quality = await api('/api/projects/' + encodeURIComponent(S.pid) + '/quality'); }
  catch (_) { S.quality = null; }
}

async function loadReqTable() {
  if (!S.pid) return;
  try { S.reqTable = await api('/api/projects/' + encodeURIComponent(S.pid) + '/requirements'); }
  catch (_) { S.reqTable = null; }
}

/* ---------------- 一张「能不能交」卡 ----------------
   quality / health / 导出门禁 / 交付状态以前各出一张卡，用户不知道该信哪个。
   后端 /readiness 只读汇总成一句话，检查明细仍折叠保留，不删功能。 */
function readinessHtml() {
  return `<div class="card" id="readinessBox"><h2>能不能交</h2><div class="hint">正在核对…</div></div>`;
}

function readinessCardHtml(r) {
  const cls = r.level === 'red' ? 'high' : r.level === 'yellow' ? 'mid' : 'ok';
  const a = r.action || {};
  const btn = a.tab
    ? `<button class="btn primary" data-act="goto" data-tab="${esc(a.tab)}">${esc(a.label)}</button>`
    : `<button class="btn primary" data-act="${esc(a.act || 'check')}">${esc(a.label)}</button>`;
  const items = r.findings || [];
  return `<h2>能不能交 <span class="chip ${cls}">${esc(r.title)}</span></h2>
    <p class="desc">${esc(r.reason || '')}</p>
    ${r.cert_hint ? `<div class="hint" style="margin-top:6px;color:#c0392b">${esc(r.cert_hint)}</div>` : ''}
    ${r.asset_gap_hint ? `<div class="hint" style="margin-top:6px;color:#b45309">${esc(r.asset_gap_hint)}
      <button class="btn mini" data-act="showAssetGap">查看缺件清单</button></div>
      <div id="assetGapBox" class="asset-gap" hidden></div>` : ''}
    <div class="row">${btn}<span class="grow"></span><span class="hint">${esc(r.hint || '')}</span></div>
    ${items.length ? `<details class="fold" style="margin-top:8px"><summary>最需要处理的 ${items.length} 项</summary>
      ${items.map(x => `<div class="q-defect"><span class="chip ${x.level === '高' ? 'warn' : 'gray'}">${esc(x.level)}</span><span class="q-title">${esc(x.title)}</span><span class="q-detail">${esc(x.detail)}</span></div>`).join('')}
    </details>` : ''}`;
}

/* P3：缺件候选清单。按需拉取，只列候选材料，不显示「能得几分」。 */
function assetGapHtml(r) {
  const rows = (r.rows || []).map(x => `<div class="q-defect">
    <span class="chip ${x.status === 'missing' || x.status === 'expired' ? 'warn' : 'gray'}">${esc(x.status_label)}</span>
    <span class="q-title">${esc(x.text)}</span>
    <span class="q-detail">${esc(x.kind_label)}${x.score ? ' · ' + esc(x.score) + ' 分' : ''}
      · 需要 ${esc(x.needed_material)}${x.loc ? ' · 原文位置 ' + esc(x.loc) : ''}
      ${(x.matched_assets || []).length ? ' · 已有：' + esc(x.matched_assets.join('、')) : ''}
      ${(x.expired_assets || []).length ? ' · 过期：' + esc(x.expired_assets.join('、')) : ''}</span>
  </div>`).join('');
  return `<div class="hint" style="margin:8px 0">共 ${r.total || 0} 条候选：缺材料 ${r.missing || 0}、过期 ${r.expired || 0}、已有候选 ${r.candidate || 0}${r.truncated ? '（只显示前 40 条）' : ''}</div>
    ${rows || '<div class="empty">没有识别到可对照的材料类型。这不代表条款不需要材料，请人工核对评分办法。</div>'}
    <p class="hint" style="margin-top:8px">${esc(r.note || '')}</p>
    ${r.error ? `<p class="hint" style="color:#c0392b">${esc(r.error)}</p>` : ''}`;
}

/* 结论卡与「自动检查明细」必须说同一件事：整本体检已经列出 findings 时，
   明细区不能再显示「0 条 / 还没有检查结果，点开始检查」。 */
function readinessFindingsHtml(r) {
  const items = (r.findings || []).slice(0, 8);
  if (!items.length) return '';
  return `<div class="hint" style="margin-bottom:8px">下面 ${items.length} 项来自整本体检，是只读结论；
    点「开始检查」可生成能逐条采纳、能定位到段落的问题清单。</div>`
    + items.map(x => `<div class="q-defect">
        <span class="chip ${x.level === '高' ? 'warn' : 'gray'}">${esc(x.level || '提示')}</span>
        <span class="q-title">${esc(x.title || '')}</span>
        <span class="q-detail">${esc(x.detail || '')}</span></div>`).join('');
}

let readinessSeq = 0;

async function loadReadiness() {
  if (!S.pid) return;
  if (!$('#readinessBox')) return;
  const seq = ++readinessSeq;
  const pid = S.pid;
  // 后端体检慢时不能让「正在核对…」一直挂着；超时给一个可重试的明确状态。
  const timer = setTimeout(() => {
    const now = $('#readinessBox');
    if (now && seq === readinessSeq && now.dataset.done !== '1') {
      now.innerHTML = `<h2>能不能交 <span class="chip warn">还没查完</span></h2>
        <p class="desc">核对花的时间比预期长，这一刻不能当作「可以交」。</p>
        <button class="btn mini" data-act="retryReadiness">重新核对</button>`;
    }
  }, 8000);
  try {
    const r = await api('/api/projects/' + pid + '/readiness');
    if (seq !== readinessSeq) return;          // 项目/页面已换，丢弃旧结论
    S.readiness = r;
    const now = $('#readinessBox');
    if (now) { now.dataset.done = '1'; now.innerHTML = readinessCardHtml(r); }
    const list = document.querySelector('#checkIssueList');
    if (list && list.dataset.empty === '1') {
      const html = readinessFindingsHtml(r);
      if (html) list.innerHTML = html;
    }
    const chip = document.querySelector('#tab-check h2 .chip');
    if (chip && chip.textContent.includes('还没检查') && (r.findings || []).length) {
      chip.textContent = `整本体检 · ${(r.findings || []).length} 项待处理`;
      chip.className = 'chip mid';
    }
  } catch (e) {
    if (seq !== readinessSeq) return;
    const now = $('#readinessBox');
    if (now) {
      now.dataset.done = '1';
      now.innerHTML = `<h2>能不能交 <span class="chip warn">没查成</span></h2>
        <p class="desc">${esc(e.message)}。这一刻不能当作「可以交」，请先重新检查。</p>
        <button class="btn" data-act="retryReadiness">重新核对</button>`;
    }
  } finally { clearTimeout(timer); }
}

function qualityPanelHtml() {
  const q = S.quality;
  if (!q) return '';
  // 三态：pass 通过 / fail 有未通过项 / unavailable 这一轮没跑完。
  // 以前「检查报错」也会显示成没事，这里必须区分开。
  const st = q.status || (q.ok === false ? 'unavailable'
    : (q.all_passed ? 'pass' : 'fail'));
  if (st === 'unavailable') {
    return `<div class="card">
      <h2>正文检查 <span class="chip warn">这一轮没检查完</span></h2>
      <p class="desc">检查过程出错了（${esc(q.error || '未知原因')}），所以「没有发现问题」这句话现在不成立。
        未完成的检查不算通过，导出也会被拦住。</p>
      <div class="q-block">先点「重新体检」；如果反复失败，按人工清单核对招标文件与正文，再决定是否继续。</div>
      <button class="btn" data-act="check">重新体检</button>
    </div>`;
  }
  const ok = st === 'pass';
  const bad = q.undeliverable || [];
  const lines = (q.defects || []);
  return `<div class="card">
      <h2>正文检查 <span class="chip ${ok ? 'ok' : 'warn'}">${ok ? '检查过，没发现明显硬伤' : `${q.passed_count}/${q.total_checks} 项通过`}</span>
        ${bad.length ? `<span class="chip warn">${bad.length} 章还不能直接交</span>` : ''}</h2>
      <p class="desc">自动检查四件事：有没有逐条响应、数字有没有出处、有没有 XXX 之类占位符、章节是不是空的。只查明显硬伤，不能代替人工评审；保护章节（报价、投标函等）不套字数下限，AI 也不会改，但空着一定会被标出来。</p>
      ${bad.length ? `<div class="q-block"><b>这几章还不能下载</b>
        ${bad.map(s => `<div class="q-bad-row"><span>「${esc(s.title)}」：${esc((s.lines || []).join('；')) || '正文需要人工核对'}</span>
          ${s.section_id ? `<button class="btn mini" data-act="gotoSec" data-sid="${esc(s.section_id)}">去改这一章</button>` : ''}</div>`).join('')}
        必须先改好正文才能下载，勾选确认不能代替修改。</div>` : ''}
      ${lines.length ? `<div class="q-defects">${lines.map(d => `<div class="q-defect">
        <span class="chip ${d.level === '高' ? 'warn' : 'gray'}">${esc(d.level || '提示')}</span>
        <span class="q-title">${esc(d.title || '')}</span>
        <span class="q-detail">${esc(d.detail || '')}</span></div>`).join('')}</div>`
      : '<div class="hint">没有发现明显的硬伤。</div>'}
    </div>`;
}

async function loadVersions() {
  if (!S.pid) return;
  try { S.versions = (await api('/api/projects/' + S.pid + '/versions')).batches || []; }
  catch (_) { S.versions = []; }
}

async function loadUsage() {
  if (!S.pid) return;
  try {
    S.usage = await api('/api/projects/' + S.pid + '/usage');
    S.budget = await api('/api/budget');
  } catch (_) { S.usage = null; S.budget = null; }
}

/* 目录计划留痕：刷新页面后「目录体检」仍在，不用为了看未匹配评分项重跑一次目录。 */
async function loadOutlinePlan() {
  if (!S.pid) return;
  try { S.outlinePlan = (await api('/api/projects/' + S.pid + '/outline/plan')) || {}; }
  catch (_) { S.outlinePlan = S.outlinePlan || {}; }
}

function openLocate() {
  const m = $('#locateModal'); if (!m) return;
  const r = S.locate || { pages: [], hint: '未取到原文' };
  $('#locateBody').innerHTML = (r.pages || []).map(p => `
    <div class="card">
      <h3>${esc(p.kind || '文件')} · ${esc(p.title || '')} · 原文第 ${p.page} 页（物理页 ${p.physical}） · ${esc(p.source)}</h3>
      <pre class="loc-text">${esc(p.text || '（本页无文字）')}</pre>
    </div>`).join('') || `<div class="empty">${esc(r.hint || '未找到该页原文')}</div>`;
  m.hidden = false;
}
function closeLocate() { const m = $('#locateModal'); if (m) m.hidden = true; }

function renderUsage() {
  const u = S.usage || { summary: {}, rows: [], today_tokens: 0 };
  const s = u.summary || {};
  const b = (S.budget && S.budget.budget) || { daily_token_limit: 200000, enabled: true };
  const used = u.today_tokens || 0;
  const pct = Math.min(100, Math.round(used / (b.daily_token_limit || 200000) * 100));
  $('#tab-usage').innerHTML = `
    <div class="card">
      <h2>用量与审计 <span class="hint">本项目 · 每次模型调用都留账</span></h2>
      <div class="stat-grid">
        <div class="stat"><div class="n">${s.calls || 0}</div><div class="t">模型调用</div></div>
        <div class="stat"><div class="n">${s.tin || 0}</div><div class="t">输入 token</div></div>
        <div class="stat"><div class="n">${s.tout || 0}</div><div class="t">输出 token</div></div>
        <div class="stat"><div class="n">${Math.round((s.ms || 0) / 1000)}s</div><div class="t">累计耗时</div></div>
      </div>
      <p class="desc">演示模式的 token 为按字数估算；接真实模型时取 LangChain 回调回报的实际用量。触发者、环节、模型、耗时与输入输出摘要逐条留痕。</p>
    </div>
    <div class="card">
      <h2>单日预算护栏</h2>
      <div class="kv-row"><span>今日已用 / 上限</span><span><b>${used}</b> / ${b.daily_token_limit || 200000} token（${pct}%）</span></div>
      <div class="progress-wrap"><div class="progress"><div style="width:${pct}%"></div></div></div>
      <div class="row" style="margin-top:10px;align-items:center">
        <label class="hint">当日上限 <input type="number" id="budgetLimit" value="${b.daily_token_limit || 200000}" min="1000" max="10000000" style="width:110px" /></label>
        <button class="btn mini" data-act="budgetSave">保存上限</button>
        <button class="btn mini" data-act="budgetToggle">${b.enabled === false ? '启用护栏' : '停用护栏'}</button>
        <button class="btn mini" data-act="usageRefresh">刷新</button>
      </div>
      <p class="desc">预算用尽后，智能体本次只跑本地确定性基座，需生成的文字显式标注「需人工补充」，不再调用外部模型。</p>
    </div>
    <div class="card">
      <h2>最近调用 <span class="hint">${(u.rows || []).length} 条</span></h2>
      ${(u.rows || []).map(r => `<div class="kv-row"><span>${esc((r.created_at || '').slice(5, 19))} · ${esc(r.actor)} · ${esc(r.task)} · ${esc(r.model)} ${r.ok ? '' : '<b style="color:var(--danger)">失败</b>'}</span><span>${r.tokens_in}+${r.tokens_out} tok · ${r.elapsed_ms}ms</span></div>
        <div class="loc digest" title="入：${esc(r.input_digest || '')}
出：${esc(r.output_digest || '')}">入：${esc(r.input_digest || '')}｜出：${esc(r.output_digest || '')}</div>`).join('') || '<div class="empty">还没有模型调用记录</div>'}
    </div>`;
}

/* ---------------- 数据出域 ---------------- */
function openPrivacy() {
  const m = $('#privacyModal'); if (!m) return;
  m.hidden = false; renderPrivacy();
}
function closePrivacy() {
  const m = $('#privacyModal');
  if (m) m.hidden = true;
  // 未确认也能关掉面板；后端 guard 本来就在确认前禁止外发，不是靠遮罩拦人。
  if (!(S.privacy || {}).acknowledged)
    toast('还没确认联网设置；确认前不会调用外部模型');
}
function renderPrivacy() {
  const p = S.privacy || {};
  const s = $('#privacyScrub'); if (s) s.checked = p.scrub_sensitive !== false;
  const mx = $('#privacyMax'); if (mx) mx.value = p.max_chars || 12000;
  const row = $('#privacyToggleRow'); if (row) row.hidden = !p.acknowledged;
  const btn = $('#privacyToggleBtn');
  if (btn) btn.textContent = p.allow_outbound ? '关闭外发' : '重新开启外发';
  const note = $('#privacyNote'); if (note) note.textContent = p.note || '';
}

async function loadSamples() {
  try { S.samples = (await api('/api/kb/samples')).items || []; } catch (_) { /* 未登录或接口未就绪时忽略 */ }
}

async function refresh() {
  if (!S.pid) return;
  S.project = await api('/api/projects/' + S.pid);
  if (!(S.samples || []).length) await loadSamples();
  await loadAutoStatus(); await loadVersions(); await loadUsage(); await loadOutlinePlan(); await loadBrainRuns(); await loadQuality(); await loadReqTable();
  renderAll();
}

function renderAll() {
  if (!S.project) return;
  // 只重渲当前页签：其它页签里没保存的正文、粘贴的标书、资料输入框不再被冲掉
  renderChat();
  renderTab(S.activeTab);
}

/* ---------------- 对话 ---------------- */
const QUICKS = [
  ['写整本标书', '', '根据我提供的资料和招标文件写整本竞标书'],
  ['检查标书', '', '帮我验一下标书，看看有没有废标风险'],
  ['按问题改好并复查', '', '按问题把标书改好并复检'],
  ['下载 Word', '', '导出 Word'],
  ['生成大纲', '', '按招标文件生成大纲'],
  ['看看我的资料', '', '我现在有哪些资料'],
  ['废标风险核对', '', '做一次废标条款核对'],
  ['技术偏离表', '', '生成技术偏离表'],
  ['暗标检查', '', '查一下暗标有没有泄密信息'],
  ['报价校验', '', '校验一下报价表'],
];

/* 第一次进来给一条最短路径；做完了可以收起，不再占地方。
   符号全部用 CSS 画（数字徽标 / 勾 / 箭头），不放易缺字的特殊字符。 */
function startCardHtml() {
  if (CHAT.guideHidden || !S.project) return '';
  const p = S.project, secs = p.sections || [];
  const hasTender = !!(p.tender_text || '').trim();
  const written = secs.filter(s => (s.text || '').trim()).length;
  const empty = secs.filter(s => !(s.text || '').trim() && !s.protected).length;
  let step, label, hint, tab = 'materials';
  if (!hasTender) {
    step = 1; label = '上传招标文件'; tab = 'materials';
    hint = '把招标书传给我，我才知道要响应什么。';
  } else if (!secs.length) {
    step = 2; label = '生成目录'; tab = 'write';
    hint = '点这里直接按招标文件生成目录，生成后可逐章改名。';
  } else if (empty) {
    step = 3; label = written ? `继续写剩下 ${empty} 章` : '开始写正文'; tab = 'write';
    hint = '点这里直接开始写空着的章节，进度会显示在正文页。';
  } else {
    step = 4; label = '检查并下载'; tab = 'check';
    hint = '点这里先跑一遍检查，检查页就能直接下载 Word。';
  }
  const dot = (n, name) => `<span class="st-step ${n < step ? 'done' : n === step ? 'now' : ''}">
      <i>${n < step ? '' : n}</i>${esc(name)}</span>`;
  return `<div class="start-card">
    <div class="st-head">
      <div class="st-title">接下来做这一步</div>
      <button class="st-close" data-act="hideGuide" title="不再显示引导">收起引导</button>
    </div>
    <div class="st-steps">${dot(1, '上传招标书')}<span class="st-arrow"></span>${dot(2, '定目录')}<span class="st-arrow"></span>${dot(3, '写正文')}<span class="st-arrow"></span>${dot(4, '检查下载')}</div>
    <div class="st-action">
      <button class="btn primary st-go" data-act="guideGo" data-step="${step}" data-tab="${tab}">${esc(label)}</button>
      <span class="hint">${esc(hint)}</span>
    </div>
  </div>`;
}

function fmt(s) {
  return esc(s || '').replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>');
}

function stepKind(t) {
  if (t.cost === 'async') return ['后台处理', 'async'];
  if (t.cost === 'model' || t.kind === 'ai') return ['AI 写作', 'ai'];
  if (t.kind === 'flow' || t.kind === 'stage') return ['自动流程', 'flow'];
  return ['本地检查', 'base'];
}

function cardHtml(c) {
  if (c.type === 'plan') return `<div class="cardlet planlet">
    <div class="ct">这次做了什么
      <span class="chip ${c.planner === 'model' ? 'blue' : 'gray'}">${esc(c.planner_label || (c.planner === 'model' ? 'AI 自己安排' : '本地安排'))}</span>
      ${c.model ? `<span class="chip gray">${esc(c.model)}</span>` : ''}
      ${c.replanned ? '<span class="chip gray">已重规划 1 次</span>' : ''}
      <span class="hint">${(c.items || []).length} 步 · ${c.elapsed_ms || 0} ms</span></div>
    <div class="plan-goal"><b>目标</b> ${esc(c.goal || '')}</div>
    <div class="plan-why"><b>判断</b> ${esc(c.reasoning || '')}</div>
    ${c.fallback_reason ? `<div class="plan-why"><b>降级原因</b> ${esc(c.fallback_reason)}</div>` : ''}
    ${c.replan_reason ? `<div class="plan-why"><b>重规划</b> ${esc(c.replan_reason)}</div>` : ''}
    <div class="plan-steps">${(c.items || []).map(t => {
      const k = stepKind(t);
      return `<div class="plan-step ${t.ok ? '' : 'bad'}">
        <div class="ps-head"><span class="ps-idx">${t.index}</span><span class="ps-kind ${k[1]}">${k[0]}</span>
          <b>${esc(t.label)}</b><span class="hint">${t.elapsed_ms || 0} ms</span>
          <span class="${t.ok ? 'ps-ok' : 'ps-bad'}">${t.ok ? '完成' : '失败'}</span></div>
        ${t.why ? `<div class="ps-why">选它：${esc(t.why)}</div>` : ''}
        ${t.summary ? `<div class="ps-res">${esc(String(t.summary).slice(0, 160))}</div>` : ''}
        ${t.validation && t.validation !== 'ok' ? `<div class="ps-why">步间校验：${esc(t.validation)}</div>` : ''}
        ${t.async ? '<div class="ps-async">后台运行中，完成后自动发回结果</div>' : ''}
      </div>`;
    }).join('')}</div></div>`;
  if (c.type === 'kv') return `<div class="cardlet"><div class="ct">${esc(c.title)}</div>${(c.rows || []).map(r => `<div class="kv-row"><span>${esc(r[0])}</span><span>${esc(r[1])}</span></div>`).join('')}</div>`;
  if (c.type === 'list') return `<div class="cardlet"><div class="ct">${esc(c.title)}</div><ul>${(c.items || []).map(i => `<li>${esc(i)}</li>`).join('')}</ul></div>`;
  if (c.type === 'issues') return `<div class="cardlet"><div class="ct">${esc(c.title)}</div>${(c.items || []).map(i => `<div class="issue-line">${levelChip(i.level)} <b>${esc(i.title)}</b> <span class="hint">· ${esc(i.section)} · ${esc(i.source || '规则')}</span><div class="hint">${esc((i.detail || '').slice(0, 90))}</div>${i.anchor ? `<div class="hint">命中：${esc(i.anchor.slice(0, 70))}</div>` : ''}</div>`).join('') || '<div class="hint">无</div>'}</div>`;
  if (c.type === 'docs') return `<div class="cardlet"><div class="ct">${esc(c.title)}</div>${(c.items || []).map(d => `<div class="doc-line"><b>${esc(d.title)}</b> <span class="hint">${esc(d.chars)} 字</span><small>${esc(d.preview)}</small></div>`).join('') || '<div class="hint">暂无</div>'}</div>`;
  if (c.type === 'diff') return `<div class="cardlet"><div class="ct">${esc(c.title)}（${(c.items || []).length}）</div>${(c.items || []).map(f => `<div class="diff-line"><b>${esc(f.title)}</b> <span class="hint">· ${esc(f.section)}</span><div class="diff"><div class="before"><div class="diff-cap">改写前</div>${esc((f.before || '').slice(0, 220))}</div><div class="after"><div class="diff-cap">改写后</div>${esc((f.after || '').slice(0, 220))}</div></div>${(f.sources || []).length ? `<div class="hint">依据：${f.sources.map(esc).join('、')}</div>` : ''}</div>`).join('') || '<div class="hint">无</div>'}</div>`;
  if (c.type === 'auto') return `<div class="cardlet autolet"><div class="ct">${esc(c.title)}</div>
    <div class="auto-sum">${esc(c.summary || '')} <span class="hint">${c.elapsed_ms || 0} ms${c.cached ? ' · 命中缓存' : ''}</span></div>
    <div class="auto-items">${(c.items || []).map(x => `<div class="auto-item ${x.ok ? '' : 'bad'}">
      <span class="ai-name">${esc(x.name)}</span><span class="hint">${x.findings} 条</span>
      ${x.skipped ? '<span class="chip warn">需真实模型</span>' : ''}
      ${x.download ? `<a class="dl auto-dl" href="${esc(x.download)}" target="_blank">文件</a>` : ''}
    </div>`).join('')}</div></div>`;
  if (c.type === 'link') return `<div class="cardlet"><div class="ct">${esc(c.title)}</div><a class="dl" data-act="download" data-url="${esc(c.url)}" href="javascript:void(0)">${esc(c.label)}</a></div>`;
  if (c.type === 'cta') return `<div class="cardlet cta">
    <div class="ct">${esc(c.title || '下一步')}</div>
    ${c.text ? `<div class="plan-why">${esc(c.text)}</div>` : ''}
    <div class="row" style="margin-top:9px"><button class="btn primary" data-act="goto" data-tab="${esc(c.tab || 'write')}">${esc(c.label || '去看看')}</button></div>
  </div>`;
  return '';
}

function jobHtml(job) {
  const pending = job.status === 'running' || job.status === 'queued';
  if (pending) {
    const label = job.status === 'queued' ? '等待执行（前面还有任务在跑）' : (job.step || '运行中');
    return `<div class="cardlet"><div class="ct">后台任务</div><div class="progress"><div style="width:${job.progress || 0}%"></div></div><div class="job-status">${esc(label)}${job.status === 'queued' ? '' : ` · ${job.progress || 0}%`}</div></div>`;
  }
  if (job.status === 'lost') return `<div class="cardlet"><div class="job-status">连接中断：${esc(job.error || '没取到进度')} <button class="btn" data-act="rejob" data-jid="${esc(job.jid || '')}">重新获取</button></div></div>`;
  const tail = job.status === 'partial' ? '部分完成：' : job.status === 'done' ? '任务完成：' : '失败：';
  const res = job.result || {};
  const c = res.counts || {};
  const bits = [['written', '已写'], ['human', '人工补稿'], ['manual', '保护章节待人工'],
                ['skipped', '跳过'], ['conflict', '冲突'], ['empty', '空稿'],
                ['failed', '出错'], ['undeliverable', '未达标']]
    .filter(([k]) => c[k]).map(([k, label]) => `${label} ${c[k]} 章`);
  const extra = bits.length
    ? `<div class="job-note">实际写入：${esc(bits.join(' · '))}</div>` : '';
  const d = res.delivery || {};
  const delivery = d.label
    ? `<div class="job-note">交付状态：${esc(d.label)}（${esc(d.reason || '仍需质量、要求、附件与人工复核')}）</div>` : '';
  return `<div class="cardlet"><div class="job-status">${tail}${esc(job.step || '')}</div>${extra}${delivery}</div>`;
}

/* 上传解析期间锁住「发送」：文件正文还没解析出来时点发送，只会把一份空附件
   或上一个附件带过去。只局部更新按钮与提示条，不重建输入框，用户打字不被打断。 */
function uploadBusyHtml() {
  return `「<b>${esc(CHAT.uploadingName || '文件')}</b>」正在上传解析，<b>解析成功后「发送」才会开放</b>；
    可以先在下面把要问的话写好，解析完成后点一次「发送」，问题和文件一起提交。
    <div class="hint">大文件 / 扫描件解析可能要一会儿，请不要关闭页面。</div>`;
}
function syncComposerBusy() {
  const busy = !!CHAT.uploading;
  const sendBtn = $('#chatSendBtn');
  if (sendBtn) {
    sendBtn.disabled = busy || !!CHAT.sending;
    sendBtn.textContent = busy ? '解析中…' : (CHAT.sending ? '发送中…' : '发送');
  }
  const upBtn = $('#chatAttachBtn');
  if (upBtn) upBtn.disabled = busy;
  const hint = $('#chatModeHint');
  if (hint) hint.textContent = busy ? '文件解析中，发送暂不可用'
    : (S.mode === 'demo' ? '本地演示' : 'AI 模型');
  let bar = $('#uploadBusyBar');
  if (busy) {
    if (!bar) {
      const composer = $('#tab-chat .composer');
      if (!composer) return;
      bar = document.createElement('div');
      bar.id = 'uploadBusyBar';
      bar.className = 'attach-bar attach-uploading';
      composer.insertBefore(bar, composer.firstChild);
    }
    bar.innerHTML = uploadBusyHtml();
  } else if (bar) bar.remove();
}

function renderChat() {
  // 用途没判准被退回时，把用户刚写的那句话回填，别让他重写一遍
  const prev = CHAT.pendingText || (($('#chatInput') || {}).value || '');
  CHAT.pendingText = '';
  syncAttach();   // 每次重渲都把「未发送附件」同步到会话缓存，刷新也不丢
  const pending = CHAT.pending;
  const uf = CHAT.uploadFail;
  // 服务未启动 / 登录失效时，原件留存接口同样不可用，别再给一个会二次报错的按钮
  const ufSystem = !!uf && ['network', 'service_unavailable', 'unauthorized'].includes(uf.reason || '');
  // 正式项目里 29 条历史对话连结果卡片一起平铺会有一万多字，输入框被挤到最底下。
  // 默认只平铺最近几条，更早的收进可展开的历史区，功能一条不删。
  const msgs = CHAT.messages || [];
  const KEEP_RECENT = 6;
  const split = Math.max(0, msgs.length - KEEP_RECENT);
  const older = msgs.slice(0, split), recent = msgs.slice(split);
  const msgHtml = m => `
        <div class="msg ${m.role}">
          <div class="avatar">${m.role === 'user' ? '我' : '标'}</div>
          <div class="bubble">
            ${m.restored && m.role === 'agent' && ((m.cards || []).length || m.job) ? '<div class="src-line">历史记录 · 当时的结果卡片与执行轨迹已还原</div>' : ''}
            <div class="reply">${fmt(m.text || m.reply)}</div>
            ${(m.cards || []).map(cardHtml).join('')}
            ${m.job ? jobHtml(m.job) : ''}
            ${(m.sources || []).length ? `<div class="src-line">本次依据：${m.sources.map(esc).join(' · ')}</div>` : ''}
          </div>
        </div>`;
  const historyHtml = older.length ? `<details class="fold chat-history">
      <summary>更早的 ${older.length} 条对话（点开看当时的结果卡片与执行轨迹）</summary>
      <div class="chat-history-body">${older.map(msgHtml).join('')}</div>
    </details>` : '';
  $('#tab-chat').innerHTML = `
    ${startCardHtml()}
    <div class="chat-scroll" id="chatScroll">
      ${older.length ? `<div class="src-line" style="margin:0 0 10px 46px">共 ${msgs.length} 条对话，下面只显示最近 ${recent.length} 条</div>` : ''}
      ${historyHtml}
      ${recent.map(msgHtml).join('')}
    </div>
    ${uf ? `<div class="attach-bar">
      「<b>${esc(uf.filename)}</b>」没有入库：${esc(uf.msg)}
      <div class="hint">${ufSystem
        ? '服务或登录恢复后，重新点「上传文件」选择同一个文件即可；本次没有解析、也没有入库。'
        : '有效识别 0 字时不算导入成功。可以只把原件留成「待处理」（不进依据、不参与核查），之后 OCR 或手工补录再重传。'}</div>
      <div class="row">
        ${ufSystem ? '' : '<button class="btn mini" data-act="keepOriginal">仅留存原件（待处理）</button>'}
        <button class="btn mini danger" data-act="dismissUploadFail">知道了</button>
      </div></div>` : ''}
    <div class="quick">${QUICKS.slice(0, 4).map(q => `<button data-act="quick" data-action="${q[1]}" data-text="${esc(q[2])}">${esc(q[0])}</button>`).join('')}
      <details class="quick-more"><summary>更多指令</summary>
        <div class="quick-more-body">${QUICKS.slice(4).map(q => `<button data-act="quick" data-action="${q[1]}" data-text="${esc(q[2])}">${esc(q[0])}</button>`).join('')}</div>
      </details></div>
    <div class="composer">
      ${CHAT.uploading ? `<div class="attach-bar attach-uploading" id="uploadBusyBar">${uploadBusyHtml()}</div>` : ''}
      ${pending ? `<div class="attach-bar attach-chip${CHAT.sending ? ' is-sending' : ''}">
        <div class="attach-chip-main">
          <span class="attach-chip-tag">${CHAT.sending ? '正在发送' : (CHAT.attachNote ? '附件未入库' : '待发送附件')}</span>
          <b>${esc(pending.filename)}</b>
          <span>有效 ${pending.effective_chars || pending.chars || 0} 字${pending.pages ? '，共 ' + pending.pages + ' 页' : ''}</span>
          <span class="grow"></span>
          <button class="btn mini danger" data-act="cancelAttach" ${CHAT.sending ? 'disabled' : ''}>移除</button>
        </div>
        ${CHAT.attachNote ? `<div class="hint attach-note"><b>${esc(CHAT.attachNote)}</b></div>` : ''}
        <div class="hint">${CHAT.sending
          ? '这句话和文件正在一起提交；发送期间文件一直留在输入框，失败也不会丢。'
          : `${pending.target ? `用途已指定为「${esc(ATTACH_LABEL[pending.target] || pending.target)}」。` : '用途自动判断。'}文件和下面这句话一起发送，服务端会先按用途把文件存进资料库、再按你的话执行；一次发送就够，成功后附件自动消失，可以接着打字、继续发送。`}</div>
        ${CHAT.uploadHint ? `<div class="hint">上次没判准（${esc(CHAT.uploadHint.reason || '特征不足')}，把握 ${Number(CHAT.uploadHint.confidence || 0).toFixed(2)}）：你刚才那句话已留在输入框，选一个用途再点「发送」就行。</div>` : ''}
        ${pending.needs_review ? `<div class="hint">解析待核对：${esc(pending.review_note || '有内容未识别')}。发送后这份资料会标「待人工核对」，在补录或确认前不进入依据、不参与已核查。</div>` : ''}
        ${pending.limit_note ? `<div class="hint">能力限制：${esc(pending.limit_note)}。</div>` : ''}
        <div class="attach-chip-foot">
          ${CHAT.uploadHint ? `<button class="btn mini primary" data-act="pickTarget" data-target="${esc(CHAT.uploadHint.target)}">按「${esc(CHAT.uploadHint.label)}」用</button>` : ''}
          <details class="adv-inline attach-target"><summary>用途拿不准？手动指定（可选）</summary>
            <div class="attach-target-body">
              <button class="btn mini" data-act="pickTarget" data-target="auto">自动判断</button>
              <button class="btn mini" data-act="pickTarget" data-target="tender">招标文件</button>
              <button class="btn mini" data-act="pickTarget" data-target="draft">已有竞标书</button>
              <button class="btn mini" data-act="pickTarget" data-target="material">我提供的资料</button>
              <button class="btn mini" data-act="pickTarget" data-target="kb">企业素材库</button>
            </div>
          </details>
        </div>
      </div>` : ''}
      ${(!pending && (CHAT.recent || []).length) ? `<div class="src-line attach-recent">本次对话已入库：${CHAT.recent.map(x => esc(x.label || x.filename)).join(' · ')}。后面直接打字提问就行，系统会从资料库里引用，不用重复上传。</div>` : ''}
      <textarea id="chatInput" rows="2" placeholder="直接说要做什么，例如：把这份招标书变成投标书（Enter 发送 / Shift+Enter 换行）">${esc(prev)}</textarea>
      <div class="crow">
        <button class="upload-btn" id="chatAttachBtn" data-act="attach" ${CHAT.uploading ? 'disabled' : ''}>上传文件（docx / pdf / xlsx / txt）</button>
        <span class="grow"></span>
        <span class="hint" id="chatModeHint">${CHAT.uploading ? '文件解析中，发送暂不可用' : (S.mode === 'demo' ? '本地演示' : 'AI 模型')}</span>
        <button class="btn primary" id="chatSendBtn" data-act="send" ${CHAT.uploading || CHAT.sending ? 'disabled' : ''}>${CHAT.uploading ? '解析中…' : (CHAT.sending ? '发送中…' : '发送')}</button>
      </div>
    </div>`;
  const sc = $('#chatScroll'); if (sc) sc.scrollTop = sc.scrollHeight;
  syncComposerBusy();
}

/* 把没入库的文件退回输入框。资料页「定向上传」失败时，不能顶掉输入框里
   已经挂着的另一个待发送附件，否则用户会莫名丢文件。 */
function keepAttachment(flight, fromComposer, note) {
  if (!flight) return;
  if (!fromComposer && CHAT.pending && CHAT.pending.filename !== flight.filename) return;
  CHAT.pending = flight;
  CHAT.attachNote = note;
}

async function sendChat(text, action, attachment) {
  if (CHAT.uploading) {
    // 按钮已置灰，但 Enter / 快捷指令仍可能绕过来；这里兜底拦住，且不丢刚打的字
    if (text) { CHAT.pendingText = text; renderChat(); }
    toast('文件还在解析，解析完成后「发送」会开放；你写的话已留在输入框');
    return false;
  }
  if (CHAT.sending || !S.pid) {
    // 上一句还没跑完时不清输入框：用户刚打的字要留在原地，等他再点一次发送
    if (text) { CHAT.pendingText = text; renderChat(); }
    if (CHAT.sending) toast('上一句还在执行，内容已留在输入框，等它跑完再发');
    return false;
  }
  let att = attachment || null;
  // 资料页的定向上传带显式 attachment，成功/失败都不该动输入框里挂着的另一个附件
  const fromComposer = !attachment && !!CHAT.pending;
  if (!att && CHAT.pending) {
    // 对话框上传的文件只挂在这里，等「问题 + 文件」一起发送才提交。
    // 不再单独发一条空消息：那会让用途判断看不到用户的话，也会多记一条上传记录。
    const { chars, ...rest } = CHAT.pending;
    att = { ...rest };
  }
  // 发送前**不清附件**：只有服务端确认入库或明确退回，才决定去留；
  // 网络异常 / 502 / 登录过期 / 200 但没入库，附件都必须还在输入框里。
  const flight = att ? { ...att, chars: (att.text || '').length } : null;
  const act = att ? '' : (action || '');
  CHAT.uploadHint = null;
  CHAT.attachNote = '';
  // 附件属于「输入框里还没发出去的内容」，不占聊天气泡；记录里只留用户自己打的字
  if (text) CHAT.messages.push({ role: 'user', text });
  CHAT.sending = true; renderChat();
  try {
    // 对话记忆由服务端落库并回读，不依赖浏览器内存；刷新 / 换设备仍连续
    const r = await api('/api/chat', 'POST', {
      pid: S.pid, message: text || '', action: act, attachment: att,
    });
    const stored = attachmentStored(r, att);
    if (r.need_confirm && !stored) {
      // 兼容旧服务端：用途反问且确实没入库，附件和刚写的那句话一起退回
      CHAT.uploadHint = r.need_confirm;
      keepAttachment(flight, fromComposer,
        '这次没有入库：用途没判准。文件还在输入框，选一个用途再点「发送」即可，不用重新上传。');
      CHAT.pendingText = text || '';
      CHAT.messages.push({ role: 'agent', reply: r.reply, cards: [], sources: [] });
      return false;
    }
    if (att && !stored) {
      // 200 不等于收到文件：没解析出有效文字、入库出错都要把真实原因写在附件条上
      // 新后端一定会返回 attachment_stored；完全没有这个字段 = 连的还是旧进程，
      // 这时再怎么点发送都不会入库，必须明确说清，别让用户一直重试。
      const legacy = typeof r.attachment_stored === 'undefined';
      keepAttachment(flight, fromComposer, legacy
        ? '当前连的是旧版本服务：响应里没有入库确认，这次文件没有保存，点多少次发送都一样。请打开新版预览地址，或重启本地服务；文件还在输入框，不用重新上传。'
        : r.attachment_summary
          ? `这次没有入库：${r.attachment_summary}。文件还在输入框，处理好后直接点「发送」重试即可，不用重新上传。`
          : '这次服务端没有确认入库。文件还在输入框，检查服务或网络后直接点「发送」重试即可，不用重新上传。');
      CHAT.pendingText = text || '';
    } else if (att) {
      if (fromComposer) { CHAT.pending = null; CHAT.attachNote = ''; }
      const target = r.attachment_target || att.target || '';
      CHAT.recent = [...(CHAT.recent || []), {
        filename: att.filename,
        label: `${att.filename}${target ? '（' + (ATTACH_LABEL[target] || target) + '）' : ''}`,
      }].slice(-3);
    } else {
      CHAT.attachNote = '';
    }
    const m = { role: 'agent', reply: r.reply, cards: r.cards || [], sources: r.sources || [] };
    CHAT.messages.push(m);
    if (r.job_id) {
      const card = (r.cards || []).find(c => c.type === 'job');
      m.job = { jid: r.job_id, kind: card ? card.kind : 'compose',
                status: r.status === 'running' ? 'running' : 'queued',
                progress: 0, step: r.step || '等待执行' };
      renderChat();
      pollChatJob(r.job_id, CHAT.messages.length - 1);
    }
    // 大脑改过的章节 / 问题清单 / 资料，页签同一次刷新就能看到，不再显示旧数据；
    // 刷新本身失败不能把「已入库」误判成「发送失败」，否则附件会被错误地留在输入框
    try { await refresh(); } catch (_) { /* 页签刷新失败不影响本次发送结果 */ }
    return !att || stored;        // 资料页定向上传据此判断有没有真的导入成功
  } catch (e) {
    keepAttachment(flight, fromComposer,
      '发送失败，文件还留在输入框、没有入库；处理好后直接点「发送」重试即可，不用重新上传或重新解析。');
    CHAT.pendingText = text || '';
    toast(e.message);
    CHAT.messages.push({ role: 'agent', reply: '出错了：' + e.message, cards: [], sources: [] });
    return false;
  } finally {
    CHAT.sending = false; renderChat();
  }
}

function pollChatJob(jid, msgIndex) {
  let misses = 0;
  const tick = async () => {
    const m = msgIndex == null ? null : CHAT.messages[msgIndex];
    if (msgIndex != null && !m) return;
    let j;
    try { j = await api('/api/jobs/' + jid); misses = 0; }
    catch (e) {
      // 轮询偶尔断一下不能把任务卡成「失败」：连续 3 次取不到才停，给一个重新获取入口
      misses += 1;
      if (m) {
        m.job = { ...(m.job || {}), jid, status: misses >= 3 ? 'lost' : 'running',
                  step: `连接中断，正在重试（${misses}/3）`, error: e.message };
        renderChat();
      }
      if (misses >= 3) return;
      setTimeout(tick, 1500);
      return;
    }
    if (m) m.job = { ...(m.job || {}), jid, status: j.status, progress: j.progress, step: j.step };
    renderChat();
    if (j.status === 'queued' || j.status === 'running') { setTimeout(tick, 500); return; }
    try {
      const r = await api('/api/chat/job/' + jid);
      if (r.pending) { setTimeout(tick, 500); return; }
      CHAT.messages.push({ role: 'agent', reply: r.reply, cards: r.cards || [], sources: r.sources || [] });
      CHAT.resumed.add(jid);
      await refresh();
    } catch (e) { toast(e.message); }
  };
  tick();
}

async function resumePendingJobs() {
  // 刷新 / 换设备回来：还在跑的任务继续轮询，跑完但没看的结果补回对话
  try {
    const r = await api('/api/chat/pending?pid=' + encodeURIComponent(S.pid));
    for (const j of (r.jobs || [])) {
      if (CHAT.resumed.has(j.id)) continue;
      CHAT.resumed.add(j.id);
      CHAT.messages.push({ role: 'agent', reply: `后台任务「${j.kind === 'compose' ? '整本生成' : '优化复检'}」还在跑，已自动续上进度；结果出来会补进对话。`, cards: [], sources: [] });
      pollChatJob(j.id, CHAT.messages.length - 1);
    }
    if ((r.jobs || []).length) renderChat();
  } catch (_) { /* 没有待恢复任务 */ }
}

function setUploadFail(f, msg, reason) {
  CHAT.uploadFail = { filename: f.name, msg, reason: reason || '', file: f };
  renderChat(); toast(msg);
}

async function onFilePicked(e) {
  const f = e.target.files[0]; e.target.value = '';
  if (!f) return;
  if (CHAT.uploading) return toast('上一个文件还在解析，等它解析完再选下一个');
  const fd = new FormData(); fd.append('file', f);
  // 解析期间「发送」置灰：正文没解析出来之前，不能把文件跟着问题一起提交
  CHAT.uploading = true; CHAT.uploadingName = f.name;
  syncComposerBusy();
  toast('正在解析 ' + f.name);
  let r = null, j = null, networkErr = false;
  try {
    r = await fetch('/api/upload', { method: 'POST', body: fd });
    // 服务没启动时网关返回的是纯文本 502，不是 JSON；直接 r.json() 会抛英文异常
    j = await r.json().catch(() => null);
  } catch (_) { networkErr = true; }
  finally {
    // 解析一结束就开放「发送」。资料页的定向上传随后会调 sendChat，
    // 若这时还锁着，会把自己的入库请求一起挡掉。
    CHAT.uploading = false; CHAT.uploadingName = '';
    syncComposerBusy();
  }
  if (networkErr) {
    setUploadFail(f, '连不上本地服务，请确认「启动.bat」已运行，再刷新页面重试', 'network');
    return;
  }
  if (!r.ok || !j) {
    const d = (j && j.detail) || {};
    let msg, reason = '';
    if (!j) {
      msg = `本地服务没响应（HTTP ${r.status}），请确认「启动.bat」已运行，再刷新页面重试`;
      reason = 'service_unavailable';
    } else if (r.status === 401) {
      msg = '登录状态已失效，请重新登录后再上传';
      reason = 'unauthorized';
    } else {
      msg = typeof d === 'string' ? d : (d.msg || '解析失败');
      reason = (d && typeof d === 'object' && d.reason) || '';
    }
    setUploadFail(f, msg, reason);
    return;
  }
  CHAT.uploadFail = null;
  const att = { filename: j.filename, text: j.text,
                page_map: j.page_map || [], scan_pages: j.scan_pages || 0,
                ocr_pages: j.ocr_pages || 0, pages: j.pages || 0,
                effective_chars: j.effective_chars || 0,
                unread_pages: j.unread_pages || [], images: j.images || 0,
                parse_status: j.parse_status || 'ok', usable: j.usable !== false,
                needs_review: !!j.needs_review, review_note: j.review_note || '',
                limit_note: j.limit_note || '', ocr_engine: j.ocr_engine || '' };
  const extra = j.scan_pages ? `，其中 ${j.scan_pages} 页扫描件未识别（有效 ${j.effective_chars} 字，待人工核对）`
                : j.ocr_pages ? `，${j.ocr_pages} 页由本地 OCR 识别` : '';
  try {
    if (S.uploadTarget) {
      const target = S.uploadTarget; S.uploadTarget = '';
      await applyUpload(target, att, j.effective_chars);
    } else {
      const replaced = !!CHAT.pending && CHAT.pending.filename !== att.filename;
      CHAT.uploadHint = null;
      CHAT.attachNote = '';
      CHAT.pending = { ...att, chars: j.effective_chars };
      const t = $('.tab[data-tab="chat"]'); if (t) t.click();
      renderChat();
      const ci = $('#chatInput'); if (ci) ci.focus();   // 附件挂好后直接能打字
      toast(`${replaced ? '已替换上一个待发送附件；' : ''}已解析有效 ${j.effective_chars} 字${extra}；现在可以点「发送」，问题和文件一起提交`);
    }
  } catch (err) { toast(err.message); }
}

async function applyUpload(target, att, chars) {
  // 资料页 / 正文页的定向上传与对话框里挂着的附件互不干扰，不去清它
  // 正文页上传已有标书：直接切章入库，不绕去对话
  if (target === 'draft-import') {
    try {
      const r = await api(`/api/projects/${S.pid}/import`, 'POST', { text: att.text || '' });
      S.bidImport = false;
      S.skipDraft = true;
      await refresh();
      S.secId = ''; S.skipDraft = true; renderWrite();
      toast(`已按标书切出 ${r.chapters} 章，正文原样保留`);
    } catch (e) { toast(e.message); }
    return;
  }
  const ok = await sendChat('', '', { ...att, target });
  try { await refresh(); } catch (_) { /* 刷新失败不影响导入结果提示 */ }
  if (target === 'kb') { try { await loadKb(); } catch (_) { /* 忽略 */ } }
  toast(ok ? `已导入「${att.filename}」${chars ? '（' + chars + ' 字）' : ''}`
    : `「${att.filename}」这次没有导入成功，请回到对话框看附件提示，不用重新上传`);
}

/* ---------------- 工作台 ---------------- */
function renderOverview() {
  const p = S.project, meta = p.summary_parsed || {};
  const checks = p.checks || [];
  const lastRound = checks.length ? Math.max(...checks.map(c => c.round)) : 0;
  const recent = checks.filter(c => c.round === lastRound);
  const high = recent.filter(c => c.level === '高' && c.status !== 'ignored');
  const secs = p.sections || [];
  const empty = secs.filter(s => !(s.text || '').trim() && !s.protected);
  const run = (S.brainRuns || [])[0];
  const steps = (run && run.trace) || [];
  const todos = [];
  if (!(p.tender_text || '').trim()) todos.push(['还没有招标文件', '去资料页导入', 'goto', 'materials']);
  if (!meta.must || !meta.must.length) todos.push(['招标要求还没解读', '解读招标文件', 'ask', '重新解读招标文件']);
  if (!p.outline) todos.push(['还没有目录', '去定目录', 'goto', 'write']);
  if (empty.length) todos.push([`还有 ${empty.length} 章没写正文`, '去写正文', 'goto', 'write']);
  if (high.length) todos.push([`有 ${high.length} 条高风险问题`, '去检查与修改', 'goto', 'check']);
  const todoHtml = todos.map(t => t[2] === 'goto'
    ? `<div class="todo"><span>${esc(t[0])}</span><button class="btn mini" data-act="goto" data-tab="${esc(t[3])}">${esc(t[1])}</button></div>`
    : `<div class="todo"><span>${esc(t[0])}</span><button class="btn mini" data-act="ask" data-text="${esc(t[3])}">${esc(t[1])}</button></div>`).join('');
  const traceHtml = steps.length ? steps.slice(-8).map(t => `
    <div class="trace-step ${t.ok ? '' : 'bad'}"><span class="hint">${t.index}</span>
      <b>${esc(t.label || t.tool || '')}</b>
      <span class="chip ${t.ok ? 'ok' : 'warn'}">${t.ok ? '完成' : '未通过'}</span>
      <span class="hint">${esc(String(t.summary || '').slice(0, 90))}</span></div>`).join('')
    : '<div class="empty">还没有执行轨迹；在对话里说一句话，这里会显示真实跑过的步骤</div>';
  renderInto('tab-overview', `
    <div class="stat-grid">
      <div class="stat"><div class="n">${secs.length}</div><div class="t">标书章节</div></div>
      <div class="stat"><div class="n">${(p.materials || []).length}</div><div class="t">我提供的资料</div></div>
      <div class="stat stat-link" data-act="goto" data-tab="kb" title="点开管理知识库"><div class="n">${S.kbCount}</div><div class="t">企业素材库</div></div>
      <div class="stat"><div class="n">${recent.length}</div><div class="t">最近一轮问题</div></div>
    </div>
    <div class="card"><h2>${esc(p.name)} · 当前进度</h2>
      <div class="set-row"><span>招标文件 ${(p.tender_text || '').length} 字 · ${meta.must && meta.must.length ? `硬性要求 ${meta.must.length} 条` : '尚未解读'}</span>
        <span class="hint">大纲 ${p.outline ? '已生成' : '未生成'} · 待写正文 ${empty.length} 章 · 高风险 ${high.length} 条</span></div>
      ${todoHtml ? `<h3>下一步</h3>${todoHtml}` : '<p class="desc">当前没有待办：资料、大纲、正文与体检都已就绪，可以去导出交付。</p>'}
      <p class="desc">按钮会把你带到对应页面；需要 AI 自动跑的任务，过程与结果仍会回到「开始」页的对话里。</p>
    </div>
    <div class="card"><h2>最近一次做了什么 <span class="hint">${run ? `${esc(run.planner || '')} · ${esc(run.created_at || '')}` : '暂无'}</span></h2>
      ${run ? `<p class="desc">「${esc((run.message || '').slice(0, 80))}」，${esc(run.goal || '')}</p>` : ''}
      ${traceHtml}</div>
    <div class="card"><h2>招标书解读</h2>
      ${meta.summary ? `<div>${esc(meta.summary)}</div>` : '<div class="empty">尚未解读</div>'}
      ${(meta.must || []).length ? `<h3>硬性要求（${meta.must.length} 条）</h3>${meta.must.map(m => `<div class="doc" style="margin-bottom:6px">${esc(m)}</div>`).join('')}` : ''}
      <div class="row" style="margin-top:12px"><button class="btn" data-act="interpret">重新解读招标文件</button>
        <span class="hint">报价表、投标函等保护章节不会被自动改写。</span></div>
    </div>
    <div class="card"><h2>两种用法</h2>
      <p class="desc"><b>写新标书</b>：上传招标文件、写整本、检查、下载 Word。</p>
      <p class="desc"><b>改已有标书</b>：上传写好的标书、检查问题、采纳要改的、复查、下载。</p></div>`);
}

/* ---------------- 招标解读汇总（展开时才算，不给首屏添负担） ---------------- */
let briefCache = { pid: '', html: '' };

function briefHtml(r) {
  const line = s => `<div class="hint" style="margin:4px 0">· ${esc(s)}</div>`;
  const block = (title, arr) => arr && arr.length ? `<div style="margin-top:10px"><b>${esc(title)}</b>${arr.map(line).join('')}</div>` : '';
  const smp = (r.requirements || {}).samples || {};
  const cert = (r.certificates || {}).items || [];
  // P4：招标要素维度、★▲ 参数、按章重点，都与货物清单分开列
  const dims = (r.dimensions || {}).rows || [];
  const found = dims.filter(x => x.status === 'found');
  const missing = dims.filter(x => x.status !== 'found');
  const dimBlock = dims.length ? `<div style="margin-top:10px"><b>招标要素（${found.length}/${dims.length} 项已识别）</b>
    ${found.map(x => `<div class="hint" style="margin:4px 0">· ${esc(x.label)}：${esc(x.value)}${x.loc ? ` <span class="faint">[${esc(x.loc)}]</span>` : ''}</div>`).join('')}
    ${missing.length ? `<details class="fold" style="margin-top:6px"><summary>还有 ${missing.length} 项未识别</summary>${missing.map(x => `<div class="hint" style="margin:4px 0">· ${esc(x.label)}：未识别</div>`).join('')}</details>` : ''}
    </div>` : '';
  const star = r.star_params || {};
  const starBlock = (star.items || []).length ? `<div style="margin-top:10px"><b>★▲ 强制技术参数（${star.total || 0} 条，与货物清单分开）</b>
    ${star.items.map(x => `<div class="hint" style="margin:4px 0">· ${esc(x.mark)} ${esc(x.text)} <span class="faint">[${esc(x.loc || '')}]</span></div>`).join('')}
    ${star.truncated ? '<div class="hint">只显示前 40 条，完整以招标原件为准。</div>' : ''}
    <div class="hint">${esc(star.note || '')}</div></div>` : '';
  const focus = r.chapter_focus || [];
  const focusBlock = focus.length ? `<details class="fold" style="margin-top:10px"><summary>按章重点核对（${focus.length} 章，展开才看）</summary>
    ${focus.map(g => `<div style="margin-top:8px"><b>${esc(g.title)}</b>${(g.items || []).map(x => `<div class="hint" style="margin:4px 0">· ${esc(x.label)}：${esc(x.value)}</div>`).join('')}</div>`).join('')}
    </details>` : '';
  return `<p class="desc">${esc(r.summary || '')}</p>
    ${(r.lines || []).slice(0, 8).map(line).join('')}
    ${block('废标条款（前 6 条）', smp.rejection)}
    ${block('硬性要求（前 6 条）', smp.hard)}
    ${block('评分要素（前 6 条）', ((r.scoring || {}).rows || []).map(x => `${x.item || ''}${x.score ? ' · ' + x.score + ' 分' : ''}`))}
    ${block('证照有效期需核对', cert.map(x => `${x.title} · ${x.label || ''} ${x.valid_until || ''}`))}
    ${dimBlock}${starBlock}${focusBlock}
    <p class="hint" style="margin-top:10px">${esc(r.dimensions_note || '')}</p>
    <p class="hint" style="margin-top:10px">${esc(r.note || '')}</p>
    ${(r.errors || []).length ? `<p class="hint" style="color:#c0392b">${esc(r.errors.join('；'))}</p>` : ''}`;
}

function bindBrief() {
  const box = document.getElementById('briefBox');
  const body = document.getElementById('briefBody');
  if (!box || !body) return;
  if (briefCache.pid === S.pid && briefCache.html) {
    body.innerHTML = briefCache.html;
    box.dataset.loaded = '1';
  }
  box.addEventListener('toggle', async () => {
    if (!box.open || box.dataset.loaded) return;
    box.dataset.loaded = '1';
    try {
      const r = await api('/api/projects/' + S.pid + '/brief');
      briefCache = { pid: S.pid, html: briefHtml(r) };
      body.innerHTML = briefCache.html;
    } catch (_) {
      box.dataset.loaded = '';
      body.textContent = '汇总没读出来，收起后重新展开可重试。';
    }
  });
}

/* ---------------- 资料与知识库 ---------------- */
// 只做展示：真正拦不拦由后端 certificates.export_issues 按同一口径判定
function certChip(d) {
  const v = String(d && d.valid_until || '').trim().slice(0, 10);
  if (!v) return '';
  const t = new Date();
  const today = `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')}`;
  if (v < today) return `<span class="chip low">证照已过期 ${esc(v)}</span>`;
  const days = Math.round((Date.parse(v) - Date.parse(today)) / 86400000);
  if (days <= 60) return `<span class="chip low">${days} 天后到期</span>`;
  return `<span class="hint">有效期至 ${esc(v)}</span>`;
}

function docCard(d, act) {
  const open = S.docOpen === d.id;
  const text = d.content || '';
  return `<div class="doc ${open ? 'open' : ''}">
    <div class="dt"><span>${esc(d.title)}</span>
      ${d.needs_review && !d.confirmed_at ? '<span class="chip low">待核对</span>' : ''}
      ${certChip(d)}
      <span class="doc-acts">
        <button class="btn mini" data-act="docView" data-did="${esc(d.id)}">${open ? '收起' : '查看'}</button>
        <button class="btn mini danger" data-act="${act}" data-did="${esc(d.id)}">删除</button>
      </span></div>
    <div class="dc ${open ? 'open' : ''}">${esc(open ? text : text.slice(0, 150))}</div>
    <div class="dm">${text.length} 字 · ${esc((d.created_at || '').slice(5, 16))}</div>
  </div>`;
}

async function renderMaterials() {
  const p = S.project;
  const kb = (await api('/api/kb')).docs || [];
  const samples = kb.filter(d => d.kind === 'sample').length;
  S.kbCount = kb.length;
  const tender = (p.tender_text || '').length;
  const reqs = (S.reqTable && S.reqTable.effective) || 0;
  let parsing = { docs: [], tender: null, originals: [], count: 0 };
  try { parsing = await api('/api/projects/' + S.pid + '/parsing'); } catch (_) { /* 忽略 */ }
  const pend = parsing.docs || [];
  const tp = parsing.tender;
  const tenderPending = !!(tp && tp.needs_review && !tp.confirmed_at);
  const parseCard = (parsing.count || (parsing.originals || []).length) ? `
    <div class="card">
      <h2>解析待核对 <span class="chip low">${parsing.count} 项</span>
        <span class="hint">未核对范围不算已核查，导出会被拦住</span></h2>
      ${tenderPending ? `<div class="list-item">
        <div class="li-title">招标文件 · ${esc(tp.filename)}</div>
        <div class="li-sub">${esc(tp.review_note || '有页面未识别')}${tp.unread_pages && tp.unread_pages.length ? ' · 未识别页 ' + tp.unread_pages.slice(0, 20).join('、') : ''}</div>
        ${tp.image_text ? `<details style="margin-top:8px">
          <summary class="hint" style="cursor:pointer">看看内嵌图片里写了什么（${tp.image_ocr_count || 0} 张识别到文字，仅供核对）</summary>
          <div class="hint" style="margin:6px 0">下面是图片 OCR 的文字，<b>没有并入招标正文、不作为依据</b>。请对照原件确认里面没有遗漏的招标要求；确认后正文才进入依据池。</div>
          <pre style="white-space:pre-wrap;word-break:break-all;max-height:260px;overflow:auto;margin:0;padding:8px;border:1px solid var(--line,#ddd);border-radius:6px;font-size:12px">${esc(tp.image_text)}</pre>
        </details>` : ''}
        <div class="row" style="margin-top:8px"><button class="btn mini" data-act="confirmTender">我对照原件核对过了</button></div>
      </div>` : ''}
      ${pend.map(d => `<div class="list-item">
        <div class="li-title">${esc(d.title)} <span class="chip low">待核对</span></div>
        <div class="li-sub">${esc(d.review_note || '有内容未识别')} · 有效 ${d.effective_chars || 0} 字${(d.unread_pages || []).length ? ' · 未识别页 ' + d.unread_pages.slice(0, 20).join('、') : ''}</div>
        <div class="row" style="margin-top:8px">
          <input type="number" min="1" id="pageNo-${esc(d.id)}" placeholder="页码" style="width:76px" />
          <textarea id="pageText-${esc(d.id)}" rows="2" placeholder="粘贴该页原文…" style="flex:1"></textarea>
          <button class="btn mini" data-act="addPageText" data-did="${esc(d.id)}">补录本页</button>
        </div>
        <div class="row" style="margin-top:6px">
          <input type="text" id="reviewNote-${esc(d.id)}" placeholder="核对说明（可选，例如：扫描页不含实质条款）" style="flex:1" />
          <button class="btn mini" data-act="confirmDoc" data-did="${esc(d.id)}">人工确认</button>
        </div>
      </div>`).join('')}
      ${(parsing.originals || []).length ? `<div class="hint" style="margin-top:8px">仅留存的原件（待处理，不进依据）：${parsing.originals.map(o => esc(o.filename)).join('、')}</div>` : ''}
    </div>` : '';
  let goods = null;
  try { goods = await api('/api/projects/' + S.pid + '/goods'); } catch (_) { /* 忽略 */ }
  const goodsRows = (goods && goods.rows || []).slice(0, 20);
  const goodsCard = goods && goods.found ? `
    <div class="card">
      <h2>货物清单 <span class="chip gray">${goods.total} 行</span>
        <span class="hint">按招标文件原文抽取，不用再翻原件找数量</span></h2>
      <p class="desc">${esc(goods.summary || '')}${goods.missing_qty_count ? ` · <b>${goods.missing_qty_count} 行没读到数量</b>，请对照原件核对` : ''}</p>
      <details class="fold" style="margin-top:8px">
        <summary>展开清单（只显示前 ${goodsRows.length} 行，共 ${goods.total} 行）</summary>
        <div style="overflow:auto;margin-top:8px">
          <table style="width:100%;font-size:12px;border-collapse:collapse">
            <thead><tr><th align="left">物资名称</th><th align="left">规格 / 描述</th><th>单位</th><th>数量</th><th>分包</th><th>交货日期</th></tr></thead>
            <tbody>${goodsRows.map(r => `<tr>
              <td>${esc(r.name || '')}</td><td>${esc(String(r.spec || '').slice(0, 60))}</td>
              <td align="center">${esc(r.unit || '')}</td><td align="right">${esc(r.qty || '—')}</td>
              <td align="center">${esc(r.package || '')}</td><td align="center">${esc(r.delivery || '')}</td>
            </tr>`).join('')}</tbody>
          </table>
          ${goods.total > goodsRows.length ? `<div class="hint" style="margin-top:6px">还有 ${goods.total - goodsRows.length} 行未显示，完整清单以招标文件原件为准。</div>` : ''}
        </div>
      </details>
    </div>` : '';
  const briefCard = `
    <div class="card">
      <h2>招标解读汇总 <span class="hint">按规则抽取，逐项仍需人工核对</span></h2>
      <details class="fold" id="briefBox" style="margin-top:8px">
        <summary>展开看这本招标文件的要点（展开时才汇总）</summary>
        <div id="briefBody" class="hint" style="margin-top:8px">正在汇总…</div>
      </details>
    </div>`;
  renderInto('tab-materials', `
    ${parseCard}
    ${briefCard}
    ${goodsCard}
    <div class="card">
      <h2>招标文件 <span class="chip blue">第一步</span>
        <span class="hint">${tender ? `已导入 ${tender.toLocaleString()} 字` : '还没有内容'}</span></h2>
      <p class="desc">把招标文件粘进来，或直接上传文件。导入后点「提取要求」，系统会把必须逐条响应的条目列出来。</p>
      <textarea id="tenderText" rows="7" placeholder="把招标文件正文粘贴到这里…">${esc(p.tender_text || '')}</textarea>
      <div class="row" style="margin-top:10px">
        <button class="btn primary" data-act="saveTender">保存</button>
        <button class="btn" data-act="interpret">提取要求</button>
        <button class="upload-btn" data-act="uploadTo" data-target="tender">上传招标文件</button>
        <a class="btn mini" href="/vue/materials.html?pid=${encodeURIComponent(S.pid)}" target="_blank" rel="noopener">试用新版资料页</a>
        <span class="grow"></span>
        <span class="hint">${reqs ? `已提取 ${reqs} 条要求` : '还没提取要求'}</span>
      </div>
    </div>

    <div class="card">
      <h2>我提供的资料 <span class="chip blue">只对本项目生效</span>
        <span class="hint">${p.materials.length} 份</span></h2>
      <p class="desc">资质、业绩、报价、售后承诺等。写这一本标书时优先用这里的内容。</p>
      ${p.materials.length
        ? `<div class="doc-list">${p.materials.map(d => docCard(d, 'delMaterial')).join('')}</div>`
        : '<div class="empty">还没有资料。可以先上传资质、业绩，或者直接跳到写标书，边写边补。</div>'}
      ${S.matNew ? `<div class="kb-new">
          <label class="lbl">名称</label>
          <input type="text" id="matTitle" placeholder="例如：本项目授权与资质清单" />
          <label class="lbl">内容</label>
          <textarea id="matContent" rows="5" placeholder="粘贴文字内容…"></textarea>
          <div class="row" style="margin-top:10px"><button class="btn primary" data-act="addMaterial">保存到本项目</button><button class="btn" data-act="matNewToggle">取消</button></div>
        </div>` : ''}
      <div class="row" style="margin-top:12px">
        <button class="btn" data-act="matNewToggle">${S.matNew ? '收起' : '手动添加一份资料'}</button>
        <button class="upload-btn" data-act="uploadTo" data-target="material">上传文件当资料</button>
      </div>
    </div>

    <div class="card kb-entry">
      <div class="kb-entry-row">
        <div>
          <div class="kb-entry-title">知识库 <span class="chip gray">跨项目复用</span></div>
          <div class="desc">公司通用的素材，加上可以当范例的整本标书。已存 ${kb.length} 篇${samples ? `（其中标书范例 ${samples} 篇）` : ''}，写任何项目都会检索参考。</div>
        </div>
        <button class="btn" data-act="goto" data-tab="kb">打开知识库</button>
      </div>
    </div>

    <details class="fold">
      <summary>想看看写作时会引用什么？试一下检索</summary>
      <div class="row" style="margin-top:10px"><input type="text" class="grow" id="retrieveQ" placeholder="例如：售后服务响应时限与培训计划" /><button class="btn" data-act="retrieve">检索</button></div>
      <div id="retrieveResult" style="margin-top:12px"></div>
    </details>
    ${tenderVersionPanelHtml()}${assetsPanelHtml()}${factsPanelHtml()}${formsPanelHtml()}`);
  loadTenderVersions(); loadFacts(); loadForms(); bindBrief(); bindAssets();
}

/* ---------------- 知识库（跨项目复用：通用素材 + 标书范例） ---------------- */
const KB_LABEL = { material: '通用素材', sample: '标书范例' };

async function loadKb() {
  const r = await api('/api/kb');
  S.kbList = r.docs || [];
  S.kbCounts = r.counts || {};
  S.kbCount = r.counts ? r.counts.all : (r.total || 0);
  renderKbPage();
}

function kbFiltered() {
  const docs = S.kbList || [];
  const f = S.kbFilter || 'all';
  const q = (S.kbQ || '').trim().toLowerCase();
  return docs.filter(d => (f === 'all' || (d.kind || 'material') === f)
    && (!q || ((d.title || '') + ' ' + (d.content || '')).toLowerCase().includes(q)));
}

function kbCardsHtml() {
  const docs = kbFiltered();
  const q = (S.kbQ || '').trim();
  if (!docs.length) {
    if (q || (S.kbFilter || 'all') !== 'all') {
      return `<div class="kb-empty">没有符合条件的${q ? `「${esc(q)}」` : ''}条目，换个词或切到「全部」看看</div>`;
    }
    return `<div class="kb-empty"><b>知识库还是空的</b>把写好的标书整本传上来当范例（按章拆开，写作时照着写）；公司通用的资质、业绩、承诺可以放成「通用素材」</div>`;
  }
  return docs.map(d => {
    const open = S.kbOpen === d.id, editing = S.kbEdit === d.id;
    const pages = (d.page_map || []).length;
    const kind = d.kind === 'sample' ? 'sample' : 'material';
    const chapOpen = S.kbChapOpen === d.id;
    const chaps = S.kbChap[d.id] || [];
    const isRef = (S.project && d.id === (S.project.sample_doc_id || ''));
    if (kind !== 'sample') {
      return `<div class="kb-item ${open || editing ? 'open' : ''}">
        <div class="kb-row">
          <div class="kb-main">
            <div class="kb-title"><span class="kb-kind ${kind}">${KB_LABEL[kind]}</span>${esc(d.title)} ${certChip(d)}</div>
            <div class="kb-sub">${d.content.length.toLocaleString()} 字${pages ? ` · ${pages} 页` : ''} · ${esc((d.created_at || '').slice(5, 16))}${d.source_pid ? ' · 从项目成稿收录' : ''}</div>
            ${open || editing ? '' : `<div class="kb-peek">${esc((d.content || '').slice(0, 180))}</div>`}
          </div>
          <div class="kb-acts">
            ${editing ? '' : `<button class="btn mini" data-act="kbView" data-did="${esc(d.id)}">${open ? '收起' : '查看'}</button>
            <button class="btn mini" data-act="kbEdit" data-did="${esc(d.id)}">编辑</button>`}
            <button class="btn mini danger" data-act="kbDel" data-did="${esc(d.id)}">删除</button>
          </div>
        </div>
        ${open ? `<div class="kb-view">${esc(d.content || '（这一篇没有正文）')}</div>` : ''}
        ${editing ? kbEditorHtml(d, kind) : ''}
      </div>`;
    }
    // 标书范例：突出「按章参考」，先给目录，再给全文
    return `<div class="kb-item sample ${chapOpen || open || editing ? 'open' : ''}">
      <div class="kb-row">
        <div class="kb-main">
          <div class="kb-title"><span class="kb-kind sample">标书范例</span>${esc(d.title)}
            ${isRef ? '<span class="chip ok sm-chip">本项目参照</span>' : ''}</div>
          <div class="kb-sub">${d.content.length.toLocaleString()} 字 · ${d.n_top || 0} 章${d.n_chapters && d.n_top && d.n_chapters !== d.n_top ? ` / ${d.n_chapters} 节` : ''}${pages ? ` · ${pages} 页` : ''} · ${esc((d.created_at || '').slice(5, 16))}${d.source_pid ? ' · 从项目成稿收录' : ''}</div>
          <div class="kb-peek">写「技术方案」时自动取这本的「技术方案」整章作参照，学结构、层次和口径，不抄公司名与数字。</div>
        </div>
        <div class="kb-acts">
          ${editing ? '' : `<button class="btn mini" data-act="kbChapters" data-did="${esc(d.id)}">${chapOpen ? '收起目录' : '看目录'}</button>
          <button class="btn mini" data-act="kbUseOutline" data-did="${esc(d.id)}">用它建目录</button>
          <button class="btn mini ${isRef ? '' : 'primary'}" data-act="kbSetSample" data-did="${esc(d.id)}">${isRef ? '取消参照' : '设为参照'}</button>
          <button class="btn mini" data-act="kbEdit" data-did="${esc(d.id)}">编辑</button>`}
          <button class="btn mini danger" data-act="kbDel" data-did="${esc(d.id)}">删除</button>
        </div>
      </div>
      ${chapOpen ? `<div class="kb-chaps">${chaps.length ? chaps.map((c, i) => `
        <div class="kb-chap">
          <span class="kb-chap-idx lv${c.level || 1}">${i + 1}</span>
          <span class="kb-chap-t">${esc(c.title)}</span>
          <span class="hint">${c.chars != null ? c.chars : ''}${c.chars != null ? ' 字' : ''}</span>
        </div>`).join('') : '<div class="hint">正在拆章…</div>'}</div>` : ''}
      ${open ? `<div class="kb-view">${esc(d.content || '（这一篇没有正文）')}</div>` : ''}
      ${editing ? kbEditorHtml(d, kind) : ''}
    </div>`;
  }).join('');
}

function kbEditorHtml(d, kind) {
  return `<div class="kb-editor">
      <label class="lbl">名称</label>
      <input type="text" id="kbETitle" value="${esc(d.title)}" />
      <div class="row" style="margin-top:10px;align-items:center;gap:10px">
        <label class="lbl" style="margin:0">类型</label>
        <select id="kbEKind" class="kb-kind-select">
          <option value="material"${kind === 'material' ? ' selected' : ''}>通用素材（片段，写哪儿都能引用）</option>
          <option value="sample"${kind === 'sample' ? ' selected' : ''}>标书范例（整本，按章参考结构与措辞）</option>
        </select>
      </div>
      <label class="lbl">有效期至（选填，资质证书 / 执照 / 检测报告才填）</label>
      <input type="date" id="kbEValidUntil" value="${esc(String(d.valid_until || '').slice(0, 10))}" />
      <label class="lbl">正文</label>
      <textarea id="kbEContent" rows="10">${esc(d.content || '')}</textarea>
      <div class="row" style="margin-top:10px"><button class="btn primary" data-act="kbSave" data-did="${esc(d.id)}">保存</button><button class="btn" data-act="kbEdit" data-did="${esc(d.id)}">取消</button><span class="hint">改成范例或改了正文后，会自动重新按章拆分。</span></div>
    </div>`;
}

function kbListRefresh() {
  const box = $('#kbList'); if (box) box.innerHTML = kbCardsHtml();
  const bar = $('#kbFilterBar');
  if (bar) $$('#kbFilterBar .kb-tab').forEach(b => b.classList.toggle('on', (b.dataset.kind || 'all') === (S.kbFilter || 'all')));
}

function renderKbPage() {
  const c = S.kbCounts || {};
  const n = { all: c.all ?? (S.kbList || []).length, material: c.material ?? 0, sample: c.sample ?? 0 };
  const f = S.kbFilter || 'all';
  const tab = (kind, label) =>
    `<button class="kb-tab ${f === kind ? 'on' : ''}" data-act="kbFilter" data-kind="${kind}">${label}<b>${n[kind] || 0}</b></button>`;
  renderInto('tab-kb', `
    <div class="card">
      <h2>知识库 <span class="chip gray">跨项目复用</span></h2>
      <p class="desc">两样东西：<b>标书范例</b>——写好的整本标书，传上来后按章拆开，写某一章时照着它同一章的结构和写法；<b>通用素材</b>——公司资质、业绩、承诺这类片段，写到哪儿都能引用。只存在本机，不外发。</p>
      <div class="kb-tool">
        <input type="text" id="kbSearch" placeholder="搜标题或正文…" value="${esc(S.kbQ || '')}" />
        <button class="upload-btn" data-act="kbUpload">上传标书 / 素材文件</button>
        <button class="btn primary" data-act="kbNewToggle">${S.kbNew ? '收起' : '手动新增'}</button>
        <a class="btn mini" href="/vue/kb.html" target="_blank" rel="noopener">试用新版知识库</a>
      </div>
      ${S.kbNew ? `<div class="kb-new">
          <label class="lbl">名称</label>
          <input type="text" id="kbNewTitle" placeholder="范例写书名，素材写用途，例如：近三年同类项目业绩" />
          <label class="lbl">类型</label>
          <select id="kbNewKind" class="kb-kind-select">
            <option value="sample"${(S.kbNewKind || 'material') === 'sample' ? ' selected' : ''}>标书范例（整本，按章参考结构与措辞）</option>
            <option value="material"${(S.kbNewKind || 'material') === 'material' ? ' selected' : ''}>通用素材（片段，写哪儿都能引用）</option>
          </select>
          <label class="lbl">有效期至（选填，资质证书 / 执照 / 检测报告才填）</label>
          <input type="date" id="kbNewValidUntil" />
          <label class="lbl">正文</label>
          <textarea id="kbNewContent" rows="6" placeholder="粘贴内容…整本标书粘进来会自动按标题拆章"></textarea>
          <div class="row" style="margin-top:10px"><button class="btn primary" data-act="kbAdd">保存到知识库</button><button class="btn" data-act="kbNewToggle">取消</button><span class="hint">能切出 3 章以上的长文会自动归为「标书范例」，可随时改。</span></div>
        </div>` : ''}
    </div>
    <div class="card">
      <div class="kb-filter" id="kbFilterBar">
        ${tab('sample', '标书范例')}${tab('material', '通用素材')}${tab('all', '全部')}
        <span class="grow"></span><span class="hint">范例可以「看目录 / 用它建目录 / 设为参照」</span>
      </div>
      <div id="kbList">${kbCardsHtml()}</div>
    </div>
    <div class="hint">知识库跨项目共用；单个项目专用的资料请放「资料」页。删除后不可恢复。</div>`);
  const s = $('#kbSearch');
  if (s) s.oninput = () => { S.kbQ = s.value; kbListRefresh(); };
}

function renderKbTab() {
  renderKbPage();
  loadKb().catch(e => toast(e.message));
}

async function afterKbChange() {
  if (S.activeTab === 'materials') { try { await renderMaterials(); } catch (_) { /* 忽略 */ } }
  if (S.activeTab === 'overview' && S.project) { try { renderOverview(); } catch (_) { /* 忽略 */ } }
}

/* ---------------- 招标要求表（人工修订优先于自动抽取） ---------------- */
/* ---------------- B01/B02/B03/B04：业务完整性面板 ---------------- */
const REQ_STATUS_LABEL = {
  missing: '正文里还没响应', candidate: '只是词面命中（候选）',
  evidence_ready: '自动证据齐，待人工复核', confirmed: '已人工确认',
  na: '已标不适用（有理由）', review: '招标文件出新版，需重新复核',
};
const FACT_KIND_LABEL = {
  example: '表达范例（不作事实依据）', enterprise_fact: '当前企业事实',
  project_commitment: '本项目拟承诺值', attachment: '实际提交附件',
};

function bizCard(id, title, hint) {
  return `<div class="card"><h2>${title} <span class="hint">${hint}</span></h2><div id="${id}"><div class="hint">加载中…</div></div></div>`;
}
function reqMatrixPanelHtml() { return bizCard('reqMatrixPanel', '逐条响应矩阵', '每条必需项都要落实或明确标不适用；百分比只是起草进度'); }
function tenderVersionPanelHtml() { return bizCard('tenderVersionPanel', '招标文件版本链', '原文件 / 补遗 / 澄清 / 整份替换分开保存，旧要求转失效'); }
/* ---------------- P2 企业资产台账（默认收起，展开时才加载） ---------------- */
function assetsPanelHtml() {
  return `<details class="fold" id="assetsBox" style="margin-top:8px">
    <summary>企业资产台账：资质 / 人员 / 业绩 / 检测 / 获奖（展开才加载）</summary>
    <div id="assetsPanel" class="hint" style="margin-top:8px">正在读取…</div>
  </details>`;
}

function assetFormHtml(a, labels, mode) {
  const opts = Object.entries(labels || {}).map(([k, v]) =>
    `<option value="${esc(k)}"${a.category === k ? ' selected' : ''}>${esc(v)}</option>`).join('');
  const edit = mode === 'edit';
  return `<div class="row assets-form" style="margin-top:10px">
    <select id="assetCategory">${opts}</select>
    <input type="text" id="assetTitle" placeholder="资产名称（例：ISO9001 认证）" value="${esc(a.title || '')}" />
    <input type="text" id="assetHolder" placeholder="持证主体 / 人员" value="${esc(a.holder || '')}" />
    <input type="text" id="assetCode" placeholder="证书编号" value="${esc(a.code || '')}" />
    <input type="text" id="assetUntil" placeholder="有效期 YYYY-MM-DD" value="${esc(a.valid_until || '')}" />
    <input type="text" id="assetSource" placeholder="原件位置 / 附件编号" value="${esc(a.source_ref || '')}" />
    <input type="text" id="assetNote" placeholder="备注" value="${esc(a.note || '')}" />
    <button class="btn mini primary" data-act="${edit ? 'assetSave' : 'assetAdd'}"${edit ? ` data-aid="${esc(a.id)}"` : ''}>${edit ? '保存' : '登记资产'}</button>
    ${edit ? '<button class="btn mini" data-act="assetEditCancel">取消</button>' : ''}
  </div>`;
}

function assetPayload() {
  const v = id => (($('#' + id) || {}).value || '').trim();
  return { category: v('assetCategory'), title: v('assetTitle'),
    holder: v('assetHolder'), code: v('assetCode'), valid_until: v('assetUntil'),
    source_ref: v('assetSource'), note: v('assetNote') };
}

async function loadAssets() {
  const box = $('#assetsPanel'); if (!box || !S.pid) return;
  const fold = document.getElementById('assetsBox');
  try {
    const r = await api('/api/projects/' + S.pid + '/assets');
    const cats = r.categories || {};
    const chips = Object.keys(cats).map(k =>
      `<span class="chip ${cats[k].expired ? 'low' : 'gray'}">${esc((r.labels || {})[k] || k)} ${cats[k].count}</span>`).join('');
    const rows = (r.items || []).map(a => {
      const editing = S.assetEdit === a.id;
      const cls = (a.status === 'expired' || a.status === 'invalid') ? 'low'
        : a.status === 'expiring' ? 'warn' : a.status === 'ok' ? 'ok' : 'gray';
      return `<div class="list-item">
        <div class="row gap-sm"><span class="li-title">${esc(a.title)}</span>
          <span class="chip gray">${esc(a.category_label)}</span>
          <span class="chip ${cls}">${esc(a.label)}</span>
          <span class="faint mono row-end">${esc(a.valid_until || '未填有效期')}</span></div>
        <div class="li-sub">${esc(a.holder || '未填持证主体')} · ${esc(a.code || '未填编号')} · 原件 ${esc(a.source_ref || '未登记')} · ${esc(a.verification || 'unverified')}</div>
        ${a.note ? `<div class="li-sub">${esc(a.note)}</div>` : ''}
        ${editing ? assetFormHtml(a, r.labels, 'edit') : `<div class="row gap-sm" style="margin-top:6px">
          ${String(a.verification || '').startsWith('verified') ? '' : `<button class="btn mini" data-act="assetVerify" data-aid="${esc(a.id)}">我已对照原件核验</button>`}
          <button class="btn mini" data-act="assetEdit" data-aid="${esc(a.id)}">编辑</button>
          <button class="btn mini danger" data-act="assetDel" data-aid="${esc(a.id)}">删除</button></div>`}
      </div>`;
    }).join('');
    box.innerHTML = `
      <div class="asset-counts">${chips || '<span class="chip gray">还没有分类资产</span>'}</div>
      <div class="hint">${esc(r.summary || '')}</div>
      ${rows ? `<div class="list" style="margin-top:8px">${rows}</div>`
        : '<div class="empty">还没有登记企业资产。把资质、人员、业绩、检测报告、获奖证书先登记进来，写标书时才能核对缺哪份材料。</div>'}
      ${S.assetEdit ? '' : assetFormHtml({ category: 'qualification' }, r.labels, 'add')}
      <p class="hint" style="margin-top:10px">${esc(r.note || '')}</p>`;
    if (fold) fold.dataset.loaded = '1';
  } catch (e) {
    box.innerHTML = `<div class="hint">资产台账加载失败：${esc(e.message)}，收起后重新展开可重试。</div>`;
    if (fold) fold.dataset.loaded = '';
  }
}

function bindAssets() {
  const box = document.getElementById('assetsBox');
  if (!box) return;
  box.addEventListener('toggle', () => {
    if (box.open && !box.dataset.loaded) loadAssets();
  });
}

function factsPanelHtml() { return bizCard('factsPanel', '企业事实与原件附件', '范例只学写法；事实要带实体、单位、有效期与人工核验'); }
function formsPanelHtml() { return bizCard('formsPanel', '保护表单完成核验', '投标函 / 报价 / 授权 / 资质不参与 AI 改写，但照样核验字段、附件、签章'); }

async function loadReqMatrix() {
  const box = $('#reqMatrixPanel'); if (!box || !S.pid) return;
  try {
    const r = await api('/api/projects/' + S.pid + '/reqmatrix');
    const pct = Math.round((r.draft_progress || 0) * 100);
    const rows = (r.items || []).map(x => {
      const chip = x.status === 'confirmed' ? '<span class="chip ok">已确认</span>'
        : x.status === 'na' ? '<span class="chip gray">不适用</span>'
        : x.status === 'review' ? '<span class="chip low">待重新复核</span>'
        : x.status === 'evidence_ready' ? '<span class="chip blue">证据齐·待复核</span>'
        : '<span class="chip low">未完成</span>';
      const acts = x.status === 'confirmed' || x.status === 'na' ? ''
        : `<div class="row" style="margin-top:6px;gap:6px">
             <button class="btn mini" data-act="reqMatrixConfirm" data-rid="${esc(x.id)}">人工确认已落实</button>
             <button class="btn mini" data-act="reqMatrixNa" data-rid="${esc(x.id)}">标不适用（写理由）</button>
           </div>`;
      return `<div class="list-item">
        <div class="row gap-sm">${chip}<span class="li-title">${esc(x.text.slice(0, 90))}</span>
          <span class="faint mono row-end">${esc(x.loc || x.source_version || '')}</span></div>
        <div class="li-sub">${esc(REQ_STATUS_LABEL[x.status] || x.status)}
          ${x.section_id ? ' · 命中章节：' + esc(x.section_id) : ' · 未定位到响应章节'}
          ${x.copy_only ? ' · 注意：命中句可能只是照抄要求' : ''}
          ${x.missing_numbers && x.missing_numbers.length ? ' · 要求数值未落实：' + esc(x.missing_numbers.join('、')) : ''}</div>
        ${x.evidence ? `<div class="li-evi">响应证据：${esc(x.evidence.slice(0, 180))}</div>` : ''}
        ${(x.support_gaps || []).map(g => `<div class="hint">${esc(g)}</div>`).join('')}
        <button class="btn mini" data-act="reqSupport" data-rid="${esc(x.id)}">核对本条事实 / 证据 / 附件</button>
        <div id="reqSupport-${esc(x.id)}"></div>
        ${x.required ? acts : '<div class="hint">非必需项（供参考）</div>'}
      </div>`;
    }).join('');
    const openedSupport = [...box.querySelectorAll('[data-support-rid]')]
      .map(card => [card.dataset.supportRid, card,
        [...card.querySelectorAll('[data-field]')].map(input =>
          [input.dataset.field, input.type === 'checkbox' ? input.checked : input.value])]);
    const focused = box.contains(document.activeElement) ? document.activeElement : null;
    box.innerHTML = `
      <div class="row gap-sm" style="margin-bottom:10px;flex-wrap:wrap">
        <span class="chip ${r.deliverable ? 'ok' : 'low'}">${r.deliverable ? '必需项全部落实' : '还有 ' + r.pending + ' 条必需项未落实'}</span>
        <span class="hint">起草进度 ${pct}%（${r.confirmed}/${r.required} 已确认，${r.na} 条不适用）— 进度不等于可交付，最终看每条必需项</span>
        <button class="btn mini" data-act="reqSupportCandidates">生成候选关联</button>
      </div>
      ${r.scope_note ? `<div class="hint" style="margin-bottom:8px">${esc(r.scope_note)}</div>` : ''}
      ${rows ? `<div class="list">${rows}</div>` : '<div class="empty">还没有要求，先到「招标要求表」提取或新增</div>'}`;
    for (const [rid, card, fields] of openedSupport) {
      const slot = document.getElementById('reqSupport-' + rid);
      if (slot) {
        slot.replaceChildren(card);
        for (const [field, value] of fields) {
          const input = card.querySelector(`[data-field="${field}"]`);
          if (input.type === 'checkbox') input.checked = value; else input.value = value;
        }
      }
    }
    if (focused?.isConnected) focused.focus({ preventScroll: true });
  } catch (e) { box.innerHTML = `<div class="hint">矩阵加载失败：${esc(e.message)}</div>`; }
}

async function loadReqSupport(rid, resetLinkDraft = false) {
  const box = document.getElementById('reqSupport-' + rid);
  if (!box) return;
  const pid = S.pid;
  const request = box._supportRequest = (box._supportRequest || 0) + 1;
  const r = await api(`/api/projects/${S.pid}/reqmatrix/${encodeURIComponent(rid)}/support`);
  if (S.pid !== pid || !box.isConnected || box._supportRequest !== request) return;
  // 请求期间用户仍可编辑；以响应到达时的输入为准，防止迟到刷新清空选择。
  const draft = [...box.querySelectorAll('[data-field]')]
    .filter(input => !resetLinkDraft || !['target','relation','note'].includes(input.dataset.field))
    .map(input => [input.dataset.field, input]);
  const focused = box.contains(document.activeElement) ? document.activeElement : null;
  const p = r.policy || {};
  const opts = [
    ...(r.facts || []).map(f => ({v: 'fact:' + f.id, label: `事实 · ${f.entity} · ${f.field} · ${f.value}${f.unit || ''} · ${f.verification} · 有效期 ${f.valid_until || '未填'}`})),
    ...(r.attachments || []).map(a => ({v: 'attachment:' + a.id, label: '附件 · ' + a.filename})),
    ...(r.evidence || []).map(e => ({v: 'evidence:' + e.id, label: '原文 · ' + e.title + ' · ' + (e.snippet || '').slice(0, 40)}))
  ];
  box.innerHTML = `<div class="card" data-support-rid="${esc(rid)}">
    <div class="hint">先核对类型及本条事实的主体、字段、单位、适用范围，再选择支撑材料。关联只对当前要求生效。</div>
    <label>要求类型 <select data-field="type">${Object.entries(r.types).map(([k,v]) => `<option value="${esc(k)}" ${p.type === k ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select></label>
    <div class="row gap-sm" style="flex-wrap:wrap">
      ${[['entity','主体'],['field','事实字段'],['unit','单位'],['scope','适用范围'],['baseline','开标或评审日期']].map(([k,v]) => `<label>${v}<input data-field="${k}" ${k === 'baseline' ? 'type="date"' : ''} value="${esc(p[k] || '')}"></label>`).join('')}
    </div>
    <label><input type="checkbox" data-field="attachment_required" ${p.attachment_required ? 'checked' : ''}>本要求明确需要附件</label>
    <label><input type="checkbox" data-field="form_checked" ${p.form_checked ? 'checked' : ''}>已核对原件字段及签章</label>
    <button class="btn mini" data-act="reqSupportPolicy" data-rid="${esc(rid)}">保存类型核对</button>
    <div class="row gap-sm" style="margin-top:8px;flex-wrap:wrap">
      <select data-field="target"><option value="">选择本项目材料</option>${opts.map(o => `<option value="${esc(o.v)}">${esc(o.label)}</option>`).join('')}</select>
      <select data-field="relation"><option value="candidate">候选待核对</option><option value="supports">经核对支持本要求</option><option value="conflicts">与本要求冲突</option></select>
      <input data-field="note" placeholder="关联理由 / 响应位置">
      <button class="btn mini" data-act="reqSupportAdd" data-rid="${esc(rid)}">建立关联</button>
    </div>
    ${(r.links || []).map(l => `<div class="list-item">${esc(l.target_type)} · ${esc(l.target.entity || l.target.filename || l.target.title || l.target_id)} · ${esc(l.relation)}${l.fresh ? '' : ' · 材料已变化'}<div class="hint">${esc(l.note || '')}</div><button class="btn mini" data-act="reqSupportDelete" data-rid="${esc(rid)}" data-lid="${esc(l.id)}">解除关联</button></div>`).join('')}
  </div>`;
  for (const [field, previous] of draft) {
    const input = box.querySelector(`[data-field="${field}"]`);
    if (!input) continue;
    if (input.tagName === 'SELECT' && previous.innerHTML !== input.innerHTML) {
      const value = previous.value;
      previous.innerHTML = input.innerHTML;
      if ([...previous.options].some(o => o.value === value)) previous.value = value;
    }
    input.replaceWith(previous);
  }
  if (focused?.isConnected) focused.focus({ preventScroll: true });
}

async function loadTenderVersions() {
  const box = $('#tenderVersionPanel'); if (!box || !S.pid) return;
  try {
    const r = await api('/api/projects/' + S.pid + '/tender-versions');
    const labels = r.labels || {};
    const rows = (r.items || []).map(v => `<div class="list-item">
      <div class="row gap-sm"><span class="li-title">第 ${v.version} 版 · ${esc(labels[v.kind] || v.kind)}</span>
        ${v.id === r.current ? '<span class="chip blue">当前有效</span>' : '<span class="chip gray">历史</span>'}
        <span class="faint mono row-end">${esc((v.created_at || '').slice(0, 16))}</span></div>
      <div class="li-sub">${esc(v.filename || '未命名')}${v.note ? ' · ' + esc(v.note) : ''} · 内容摘要 ${esc(String(v.content_hash || '').slice(0, 12) || '—')}</div>
    </div>`).join('');
    box.innerHTML = `${rows ? `<div class="list">${rows}</div>` : '<div class="empty">还没有版本记录。上传或粘贴招标文件会自动记为第 1 版。</div>'}
      <div class="row" style="margin-top:10px;flex-wrap:wrap;gap:8px;align-items:center">
        <select id="tvKind" style="width:130px"><option value="amend">补遗</option><option value="clarify">澄清</option><option value="replace">整份替换</option></select>
        <input type="text" id="tvName" placeholder="文件名（可选）" style="width:170px" />
        <button class="btn mini" data-act="tenderVersionAdd">保存这一版</button>
        <span class="hint">补遗 / 澄清只追加受影响条款，原条款保留；整份替换会换掉当前正文。旧自动要求转失效，导出确认一并失效。</span>
      </div>
      <div class="row" style="margin-top:8px"><textarea id="tvText" rows="3" class="grow" placeholder="粘贴补遗 / 澄清 / 新版本正文…"></textarea></div>`;
  } catch (e) { box.innerHTML = `<div class="hint">版本链加载失败：${esc(e.message)}</div>`; }
}

async function loadFacts() {
  const box = $('#factsPanel'); if (!box || !S.pid) return;
  try {
    const r = await api('/api/projects/' + S.pid + '/facts');
    const rows = (r.items || []).map(f => `<div class="list-item">
      <div class="row gap-sm"><span class="li-title">${esc(f.entity || '未标实体')} · ${esc(f.field || '未标字段')}</span>
        <span class="chip ${f.kind === 'example' ? 'gray' : 'blue'}">${esc(FACT_KIND_LABEL[f.kind] || f.kind)}</span>
        <span class="faint mono row-end">${esc((f.value || '') + (f.unit || ''))}</span></div>
      <div class="li-sub">核验：${esc(f.verification || 'unverified')} · 有效期 ${esc(f.valid_until || '未填')} · 来源 ${esc(f.source_loc || '未标位置')}</div>
      ${f.kind !== 'example' && !String(f.verification || '').startsWith('verified')
        ? `<div class="row" style="margin-top:6px"><button class="btn mini" data-act="factVerify" data-fid="${esc(f.id)}">我已对照原件核验</button></div>` : ''}
    </div>`).join('');
    const atts = (r.attachments || []).map(a => `<div class="list-item">
      <div class="li-title">${esc(a.filename)}</div>
      <div class="li-sub">绑定事实：${esc(a.fact || a.entity || '未绑定')} · 来源：${esc(a.source_title || '未登记')} ${esc(a.source_loc || '')} · 第 ${a.page || 0} 页</div>
      <div class="row" style="margin-top:6px"><button class="btn mini" data-act="openAttachment" data-aid="${esc(a.id)}">打开原件</button></div>
    </div>`).join('');
    const gaps = (r.gaps || []).slice(0, 6).map(g => `<div class="li-sub">· ${esc(g.title)} — ${esc(g.detail || '')}</div>`).join('');
    box.innerHTML = `
      <div class="hint" style="margin-bottom:8px">${esc(r.note || '')}；本轮数值核对已排除 ${r.examples_excluded || 0} 份范例资料。</div>
      ${gaps ? `<div class="card" style="margin-bottom:8px"><h3>缺可核验事实 · 待补任务</h3>${gaps}</div>` : ''}
      ${rows ? `<div class="list">${rows}</div>` : '<div class="empty">还没有登记企业事实。正文里的金额、业绩、资质数值需要有可回跳的事实依据。</div>'}
      ${atts ? `<div class="list" style="margin-top:8px">${atts}</div>` : ''}
      <div class="row facts-form" style="margin-top:10px">
        <select id="factKind"><option value="enterprise_fact">当前企业事实</option><option value="project_commitment">拟承诺值</option><option value="example">表达范例</option></select>
        <input type="text" id="factEntity" placeholder="实体（公司/项目）" />
        <input type="text" id="factField" placeholder="字段" />
        <input type="text" id="factValue" placeholder="数值" />
        <input type="text" id="factUnit" placeholder="单位" />
        <input type="text" id="factUntil" placeholder="有效期 YYYY-MM-DD" />
        <input type="text" id="factLoc" placeholder="原件位置 / 页码" />
        <button class="btn mini" data-act="factAdd">登记事实</button>
      </div>
      <div class="row attach-form" style="margin-top:8px">
        <input type="file" id="attFile" />
        <input type="text" id="attNote" placeholder="附件用途（例：授权书原件）" />
        <button class="btn mini" data-act="attachUpload">上传原件附件</button>
        <span class="hint">附件可打开并回跳到事实 / 资料 / 页码；报价表请粘贴「名称 | 数量 | 单价 | 金额」行，系统用 Decimal 核算。</span>
      </div>`;
  } catch (e) { box.innerHTML = `<div class="hint">事实面板加载失败：${esc(e.message)}</div>`; }
}

async function loadForms() {
  const box = $('#formsPanel'); if (!box || !S.pid) return;
  try {
    const r = await api('/api/projects/' + S.pid + '/forms');
    const rows = (r.items || []).map(x => {
      const chip = x.ok ? '<span class="chip ok">已核验完成</span>' : '<span class="chip low">未完成</span>';
      const gaps = (x.gaps || []).map(g => `<div class="li-sub">缺口：${esc(g)}</div>`).join('');
      const pend = (x.pending || []).map((t, i) => {
        const target = (x.pending_targets || [])[i] || '';
        return `<div class="li-sub">待人工复核：${esc(t)}
          ${target ? `<button class="btn mini" data-act="formConfirm" data-sid="${esc(x.section_id)}" data-target="${esc(target)}">我核对过了</button>` : ''}</div>`;
      }).join('');
      return `<div class="list-item">
        <div class="row gap-sm">${chip}<span class="li-title">${esc(x.title)}</span>
          <span class="faint mono row-end">${esc(x.rule || '保护章节')}</span></div>
        ${x.short_form ? '<div class="li-sub">短表单：字段齐、无占位符，不套正文字数下限</div>' : ''}
        ${gaps}${pend}
      </div>`;
    }).join('');
    box.innerHTML = `${r.note ? `<div class="hint" style="margin-bottom:8px">${esc(r.note)}</div>` : ''}
      ${rows ? `<div class="list">${rows}</div>` : '<div class="empty">这本标书没有保护表单章节。</div>'}`;
  } catch (e) { box.innerHTML = `<div class="hint">表单核验加载失败：${esc(e.message)}</div>`; }
}

function renderRequirements() {
  const r = S.reqTable || { rows: [], total: 0, manual: 0, auto: 0, effective: 0, labels: {} };
  const labels = r.labels || { hard: '硬性要求', rejection: '废标条款', scoring: '评分项' };
  const rows = r.rows || [];
  const groups = Object.keys(labels).map(k => {
    const list = rows.filter(x => x.kind === k);
    return `<div class="card"><h2>${esc(labels[k])} <span class="hint">大项</span></h2>
      <div class="row" style="margin:4px 0 10px;align-items:center;gap:10px;flex-wrap:wrap">
        <span class="hint">${list.length} 条 · 生效 ${(r.by_kind && r.by_kind[k] || {}).effective || 0} 条</span>
        <span class="grow"></span>
        <button class="btn mini" data-act="reqAddIn" data-kind="${k}">＋ 新增一条</button>
        <button class="btn mini danger" data-act="reqGroupDel" data-kind="${k}" title="删除整个大项：这一组下面的要求会一起删掉">删除这一组</button>
      </div>
      ${list.map(x => {
        const dis = (x.status || 'active') === 'disabled';
        return `<div class="kv-row" style="align-items:flex-start;gap:8px;flex-wrap:wrap;padding:8px 0;border-bottom:1px solid rgba(128,128,128,.15)">
          <input type="text" data-role="reqText" data-rid="${esc(x.id)}" id="reqText-${esc(x.id)}" value="${esc(x.text)}" style="flex:1 1 380px;min-width:220px" ${dis ? 'disabled' : ''} />
          <input type="text" data-role="reqScore" data-rid="${esc(x.id)}" id="reqScore-${esc(x.id)}" value="${esc(x.score || '')}" placeholder="分值" style="width:70px" ${dis ? 'disabled' : ''} />
          <span class="chip ${x.source === 'manual' ? 'blue' : 'gray'}">${x.source === 'manual' ? '人工' : '自动'}</span>
          <button class="btn mini" data-act="reqSave" data-rid="${esc(x.id)}">保存</button>
          <button class="btn mini" data-act="reqToggle" data-rid="${esc(x.id)}" data-status="${dis ? 'active' : 'disabled'}">${dis ? '启用' : '停用'}</button>
          <button class="btn mini danger" data-act="reqDel" data-rid="${esc(x.id)}" title="只删这一条要求；上面的分类不会受影响">删除这一条</button>
          <span class="hint" style="flex:1 1 100%">${dis ? '<b>已停用</b> · ' : ''}${esc(x.note || '')}</span>
        </div>`;
      }).join('') || '<div class="empty">还没有这一类的要求，点「从招标文件重抽」或手动新增</div>'}
    </div>`;
  }).join('');
  renderInto('tab-requirements', `
    <div class="card">
      <h2>招标要求表 <span class="chip ${r.effective ? 'ok' : 'warn'}">生效 ${r.effective || 0} / ${r.total || 0} 条</span></h2>
      <p class="desc">自动抽取只是初稿。抽错、抽漏直接改这里：<b>人工修订优先</b>，大纲约束、逐章写作、硬性要求覆盖、评分项覆盖、导出门禁都读这张表。停用 = 不参与判定；改过或删过的行重抽不会自动补回，需要找回点「恢复被删要求」。</p>
      <div class="row" style="margin-top:10px">
        <button class="btn" data-act="reqSync">从招标文件重抽</button>
        <button class="btn mini" data-act="reqRestore">恢复被删要求</button>
        <button class="btn mini" data-act="goto" data-tab="write">去写标书</button>
        <a class="btn mini" href="/vue/requirements.html?pid=${encodeURIComponent(S.pid)}" target="_blank" rel="noopener">试用新版要求表</a>
        <a class="btn mini" href="/vue/reqmatrix.html?pid=${encodeURIComponent(S.pid)}" target="_blank" rel="noopener">试用新版逐条矩阵</a>
        <span class="hint">人工 ${r.manual || 0} 条 · 自动 ${r.auto || 0} 条 · 停用 ${r.disabled || 0} 条${(r.hidden_groups || []).length ? ` · 已删大项 ${r.hidden_groups.length} 个，点「恢复被删要求」可找回` : ''}</span>
      </div>
    </div>
    <div class="card">
      <h2>新增一条具体要求</h2>
      <p class="desc">每个分类（硬性要求 / 废标条款 / 评分项）是大项，下面每条要求是小项。<b>两类都能删</b>：小项用行尾的「删除这一条」，大项用分组右上角的「删除这一组」，删前都会让你确认一次。</p>
      <div class="row" style="flex-wrap:wrap;gap:8px;align-items:center">
        <select id="reqNewKind" style="width:130px"><option value="hard">硬性要求</option><option value="rejection">废标条款</option><option value="scoring">评分项</option></select>
        <input type="text" id="reqNewText" class="grow" placeholder="要求原文，例如：投标人须具备安防工程一级资质" style="min-width:260px" />
        <input type="text" id="reqNewScore" placeholder="分值" style="width:80px" />
        <button class="btn primary" data-act="reqAdd">加入要求表</button>
      </div>
    </div>
    ${groups}${reqMatrixPanelHtml()}`);
  loadReqMatrix();
}

async function afterReqChange(msg) {
  S.skipDraft = true;
  await refresh();
  toast(msg);
}

/* ---------------- 标书正文：左边目录，右边逐章改 ---------------- */
function secList() { return (S.project && S.project.sections) || []; }
function secWordTotal(list) { return (list || []).reduce((n, s) => n + (s.text || '').length, 0); }
function secCurrent() {
  const list = secList();
  if (!list.length) return null;
  const picked = list.find(s => s.id === S.secId);
  if (picked) return picked;
  // 没有选中时：优先落在还没写、且可自动写的章节，其次第一篇有正文的
  return list.find(s => !(s.text || '').trim() && !s.protected)
    || list.find(s => (s.text || '').trim())
    || list[0];
}

function writeTopHtml(list) {
  const written = list.filter(s => (s.text || '').trim()).length;
  const emptyOpen = list.filter(s => !(s.text || '').trim() && !s.protected);
  const emptyProt = list.filter(s => !(s.text || '').trim() && s.protected);
  const words = secWordTotal(list);
  const pct = list.length ? Math.round(written / list.length * 100) : 0;
  const job = S.composeJob;
  let primary;
  if (emptyOpen.length) {
    primary = `<button class="btn primary" data-act="compose" ${job ? 'disabled' : ''}>${job ? `生成中 ${job.progress || 0}%` : (written ? `写剩下 ${emptyOpen.length} 章` : '一键写整本')}</button>`;
  } else if (emptyProt.length) {
    primary = `<button class="btn primary" data-act="gotoSec" data-sid="${esc(emptyProt[0].id)}">手工补 ${emptyProt.length} 章保护内容</button>`;
  } else {
    primary = `<button class="btn primary" data-act="goto" data-tab="check">正文齐了，去检查</button>`;
  }
  return `<div class="write-top">
    <div class="wt-row">
      <div class="wt-info">
        <div class="wt-title">标书正文
          <span class="chip ${emptyOpen.length || emptyProt.length ? 'blue' : 'ok'}" id="wtCount">${written}/${list.length} 章已写</span>
          <span class="hint" id="wtWords">${words.toLocaleString()} 字</span>
        </div>
        <div class="hint">点左边任意一章，右边直接改；改完自动保存。</div>
        ${sampleLineHtml()}
      </div>
      <div class="wt-acts">
        ${primary}
        <button class="btn" data-act="outline" ${job ? 'disabled' : ''}>${list.length ? '重新生成大纲' : '生成大纲'}</button>
        <button class="btn" data-act="exportWord">下载 Word</button>
        <a class="btn mini" href="/vue/write.html?pid=${encodeURIComponent(S.pid)}" target="_blank" rel="noopener">试用新版正文页</a>
        <details class="wt-more">
          <summary class="btn">更多</summary>
          <div class="wt-more-body">
            <button class="btn mini" data-act="compose" data-force="1">重写整本（覆盖正文）</button>
            <button class="btn mini" data-act="bidToggle">导入已有标书</button>
            <button class="btn mini" data-act="outlineMemToggle">沿用旧项目的目录</button>
            <button class="btn mini" data-act="export">下载 Markdown</button>
            <label class="wt-opt"><input type="checkbox" id="blindExport" /> 暗标导出（隐去公司信息与签章）</label>
          </div>
        </details>
      </div>
    </div>
    <div class="wt-bar"><i id="wtBar" style="width:${pct}%"></i></div>
    ${job ? `<div class="wt-job"><span class="wt-spin"></span><span id="wtJobStep">${esc(job.step || '正在生成…')}</span><span class="grow"></span><b id="wtJobPct">${job.progress || 0}%</b><button class="btn mini" data-act="jobCancel">取消生成</button></div>` : ''}
  </div>`;
}

/* ---------------- C03：长任务取消 / 检查点 / 重试恢复 ---------------- */
function taskCtlPanelHtml() {
  const jid = S.lastJobId || (S.composeJob || {}).jid || '';
  return `<div class="card" id="taskCtlPanel">
    <h2>长任务控制 <span class="hint">取消 / 章节检查点 / 失败章重试与恢复</span></h2>
    <div class="hint">最近任务：${jid ? esc(jid) : '暂无'}</div>
    <div class="row gap-sm" style="margin-top:8px;flex-wrap:wrap">
      ${S.composeJob ? '<button class="btn mini" data-act="jobCancel">取消生成</button>' : ''}
      <button class="btn mini" data-act="jobCheckpoints" data-jid="${esc(jid)}" ${jid ? '' : 'disabled'}>查看章节检查点</button>
      <button class="btn mini" data-act="jobRetry" data-jid="${esc(jid)}" ${jid ? '' : 'disabled'}>重试失败章</button>
      <button class="btn mini" data-act="jobResume" data-jid="${esc(jid)}" ${jid ? '' : 'disabled'}>恢复中断任务</button>
    </div>
    <div class="stop" style="margin-top:8px">取消只在章节边界生效：已经发给模型的请求仍会在远端算完，取消不撤销远端计算，结果返回后不会写进正文。恢复按章节版本核对，任务开始后的人工稿一律保留。当前是单进程单线程池，多进程部署前需要跨进程任务认领 / 锁（尚未实现）。</div>
    <div id="taskCtlBox"></div>
  </div>`;
}

function renderTaskCtl(r) {
  const box = $('#taskCtlBox'); if (!box) return;
  const p = r.plan || {}, sum = r.summary || {};
  const labels = { written: '已写入', skipped: '已有正文', manual: '待人工', human: '人工补稿',
    kept_human: '保留人工稿', failed: '失败', empty: '空稿', conflict: '版本冲突',
    cancelled: '已取消', pending: '未开始', running: '进行中', done: '已完成' };
  const rows = (p.items || []).map(x => `<div class="list-item">
    <div class="row gap-sm"><span class="li-title">${esc(x.title)}</span>
      <span class="chip ${x.decision === 'resumable' ? 'blue' : x.decision === 'kept_human' ? 'ok' : 'gray'}">${esc(x.decision === 'resumable' ? '可恢复' : x.decision === 'kept_human' ? '保留人工稿' : labels[x.decision] || x.decision)}</span>
      <span class="faint row-end">${esc(labels[x.status] || x.status)}</span></div>
    <div class="li-sub">${esc(x.why || '')} · 检查点版本 v${x.base_ver} / 当前 v${x.cur_ver}</div>
  </div>`).join('');
  box.innerHTML = `<div class="row gap-sm" style="margin-top:8px;flex-wrap:wrap">
      <span class="chip">共 ${p.total || 0} 章</span>
      <span class="chip blue">可恢复 ${(p.resumable || []).length}</span>
      <span class="chip ok">保留人工稿 ${(p.kept_human || []).length}</span>
      <span class="hint">${esc(p.note || '')}</span></div>
    <div class="hint" style="margin:6px 0">任务状态 ${esc((r.job || {}).status || '')} · 检查点 ${sum.total || 0} 条 · ${esc((r.concurrency || {}).why || '')}</div>
    ${rows ? `<div class="list">${rows}</div>` : '<div class="empty">这个任务还没有章节检查点</div>'}`;
}

async function startTaskJob(el, mode) {
  const jid = el.dataset.jid;
  if (!jid) return toast('先跑一次整本生成，才有可恢复的检查点');
  busy(el, true, '提交中…');
  try {
    const r = await api(`/api/projects/${S.pid}/jobs/${jid}/${mode}`, 'POST', {});
    if (!r.ok) return toast(r.error || '没有可处理的章节');
    S.lastJobId = r.job_id;
    S.composeJob = { jid: r.job_id, progress: 0, step: '排队中…' };
    renderWrite();
    toast(`${mode === 'retry' ? '重试' : '恢复'}已提交：${r.sections} 章，保留人工稿 ${r.kept_human} 章`);
    pollCompose(r.job_id);
  } catch (e) { toast(e.message); } finally { busy(el, false); }
}

function writeEmptyHtml() {
  const hasTender = !!((S.project.tender_text || '').trim());
  const samples = S.samples || [];
  // 默认只给一条路：按招标文件生成目录。其余起步方式都还有，但收进折叠区，
  // 免得第一次用的人要在四个并列卡片里做选择题。
  const main = `<div class="card">
    <h2>先按招标文件生成目录</h2>
    <p class="desc">${hasTender
      ? '系统会按评分项和硬性要求排好章节。目录定好后，左边选章节、右边改内容。'
      : '第一步：先导入招标文件，才能按它的要求生成目录。'}</p>
    ${hasTender
      ? '<button class="btn primary" data-act="outline">生成目录</button>'
      : '<button class="btn primary" data-act="goto" data-tab="materials">去导入招标文件</button>'}
  </div>`;
  const others = `<details class="fold" style="margin-top:10px">
    <summary>其他起步方式：已有标书 / 旧项目目录 / 手动建章（不常用）</summary>
    <div class="start-grid" style="margin-top:10px">
      <div class="start-card"><b>照知识库里的标书范例</b>
        <div class="start-desc">${samples.length
          ? `按范例的章节结构建目录，写正文时也照着它同一章写（共 ${samples.length} 本）。`
          : '把写好的标书传进「知识库」，就能照它的结构写。'}</div>
        ${samples.length
          ? `<select id="emptySamplePick" class="kb-kind-select">${samples.map(d => `<option value="${esc(d.id)}">${esc(d.title)}（${d.n_top || d.n_chapters || 0} 章）</option>`).join('')}</select>
             <button class="btn" data-act="emptyUseSample">按它建目录</button>`
          : '<button class="btn" data-act="goto" data-tab="kb">去知识库上传范例</button>'}</div>
      <div class="start-card"><b>沿用以前项目的目录</b>
        <div class="start-desc">同类项目反复投标时最省事：只复制章节标题，不带旧正文。</div>
        <button class="btn" data-act="outlineMemToggle">选一个旧目录</button></div>
      <div class="start-card"><b>导入我写好的标书</b>
        <div class="start-desc">按你标书里的标题切章，正文原样保留，之后逐章改。</div>
        <button class="btn" data-act="bidToggle">上传标书</button></div>
    </div>
    <div class="row gap-sm" style="margin-top:12px;flex-wrap:wrap;align-items:center">
      <button class="btn" data-act="secAdd">＋ 手动加一章</button>
      <span class="hint">不想用 AI 生成目录，就从这里一章一章自己建；之后每章都能改名、删除。</span>
    </div>
  </details>`;
  return main + others;
}

/* 正文页顶部：当前照着哪本范例写 */
function sampleLineHtml() {
  const p = S.project || {};
  if (p.sample_title) {
    return `<div class="sample-line"><span class="kb-kind sample">参照范例</span>
      <b>${esc(p.sample_title)}</b>
      <span class="hint">每章会按这本书的同名章节组织内容，不照抄其中的公司名与数字</span>
      <button class="btn mini" data-act="goto" data-tab="kb">换一本</button>
      <button class="btn mini" data-act="clearSample">不用范例</button></div>`;
  }
  return `<div class="sample-line quiet">还没指定参照范例。<button class="btn mini" data-act="goto" data-tab="kb">去知识库选一本写好的标书</button>
    <span class="hint">指定后，写每一章都会照它同一章的结构与写法</span></div>`;
}

function outlineMemHtml() {
  if (S.outlineMem === 'loading') {
    return `<div class="card"><h2>以前项目的目录</h2><div class="empty">读取中…</div></div>`;
  }
  const items = Array.isArray(S.outlineMem) ? S.outlineMem : [];
  return `<div class="card">
    <h2>沿用以前项目的目录 <span class="chip gray">项目记忆</span></h2>
    <p class="desc">只复制章节标题，旧项目的正文不会带过来。复制后随时可以逐章写或删章节。</p>
    ${items.length ? items.map(it => `<div class="mem-row">
      <div class="mem-main">
        <b>${esc(it.name)}</b>
        <span class="hint">${it.count} 章 · ${it.written} 章有正文 · ${esc((it.created_at || '').slice(0, 10))}</span>
        <div class="mem-titles">${it.titles.slice(0, 6).map(esc).join(' · ')}${it.titles.length > 6 ? ' …' : ''}</div>
      </div>
      <button class="btn mini primary" data-act="outlineApply" data-pid="${esc(it.pid)}">用这个目录</button>
    </div>`).join('') : '<div class="empty">还没有可以沿用的目录：先在别的项目里生成过一次目录。</div>'}
    <div class="row" style="margin-top:10px"><button class="btn mini" data-act="outlineMemToggle">收起</button></div>
  </div>`;
}

function bidImportHtml() {
  return `<div class="card">
    <h2>导入我写好的标书</h2>
    <p class="desc">上传 docx / pdf，或把正文粘进来；按标题自动切章，正文原样保留，之后再逐章改。</p>
    <div class="row">
      <button class="upload-btn" data-act="uploadTo" data-target="draft-import">上传标书文件</button>
      <span class="hint">支持 docx / pdf / txt / md，Word 标题层级最准</span>
    </div>
    <textarea id="importText" rows="5" placeholder="第一章 投标函&#10;致：xxx 招标人&#10;……"></textarea>
    <div class="row" style="margin-top:9px">
      <button class="btn primary" data-act="importDraft">按粘贴内容导入</button>
      <button class="btn" data-act="bidToggle">收起</button>
      <span class="hint">导入会替换当前目录；同名章节的正文会保留。</span>
    </div>
  </div>`;
}

function chapListHtml(list, all = list) {
  return list.map((s) => {
    const i = Math.max(0, all.indexOf(s));
    const done = !!(s.text || '').trim();
    const on = s.id === S.secId;
    const gen = S.genBusy === s.id || (S.composeJob && S.genTitle === s.title);
    return `<button class="cp-item ${on ? 'on' : ''} ${done ? 'done' : ''} ${s.protected ? 'prot' : ''}" id="cp-${esc(s.id)}" data-act="secPick" data-sid="${esc(s.id)}">
      <span class="cp-idx">${String(i + 1).padStart(2, '0')}</span>
      <span class="cp-main">
        <span class="cp-t">${esc(s.title)}</span>
        <span class="cp-sub">${gen ? '正在生成…' : done ? `${(s.text || '').length.toLocaleString()} 字` : '还没写'}${s.protected ? ' · 保护' : ''}</span>
      </span>
      ${gen ? '<span class="cp-spin"></span>' : `<span class="cp-dot ${done ? 'done' : ''}"></span>`}
      <span class="cp-ops">
        <span class="cp-op" title="上移一章" data-act="secMove" data-sid="${esc(s.id)}" data-dir="-1">↑</span>
        <span class="cp-op" title="下移一章" data-act="secMove" data-sid="${esc(s.id)}" data-dir="1">↓</span>
        <span class="cp-op" title="改这一章的名字" data-act="secRename" data-sid="${esc(s.id)}">✎</span>
        <span class="cp-op danger" title="删除这一章" data-act="secDelete" data-sid="${esc(s.id)}">×</span>
      </span>
    </button>`;
  }).join('');
}

function chapterEditorHtml(list, s, idx) {
  if (!s) return '<div class="empty">左边选一章开始</div>';
  const text = s.text || '';
  const words = text.length;
  const ev = (S.project.evidence || []).filter(e => e.section_id === s.id && e.status === 'candidate').length;
  const gen = S.genBusy === s.id || (S.composeJob && S.genTitle === s.title);
  const pro = !!s.protected;
  return `<div class="ed-head">
    <div class="ed-title-wrap">
      <div class="ed-kicker">第 ${idx + 1} / ${list.length} 章 ${pro ? '<span class="chip gray">保护章节 · AI 不改</span>' : ''}</div>
      <div class="ed-title">${esc(s.title)}</div>
      <div class="ed-meta"><span id="edWords">${words.toLocaleString()}</span> 字 · 输入来源 ${ev} 条 · <span id="edState">已保存</span></div>
    </div>
    <div class="ed-nav">
      <button class="btn mini" data-act="secStep" data-dir="-1" ${idx === 0 ? 'disabled' : ''}>上一章</button>
      <button class="btn mini" data-act="secStep" data-dir="1" ${idx === list.length - 1 ? 'disabled' : ''}>下一章</button>
    </div>
  </div>
  <div class="ed-tools">
    <button class="btn primary" data-act="genSec" data-sid="${esc(s.id)}" ${gen || pro ? 'disabled' : ''}>${gen ? '生成中…' : (text ? 'AI 重写本章' : 'AI 生成本章')}</button>
    <button class="btn" data-act="rewriteSec" data-sid="${esc(s.id)}" ${gen || pro ? 'disabled' : ''}>按资料改写</button>
    <input type="text" id="secHint" data-role="secHint" data-sid="${esc(s.id)}" placeholder="改写要求（可留空）：补两条同类业绩 / 写清响应时限…" value="${esc(S.secHint || '')}" />
    <span class="grow"></span>
    <button class="btn mini" data-act="showEvidence" data-sid="${esc(s.id)}">来源与历史 ${ev} 条</button>
  </div>
  ${pro ? '<div class="ed-note">这是保护章节（报价 / 投标函 / 承诺 / 资质），AI 不会自动改写，请手工填写。</div>' : ''}
  <div class="ed-conflict" id="edConflict" ${S.secConflict && S.secConflict.sid === s.id ? '' : 'hidden'}>
    <div class="ec-txt"><b>这一章在别处被改过</b>，你的版本没有覆盖服务端的内容（可能是另一个窗口，或刚才的 AI 生成）。</div>
    <div class="ec-acts">
      <button class="btn mini primary" data-act="conflictMine">用我的版本覆盖</button>
      <button class="btn mini" data-act="conflictTheirs">换成服务端版本</button>
    </div>
  </div>
  ${S.secCandidate && S.secCandidate.sid === s.id ? `<div class="ed-candidate" id="edCandidate">
    <div class="ec-txt"><b>这次 AI 稿没有写入</b>：${esc(S.secCandidate.reason || '生成期间这一章被改过')}。先看稿再决定，当前正文保持原样。</div>
    <pre class="ec-pre">${esc((S.secCandidate.text || '').slice(0, 4000))}</pre>
    <div class="ec-acts">
      <button class="btn mini primary" data-act="candidateApply">用这份 AI 稿覆盖</button>
      <button class="btn mini" data-act="candidateDiscard">丢弃 AI 稿</button>
    </div>
  </div>` : ''}
  <textarea class="ed-text" data-role="secText" data-sid="${esc(s.id)}" placeholder="点「AI 生成本章」起草，也可以直接手写。支持 ### 小标题、- 列表、| 表格 |。">${esc(text)}</textarea>
  <div class="ed-foot">
    <span class="ed-count"><b id="edWords2">${words.toLocaleString()}</b> 字</span>
    <span class="hint">改完自动保存；导出前会统一检查</span>
    <span class="grow"></span>
    <button class="btn" data-act="saveSec" data-sid="${esc(s.id)}">保存</button>
  </div>`;
}

/* ---------------- R06：产出文件（版本 / 用途 / 可追溯） ---------------- */
function artifactPanelHtml() {
  return `<div class="card" id="artifactPanel">
    <h2>产出文件 <span class="hint">正式交付 / 知情草稿 / 审阅件分开；只有正式交付件会标成当前定稿</span></h2>
    <div id="artifactList"><div class="hint">加载中…</div></div>
  </div>`;
}
async function loadArtifacts() {
  const box = $('#artifactList');
  if (!box || !S.pid) return;
  try {
    const r = await api('/api/projects/' + S.pid + '/artifacts');
    const items = r.items || [];
    if (!items.length) { box.innerHTML = '<div class="empty">还没有产出文件。导出 Word / Markdown 后，这里会留下可回下载的版本记录。</div>'; return; }
    box.innerHTML = `<div class="list">${items.map(a => {
      const deliverable = a.kind === 'deliverable';
      const ackDraft = a.kind === 'acknowledged_draft';
      const chip = a.status === 'missing' ? '<span class="chip gray">文件已清理</span>'
        : (deliverable && a.is_current ? '<span class="chip ok">当前定稿</span>'
          : ackDraft ? '<span class="chip low">知情草稿</span>'
            : a.is_current ? '<span class="chip low">最新审阅件</span>'
              : '<span class="chip low">历史版本</span>');
      const purpose = deliverable ? '正式交付件'
        : ackDraft ? '知情导出草稿 · 已看过提醒仍下载，不可直接提交投标'
          : '审阅件 · 未经门禁、未脱敏，不能作为投标文件';
      return `<div class="list-item">
        <div class="row gap-sm"><span class="li-title">${esc(a.name)}</span>${chip}
          <span class="faint mono row-end">v${a.version || 1}</span></div>
        <div class="li-sub">${esc(purpose)} · ${esc(a.purpose || '')} · ${esc((a.created_at || '').slice(0, 16))}</div>
        ${a.snapshot_fingerprint ? `<div class="faint" style="font-size:12px">快照 ${esc(String(a.snapshot_fingerprint).slice(0, 16))} · 内容摘要 ${esc(String(a.content_hash || '').slice(0, 16))}</div>` : ''}
        ${a.status === 'missing' ? '' : `<div class="row" style="margin-top:6px"><button class="btn mini" data-act="downloadArtifact" data-aid="${esc(a.id)}">${deliverable && !a.is_current ? '下载历史版本' : '下载'}</button></div>`}
      </div>`;
    }).join('')}</div>`;
  } catch (e) { box.innerHTML = `<div class="hint">产出记录加载失败：${esc(e.message)}</div>`; }
}

/* ---------------- C02：项目负责人 / 角色 / 章节分工与复核 ---------------- */
function teamPanelHtml() {
  return `<div class="card" id="teamPanel">
    <h2>项目团队与分工 <span class="hint">负责人、截止时间、定稿版本、章节分工与复核状态</span></h2>
    <div id="teamBox"><div class="hint">加载中…</div></div>
  </div>`;
}

function teamRoleChip(role) {
  const labels = { lead: '负责人', editor: '编辑', reviewer: '复核', reader: '只读' };
  return `<span class="chip ${role === 'lead' ? 'ok' : role === 'reader' ? 'gray' : 'blue'}">${esc(labels[role] || role)}</span>`;
}

function renderTeam(r) {
  const box = $('#teamBox'); if (!box) return;
  const canManage = S.isOwner || r.my_role === 'lead';
  const userOpts = (r.users || []).map(u =>
    `<option value="${esc(u.id)}">${esc(u.name || u.username || u.id)}</option>`).join('');
  const members = (r.members || []).map(m => {
    const u = (r.users || []).find(x => x.id === m.user_id) || {};
    return `<div class="list-item"><div class="row gap-sm">${teamRoleChip(m.role)}
      <span class="li-title">${esc(u.name || u.username || m.user_id)}</span>
      <span class="faint mono row-end">${esc(m.user_id)}</span>
      ${canManage ? `<button class="btn mini" data-act="teamRemove" data-uid="${esc(m.user_id)}">移除</button>` : ''}
    </div></div>`;
  }).join('');
  const secs = (r.sections || []).map(s => `<div class="list-item" data-sid="${esc(s.id)}">
    <div class="row gap-sm"><span class="li-title">${esc(s.title)}</span></div>
    <div class="row gap-sm" style="margin-top:6px;flex-wrap:wrap">
      <label class="hint">负责人 <select data-role="teamAssignee" ${canManage ? '' : 'disabled'}>
        <option value="">未分配</option>
        ${(r.users || []).map(u => `<option value="${esc(u.id)}" ${u.id === s.assignee ? 'selected' : ''}>${esc(u.name || u.username || u.id)}</option>`).join('')}
      </select></label>
      <label class="hint">复核 <select data-role="teamReview" ${canManage ? '' : 'disabled'}>
        <option value="pending" ${s.review_status === 'pending' ? 'selected' : ''}>待复核</option>
        <option value="approved" ${s.review_status === 'approved' ? 'selected' : ''}>已通过</option>
        <option value="rejected" ${s.review_status === 'rejected' ? 'selected' : ''}>需返工</option>
      </select></label>
    </div></div>`).join('');
  box.innerHTML = `
    <div class="row gap-sm" style="flex-wrap:wrap;margin-bottom:8px">
      <span class="chip ${r.legacy_open ? 'blue' : 'ok'}">${r.legacy_open ? '旧项目：同企业默认可编辑' : '按成员名册授权'}</span>
      <span class="hint">我的角色：${esc(r.role_label || r.my_role)} · ${esc(r.note || '')}</span>
    </div>
    <div class="card"><h3>项目信息</h3>
      <div class="row gap-sm" style="flex-wrap:wrap;align-items:center">
        <label class="hint">负责人 <select id="teamOwner" ${canManage ? '' : 'disabled'}><option value="">未指定</option>
          ${(r.users || []).map(u => `<option value="${esc(u.id)}" ${u.id === r.meta.owner_id ? 'selected' : ''}>${esc(u.name || u.username || u.id)}</option>`).join('')}
        </select></label>
        <label class="hint">截止 <input type="date" id="teamDue" value="${esc(r.meta.due_at || '')}" ${canManage ? '' : 'disabled'} /></label>
        <label class="hint">定稿版本 <input type="text" id="teamFinal" value="${esc(r.meta.final_version || '')}" placeholder="例如 v3" style="width:110px" ${canManage ? '' : 'disabled'} /></label>
        ${canManage ? '<button class="btn mini" data-act="teamMeta">保存项目信息</button>' : ''}
      </div></div>
    <div class="card"><h3>成员与角色</h3>
      ${members ? `<div class="list">${members}</div>` : '<div class="empty">还没有成员名册</div>'}
      ${canManage ? `<div class="row gap-sm" style="margin-top:8px;flex-wrap:wrap">
        <select id="teamNewUser"><option value="">选本企业成员</option>${userOpts}</select>
        <select id="teamNewRole"><option value="editor">编辑</option><option value="reviewer">复核</option><option value="reader">只读</option><option value="lead">负责人</option></select>
        <button class="btn mini" data-act="teamAdd">加入项目</button></div>` : ''}
    </div>
    <div class="card"><h3>章节分工与复核</h3>
      <div id="teamSecs">${secs || '<div class="empty">还没有章节</div>'}</div>
      ${canManage ? '<div class="row" style="margin-top:8px"><button class="btn mini" data-act="teamSections">保存分工与复核状态</button></div>' : ''}
    </div>`;
}

async function loadTeam() {
  const box = $('#teamBox'); if (!box || !S.pid) return;
  try { renderTeam(await api('/api/projects/' + S.pid + '/team')); }
  catch (e) { box.innerHTML = `<div class="hint">团队信息加载失败：${esc(e.message)}</div>`; }
}

function teamSectionItems() {
  return $$('#teamSecs [data-sid]').map(row => ({
    id: row.dataset.sid,
    assignee: (($('[data-role="teamAssignee"]', row) || {}).value || ''),
    review_status: (($('[data-role="teamReview"]', row) || {}).value || 'pending'),
  }));
}

/* ---------------- C01：暗标规则 / 真实分册与投标包 ----------------
   规划只是预览；逐册确认归属后才允许生成文件。导出仍走同一道门禁，
   逐册最终文件检查有一册没过，整个 zip 不发。 */
function bundlePanelHtml() {
  return `<div class="card" id="bundlePanel">
    <h2>真实分册与投标包 <span class="hint">逐册确认归属 → 每册过最终文件检查 → 附件 / 索引 / 响应表 / 清单一起打包</span></h2>
    <div id="bundleBox"><div class="hint">加载中…</div></div>
  </div>`;
}

function bundleVolsHtml() {
  const vols = S.bundleVolumes || [];
  if (!vols.length) return '<div class="empty">还没有章节；先定目录再分册。</div>';
  return vols.map((v, i) => `<div class="card" data-vi="${i}" style="margin:8px 0">
    <div class="row gap-sm"><span class="hint">第 ${i + 1} 册</span>
      <input type="text" class="grow" data-role="bundleVolName" value="${esc(v.name)}" style="min-width:180px" />
      <span class="hint">${v.section_ids.length} 章</span></div>
    <div class="list" style="margin-top:6px">${v.section_ids.map(sid => {
      const s = (S.bundleSections || {})[sid] || { title: sid };
      return `<div class="list-item"><div class="row gap-sm">
        <span class="li-title">${esc(s.title)}</span><span class="grow"></span>
        <select class="bundle-sec" data-role="bundleSec" data-sid="${esc(sid)}" style="width:130px">
          ${vols.map((x, j) => `<option value="${j}" ${i === j ? 'selected' : ''}>第 ${j + 1} 册</option>`).join('')}
        </select></div></div>`;
    }).join('')}</div></div>`).join('');
}

function bundleScanHtml(s) {
  if (!s) return '';
  if (!s.checked) return `<div class="hint" style="margin-top:8px">扫描没跑完：${esc(s.summary || '')}</div>`;
  const rows = (s.issues || []).map(x => `<div class="list-item">
    <div class="row gap-sm"><span class="chip ${x.level === '高' ? 'low' : 'gray'}">${esc(x.level)}</span>
      <span class="li-title">${esc(x.where || '')}</span></div>
    <div class="li-sub">${esc(x.detail || '')}</div></div>`).join('');
  return `<div style="margin-top:8px">
    <div class="row gap-sm"><span class="chip ${s.ok ? 'ok' : 'low'}">${s.ok ? '未发现身份信息命中' : '发现 ' + (s.blocking || []).length + ' 处命中'}</span>
      <span class="hint">扫描部件 ${(s.checked_parts || []).length} 个 · 身份词 ${s.identity_terms || 0} 个</span></div>
    <div class="hint" style="margin:6px 0">${esc(s.claim || '')}</div>
    ${rows ? `<div class="list">${rows}</div>` : '<div class="empty">没有命中项</div>'}</div>`;
}

function renderBundle() {
  const box = $('#bundleBox'); if (!box) return;
  const plan = S.bundlePlan || { volumes: [], total: 0 };
  const rules = S.bundleRules || {};
  const gate = S.bundleGate;
  const gateRows = gate ? (gate.issues || []).filter(x => x.level === '高').map(x =>
    `<label class="hint" style="display:flex;gap:8px;align-items:flex-start;margin:8px 0">
      <input type="checkbox" value="${esc(x.key)}" style="width:auto;margin-top:3px" />
      <span><b>${esc(x.title)}</b><br><span class="desc">${esc(x.detail || '')}</span></span></label>`).join('') : '';
  box.innerHTML = `
    <div class="hint" style="margin-bottom:8px">共 ${plan.total} 章。下面是启发式归属，要逐册核对；确认后才生成文件。${esc(plan.note || '')}</div>
    <div class="card"><h3>① 本项目暗标规则</h3>
      <div class="row" style="flex-wrap:wrap;gap:8px;align-items:center">
        <input id="blFull" type="text" placeholder="企业全称" value="${esc(rules.enterprise_full || '')}" style="min-width:200px" />
        <input id="blShort" type="text" placeholder="企业简称（逗号分隔）" value="${esc((rules.enterprise_short || []).join('、'))}" style="min-width:170px" />
        <input id="blPersons" type="text" placeholder="人员姓名（逗号分隔）" value="${esc((rules.persons || []).join('、'))}" style="min-width:170px" />
        <input id="blContacts" type="text" placeholder="电话 / 邮箱（逗号分隔）" value="${esc((rules.contacts || []).join('、'))}" style="min-width:170px" />
        <input id="blAddr" type="text" placeholder="地址（逗号分隔）" value="${esc((rules.addresses || []).join('、'))}" style="min-width:170px" />
        <button class="btn mini" data-act="bundleSaveRules">保存规则</button>
      </div>
      <div class="hint" style="margin-top:6px">${rules.configured
        ? '<span class="chip ok">已登记项目规则</span> 扫描按这些身份词全包核对'
        : '<span class="chip low">未登记项目规则</span> 只做通用模式扫描，不能宣称完全暗标合规'}。字体字号、页边距、封面、页码、签章、命名与分册格式请对照招标文件人工核对；系统只报告文件里实际有什么，不替你判定格式合规。</div>
    </div>
    <div class="card"><h3>② 扫描当前 Word</h3>
      <div class="row" style="gap:8px;align-items:center"><button class="btn mini" data-act="bundleScan">按当前正文临时渲染并全包扫描</button>
        <span class="hint">不落盘、不登记产物；正式提交前仍须对最终文件再扫一遍</span></div>
      ${bundleScanHtml(S.bundleScan)}</div>
    <div class="card"><h3>③ 逐册确认归属</h3><div id="bundleVols">${bundleVolsHtml()}</div>
      <div class="hint">每册单独生成 Word；移动章节只改归属，不会改正文。</div></div>
    ${gate ? `<div class="card"><h3>④ 导出门禁逐条确认</h3>
      <div class="desc">有 ${gate.high} 条高风险问题，勾选确认后才能生成投标包。章节硬伤不能靠勾选放行。</div>
      <div id="bundleGateChecks">${gateRows}</div>
      <div class="row" style="margin-top:8px"><button class="btn primary" data-act="bundleGateConfirm">确认后生成投标包</button></div></div>` : ''}
    <div class="card"><h3>生成投标包</h3>
      <label class="hint" style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="bundleConfirmed" ${S.bundleConfirmed ? 'checked' : ''} style="width:auto" /> 我已逐册核对章节归属与附件，确认按上面分册生成</label>
      <label class="hint" style="display:flex;gap:8px;align-items:center;margin-top:6px"><input type="checkbox" id="bundleBlind" ${S.bundleBlind ? 'checked' : ''} style="width:auto" /> 按暗标导出（去标识 + 逐册全包扫描）</label>
      <div class="row" style="margin-top:8px"><button class="btn primary" data-act="bundleExport">生成真实投标包（zip）</button></div>
      <div class="stop" style="margin-top:8px">包里有逐册 Word、登记过的原件附件、分册索引、逐条响应表、文件包清单。目录域要在 Word 打开时更新；PDF 转换和平台专用格式未实现；图片 / 嵌入对象里的文字、盖章、水印须人工逐项核对。</div>
    </div>`;
}

async function loadBundle() {
  const box = $('#bundleBox'); if (!box || !S.pid) return;
  if (S.bundleVolumesPid !== S.pid) {
    S.bundleGate = null; S.bundleScan = null; S.bundleConfirmed = false;
    S.bundleVolumes = null;
  }
  try {
    const [plan, rules] = await Promise.all([
      api('/api/projects/' + S.pid + '/bundle/plan'),
      api('/api/projects/' + S.pid + '/bundle/rules'),
    ]);
    S.bundlePlan = plan; S.bundleRules = rules;
    const secs = {};
    (plan.volumes || []).forEach(v => (v.sections || []).forEach(s => { secs[s.id] = s; }));
    S.bundleSections = secs;
    if (!S.bundleVolumes || S.bundleVolumesPid !== S.pid) {
      S.bundleVolumes = (plan.volumes || []).map(v => ({
        name: v.name, section_ids: (v.sections || []).map(s => s.id) }));
      S.bundleVolumesPid = S.pid;
    }
    renderBundle();
  } catch (e) { box.innerHTML = `<div class="hint">分册规划加载失败：${esc(e.message)}</div>`; }
}

function bundleCollect() {
  return $$('#bundleVols [data-vi]').map(card => ({
    name: (($('[data-role="bundleVolName"]', card) || {}).value || '').trim(),
    section_ids: $$('[data-role="bundleSec"]', card).map(s => s.dataset.sid),
  })).filter(v => v.name);
}

function onBundleChange(e) {
  const t = e.target;
  if (t.id === 'bundleBlind') { S.bundleBlind = t.checked; return; }
  if (t.id === 'bundleConfirmed') { S.bundleConfirmed = t.checked; return; }
  if (!t.classList || !t.classList.contains('bundle-sec')) return;
  const to = Number(t.value), sid = t.dataset.sid;
  const vols = bundleCollect();
  vols.forEach(v => { v.section_ids = v.section_ids.filter(x => x !== sid); });
  if (vols[to]) vols[to].section_ids.push(sid);
  S.bundleVolumes = vols;
  const box = $('#bundleVols'); if (box) box.innerHTML = bundleVolsHtml();
}

async function bundleDownload(token) {
  const vols = S.bundleVolumes || [];
  const r = await fetch('/api/projects/' + S.pid + '/bundle/export', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, volumes: vols, confirmed: true, blind: !!S.bundleBlind }),
  });
  if (!r.ok) {
    let j = {}; try { j = await r.json(); } catch (_) { /* 忽略 */ }
    const extra = (j.checks || []).map(c => c.volume + '：' + c.summary).slice(0, 3).join('；');
    throw new Error((typeof j.detail === 'string' ? j.detail : j.error || '生成失败 ' + r.status)
      + (extra ? '；' + extra : ''));
  }
  const b = await r.blob();
  const cd = r.headers.get('content-disposition') || '';
  const name = cd.includes("filename*=UTF-8''")
    ? decodeURIComponent(cd.split("filename*=UTF-8''")[1]) : '投标文件.zip';
  const a = document.createElement('a');
  a.href = URL.createObjectURL(b); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  toast('投标包已开始下载（' + (r.headers.get('X-Bundle-Volumes') || vols.length) + ' 册）');
  loadArtifacts();
}

/* 当前版本：正文页只做「定目录 → 写正文」这一件事。
   产出文件 / 投标包与暗标 / 长任务控制这些面板先完全不展示：功能本身对
   第一次用的人太杂，摆在正文页里只会让人迷路。后端接口、权限、回归断言
   全部保留；流程跑顺后把开关改成 true（或做成独立导出页）即可放回，不用重写。 */
const SHOW_ADVANCED = false;

function advancedHtml(withTasks = false) {
  if (!SHOW_ADVANCED) return '';
  return `<details class="card adv-fold">
    <summary>交付与高级设置 <span class="hint">产出文件 / 投标包与暗标 / 长任务控制 · 跑通正文流程不用展开</span></summary>
    <div class="adv-body">${withTasks ? taskCtlPanelHtml() : ''}${artifactPanelHtml()}${bundlePanelHtml()}</div>
  </details>`;
}

/* 目录体检：后端 expand() 已经算出未匹配评分项、篇幅预算与格式命中，
   过去前端只拿 chapters.length 弹了个 toast，其余全丢。确认目录前先看一眼，
   免得 12 个评分项漏了还被一路放行。 */
/* 整本生成的常驻结论：进度条只在任务跑着的时候有，结束后必须还能看见
   「写了几章、还剩几章、哪几章没达标」，别让用户去翻聊天记录猜结果。 */
/* 真实模型要花钱：实测一次 29 章约 8.7 万 token、日限额 20 万，连跑两本就超额。
   事前给估算区间与剩余额度，只做知情，不阻断、不新增页签、演示模式不打扰。 */
function composeCostNote(chapters) {
  const b = (S.budget && S.budget.budget) || null;
  const limit = Number((b && b.daily_token_limit) || 200000);
  const used = Number((S.usage || {}).today_tokens || 0);
  const n = Math.max(1, Number(chapters) || 1);
  const mid = Math.round(n * 3000 / 1000) * 1000;
  const lo = Math.round(mid * 0.8 / 1000) * 1000;
  const hi = Math.round(mid * 1.3 / 1000) * 1000;
  const left = Math.max(0, limit - used);
  const pct = left ? Math.round(mid / left * 100) : 100;
  const quota = b
    ? `今日已用 ${used.toLocaleString()} / 上限 ${limit.toLocaleString()}，剩余 ${left.toLocaleString()} token`
      + (b.enabled === false ? '（护栏未启用）' : '') + `；本次约占剩余额度的 ${pct}%`
    : '今日额度暂时读不到，生成后请到「用量」核对';
  return `本次约写 ${n} 章，预计 ${lo.toLocaleString()}–${hi.toLocaleString()} token。\n`
    + quota + '。\n按章数估算，实际用量以审计记录为准。';
}

function composeResultHtml(list) {
  const r = S.composeResult;
  if (!r || S.composeJob) return '';
  const written = list.filter(s => (s.text || '').trim()).length;
  const emptyOpen = list.filter(s => !(s.text || '').trim() && !s.protected);
  const emptyProt = list.filter(s => !(s.text || '').trim() && s.protected);
  const tone = r.status === 'done' ? 'ok' : r.status === 'partial' ? 'mid' : 'high';
  const label = r.cancelled ? '已取消' : r.status === 'done' ? '已完成'
    : r.status === 'partial' ? '部分完成' : '未跑完';
  return `<div class="card" id="composeResultCard">
    <h2>上次整本生成 <span class="chip ${tone}">${label}</span></h2>
    <div class="row gap-sm">
      <span class="chip blue">写好 ${written}/${list.length} 章</span>
      <span class="chip ${emptyOpen.length ? 'mid' : 'ok'}">待写 ${emptyOpen.length} 章</span>
      <span class="chip ${emptyProt.length ? 'mid' : 'ok'}">保护章待手填 ${emptyProt.length} 章</span>
      ${r.undeliverable_total ? `<span class="chip high">整本未达标 ${r.undeliverable_total} 章</span>` : ''}
      ${r.task_tokens ? `<span class="hint">本次约 ${r.task_tokens.toLocaleString()} token${S.mode === 'demo' ? '（演示估算，未产生费用）' : ''}</span>` : ''}
    </div>
    ${r.manual_followup ? `<p class="desc" style="margin-top:10px">${esc(r.manual_followup)}</p>` : ''}
    ${r.note ? `<p class="desc" style="margin-top:8px">${esc(r.note)}</p>` : ''}
    <div class="row" style="margin-top:8px">
      <button class="btn mini" data-act="toggleOnlyEmpty">${S.onlyEmpty ? '显示全部章节' : `只看待写章（${emptyOpen.length}）`}</button>
      <button class="btn mini" data-act="gotoNextEmpty">定位下一章</button>
      <span class="grow"></span>
      <button class="btn mini" data-act="dismissComposeResult">收起</button>
    </div>
  </div>`;
}

function outlinePlanHtml() {
  const plan = S.outlinePlan || {};
  const list = secList();
  const unmatched = (plan.unmatched_scoring || [])
    .filter(x => typeof x === 'string' && x.trim());
  const odd = list.filter(s => /[X×＊*]{2,}|待定|占位/.test(s.title || ''));
  const wrote = list.filter(s => String(s.text || '').trim()).length;
  const hasPlan = plan.scoring_items || plan.total_words || plan.chapters;
  if (!hasPlan && !unmatched.length && !odd.length) return '';
  const words = Number(plan.total_words || 0);
  const pages = Number(plan.estimated_pages || 0);
  const scoring = Number(plan.scoring_items || 0);
  // 老项目 / 沿用旧目录时没有评分办法留痕，写「0 项」会让人误以为招标没有评分办法。
  const stat = scoring
    ? `评分办法 ${scoring} 项 · 按招标格式展开 ${Number(plan.used_format || 0)} 章`
    : '本次目录没有评分办法留痕（沿用旧目录或老项目）';
  return `<div class="card" id="outlinePlanCard">
    <h2>目录体检 <span class="hint">${wrote ? `已写 ${wrote} 章，按需核对` : '确认目录前先看一眼'}</span></h2>
    <div class="row gap-sm">
      <span class="chip blue">当前 ${list.length} 章</span>
      <span class="hint">${stat}${words ? ` · 篇幅预算 ${words.toLocaleString()} 字 / 约 ${pages.toFixed(1)} 页` : ''}</span>
    </div>
    ${unmatched.length ? `<div class="hint" style="margin-top:10px;color:#b45309">
      有 ${unmatched.length} 个评分项没对上任何章节标题：可能漏了得分点，也可能已并入相关章节，请逐条核对。</div>
      <details class="fold"><summary>逐条核对未匹配评分项（${unmatched.length}）</summary>
        ${unmatched.slice(0, 40).map(n => `<div class="q-defect">
          <span class="q-title">${esc(n)}</span><span class="grow"></span>
          <button class="btn mini" data-act="addScoringChapter" data-title="${esc(n)}">＋ 补一章</button>
        </div>`).join('')}
      </details>` : ''}
    ${odd.length ? `<div class="hint" style="margin-top:10px;color:#b45309">
      有 ${odd.length} 个章节标题带着未替换的占位字，建议改名。</div>
      <div>${odd.slice(0, 8).map(s => `<div class="q-defect">
        <span class="q-title">${esc(s.title)}</span><span class="grow"></span>
        <button class="btn mini" data-act="secRename" data-sid="${esc(s.id)}">改名</button>
      </div>`).join('')}</div>` : ''}
  </div>`;
}

function renderWrite() {
  const list = secList();
  if (!list.length) {
    renderInto('tab-write', `${writeEmptyHtml()}${S.outlineMem ? outlineMemHtml() : ''}${S.bidImport ? bidImportHtml() : ''}${advancedHtml(false)}`);
    if (SHOW_ADVANCED) { loadArtifacts(); loadBundle(); }
    return;
  }
  const cur = secCurrent();
  S.secId = cur ? cur.id : '';
  const idx = cur ? list.indexOf(cur) : 0;
  const shownList = S.onlyEmpty
    ? list.filter(s => !(s.text || '').trim() && !s.protected) : list;
  // 老项目从来没点过「确认目录」但正文已经写完，不能再拿「才能写正文」误导用户。
  const wrote = list.filter(s => String(s.text || '').trim()).length;
  renderInto('tab-write', `
    ${writeTopHtml(list)}
    ${composeResultHtml(list)}
    ${outlinePlanHtml()}
    ${S.outlineMem ? outlineMemHtml() : ''}
    ${S.bidImport ? bidImportHtml() : ''}
    <div class="write-split">
      <aside class="chap-pane">
        <div class="cp-head"><span>目录</span><span class="cp-head-right"><span id="cpProg">${list.filter(s => (s.text || '').trim()).length}/${list.length}</span>
          ${String(S.project.outline_confirmed_at || '').trim()
            ? '<span class="cp-confirmed" title="目录已确认；改动章节后需重新确认">已确认</span>'
            : '<button class="cp-confirm" data-act="outlineConfirm" title="确认目录便于核对完整性">确认目录</button>'}
          <button class="cp-add" data-act="secAdd" title="手动加一章">＋ 加章</button></span></div>
        ${String(S.project.outline_confirmed_at || '').trim() ? ''
          : (wrote
            ? `<div class="cp-hint">已有 ${wrote} 章正文，目录未确认不影响继续编辑；确认后便于按目录核对完整性。</div>`
            : '<div class="cp-hint">目录还没确认：核对章节名称与顺序后点「确认目录」，才能写正文。</div>')}
        <div class="cp-list">${chapListHtml(shownList, list)}</div>
      </aside>
      <section class="edit-pane">${chapterEditorHtml(list, cur, idx)}</section>
    </div>${advancedHtml(true)}`);
  if (SHOW_ADVANCED) { loadArtifacts(); loadBundle(); }
}

/* ---------------- 章节自动保存 / 整本生成进度 ---------------- */
function setEdState(txt) {
  const el = $('#edState');
  if (el) el.textContent = txt;
}

function secEditorNow() {
  const ta = $('.ed-text');
  if (!ta) return null;
  return { ta, sid: ta.dataset.sid || '', text: ta.value };
}

function secVer(sid) {
  const s = secList().find(x => x.id === sid);
  return Number((s && s.ver) || 0);
}

function showConflictBar(show) {
  const box = $('#edConflict');
  if (box) box.hidden = !show;
}

function updateWriteMeta() {
  const list = secList();
  if (!list.length) return;
  const cur = list.find(s => s.id === S.secId);
  const words = cur ? (cur.text || '').length : 0;
  const w1 = $('#edWords'), w2 = $('#edWords2');
  if (w1) w1.textContent = words.toLocaleString();
  if (w2) w2.textContent = words.toLocaleString();
  const written = list.filter(s => (s.text || '').trim()).length;
  const c = $('#wtCount'), w = $('#wtWords'), bar = $('#wtBar'), prog = $('#cpProg');
  if (c) c.textContent = `${written}/${list.length} 章已写`;
  if (w) w.textContent = `${secWordTotal(list).toLocaleString()} 字`;
  if (bar) bar.style.width = `${Math.round(written / list.length * 100)}%`;
  if (prog) prog.textContent = `${written}/${list.length}`;
  const item = cur ? document.getElementById('cp-' + cur.id) : null;
  if (item) {
    const sub = item.querySelector('.cp-sub');
    const dot = item.querySelector('.cp-dot');
    if (sub) sub.textContent = `${words.toLocaleString()} 字${cur.protected ? ' · 保护' : ''}`;
    item.classList.toggle('done', words > 0);
    if (dot) dot.classList.toggle('done', words > 0);
  }
}

async function saveSecNow(force = false) {
  if (!S.pid) return false;
  const cur = secEditorNow();
  if (!cur) return !S.secDirty;          // 编辑器不在页面上，没有可提交的内容
  const { sid, text } = cur;
  if (S.secTimer) { clearTimeout(S.secTimer); S.secTimer = null; }
  // 同一章的保存串行执行：两次 PUT 并发时，先发的旧稿可能后到，把新稿盖回去
  const mine = (S.saveChain || Promise.resolve()).then(async () => {
    setEdState('保存中…');
    try {
      // force 只在「用户明确点过用我的版本覆盖 / 应用 AI 稿」时出现，
      // 服务端要求显式 allow_overwrite，避免不带版本号变成静默无条件写
      const body = force ? { text, allow_overwrite: true } : { text, base_ver: secVer(sid) };
      const r = await api(`/api/projects/${S.pid}/section/${sid}`, 'PUT', body);
      const sec = secList().find(x => x.id === sid);
      if (sec) { sec.text = text; sec.ver = Number(r.ver || secVer(sid) + 1); }
      // 只有编辑框里仍是这次提交的文字才算「干净」；
      // 等待接口返回期间用户又输入了，就保持未保存，绝不能标成已保存
      const live = secEditorNow();
      const same = !!live && live.sid === sid && live.text === text;
      S.secDirty = !same;
      if (same) { S.secConflict = null; showConflictBar(false); }
      const d = new Date();
      const hh = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
      setEdState(same ? `已保存 ${hh}` : '已保存，还有新改动…');
      updateWriteMeta();
      // 返回值 = 「这一章现在没有未保存内容」，不是「这次请求成功了」。
      // 等待接口返回期间用户又敲了字，就等于还有新稿没落地：切章 / 下载 / 切项目都不能放行。
      return same;
    } catch (e) {
      S.secDirty = true;
      if (e.status === 409 && e.body && e.body.conflict) {
        // 服务端版本更新：不覆盖、不丢本地文字，让用户自己选
        S.secConflict = { sid, serverText: String(e.body.text || ''), ver: Number(e.body.ver || 0) };
        setEdState('有冲突：这一章在别处被改过');
        showConflictBar(true);
      } else {
        setEdState('保存失败，再点一次保存');
      }
      toast(e.message);
      return false;
    }
  });
  S.saveChain = mine.catch(() => false);
  return await mine;
}

async function secFlush() {
  if (S.secTimer) { clearTimeout(S.secTimer); S.secTimer = null; }
  if (S.secConflict) return false;       // 冲突没处理完，不许切章 / 切项目 / 下载
  if (!S.secDirty) return true;
  if (!secEditorNow()) return false;
  return await saveSecNow();
}

function pollCompose(jid) {
  let lastStep = '';
  const tick = async () => {
    let j;
    try { j = await api('/api/jobs/' + jid); }
    catch (e) {
      S.composeJob = null; S.genTitle = '';
      S.composeResult = { status: 'unknown', step: '', generated: 0,
        undeliverable_total: 0, failed: 0, task_tokens: 0, cancelled: false,
        manual_followup: '', note: '连接中断，任务可能仍在后台运行；刷新页面可接回进度。' };
      renderWrite(); toast('连不上服务，任务可能还在跑：' + e.message); return;
    }
    if (j.status === 'queued' || j.status === 'running') {
      S.composeJob = { jid, progress: j.progress || 0,
                       step: j.status === 'queued' ? '排队中，前面还有任务…' : (j.step || '正在生成…') };
      const m = /正在生成\s*\d+\/\d+[：:](.+)$/.exec(j.step || '');
      S.genTitle = m ? m[1].trim() : '';
      // 章节切换或用户正在改正文时不重绘，避免打断输入
      const editing = document.activeElement && document.activeElement.classList.contains('ed-text');
      if (j.step !== lastStep && !editing && S.activeTab === 'write') {
        lastStep = j.step || '';
        try {
          S.project = await api('/api/projects/' + S.pid);
          S.skipDraft = true;
          renderWrite();
        } catch (_) { /* 下个心跳再试 */ }
      } else {
        const stepEl = $('#wtJobStep'), pctEl = $('#wtJobPct'), barEl = $('#wtBar');
        if (stepEl) stepEl.textContent = S.composeJob.step;
        if (pctEl) pctEl.textContent = `${S.composeJob.progress}%`;
        if (barEl) barEl.style.width = `${S.composeJob.progress}%`;
        const cp = S.genTitle ? document.getElementById('cp-' + ((secList().find(s => s.title === S.genTitle) || {}).id || '')) : null;
        if (cp) cp.classList.add('gen');
      }
      setTimeout(tick, 700);
      return;
    }
    S.composeJob = null; S.genTitle = ''; S.genBusy = '';
    // 过去任务一结束整张进度卡就没了，partial / 失败只留一个会自动消失的 toast。
    // 这里把结论留在页面上，直到下一次整本生成才替换。
    const res = j.result || {};
    S.composeResult = {
      status: j.status || 'partial', step: j.step || '',
      generated: Number(res.generated || 0),
      undeliverable_total: Number(res.undeliverable_total || 0),
      failed: (res.failed || []).length,
      task_tokens: Number(res.task_tokens || 0),
      manual_followup: String(res.manual_followup || ''),
      cancelled: !!res.cancelled
    };
    S.lastJobId = jid;
    try {
      const r = await api('/api/chat/job/' + jid);
      if (r && r.reply) CHAT.messages.push({ role: 'agent', reply: r.reply, cards: r.cards || [], sources: r.sources || [] });
      CHAT.resumed.add(jid);
    } catch (_) { /* 结果已写库，刷新即可看到 */ }
    S.skipDraft = true;
    await refresh();
    const first = secList().find(s => (s.text || '').trim()) || secList()[0];
    S.secId = first ? first.id : '';
    S.skipDraft = true;
    renderWrite();
    const d = (j.result || {}).delivery || {};
    toast(j.status === 'done'
      ? `整本生成完成：${(j.result || {}).generated || 0} 章已写；${d.label || '正文已填写'}，仍需质量/要求/附件复核，不等于可交付`
      : j.status === 'partial'
        ? `部分完成：${j.step || ''}（没写成的章节可单独重试）`
        : `生成失败：${j.error || j.step || '未知错误'}`);
  };
  tick();
}

function pollOptimize(jid) {
  let lastStep = '';
  const tick = async () => {
    let j;
    try { j = await api('/api/jobs/' + jid); }
    catch (e) { S.optJob = null; renderCheck(); toast('连不上服务，任务可能还在跑：' + e.message); return; }
    if (j.status === 'queued' || j.status === 'running') {
      S.optJob = { jid, progress: j.progress || 0,
                   step: j.status === 'queued' ? '排队中，前面还有任务…' : (j.step || '正在改写…') };
      const st = $('#optStep'), pc = $('#optPct');
      if (st) st.textContent = S.optJob.step;
      if (pc) pc.textContent = `${S.optJob.progress}%`;
      if (j.step !== lastStep && S.activeTab === 'check') { lastStep = j.step || ''; renderCheck(); }
      setTimeout(tick, 700);
      return;
    }
    S.optJob = null;
    S.optResult = j.result || null;
    try {
      const r = await api('/api/chat/job/' + jid);
      if (r && r.reply) CHAT.messages.push({ role: 'agent', reply: r.reply, cards: r.cards || [], sources: r.sources || [] });
      CHAT.resumed.add(jid);
    } catch (_) { /* 结果已写库 */ }
    await refresh();
    const o = S.optResult || {};
    toast(j.status === 'done'
      ? `改好并复查完成：问题 ${(o.before || []).length} → ${(o.after || []).length}`
      : j.status === 'partial'
        ? `部分完成：问题 ${(o.before || []).length} → ${(o.after || []).length}，${o.error_count || 0} 条改写未落地`
        : `改写失败：${j.error || j.step || '未知错误'}`);
  };
  tick();
}

/* ---------------- 验标书 ---------------- */function issueHtml(it) {
  const cls = it.level === '高' ? '' : it.level === '中' ? 'mid' : 'low';
  const st = it.status || 'pending';
  const conf = Number(it.confidence ?? 0.9);
  const id = it.id || '';
  return `<div class="issue ${cls}">
    <div class="it">${levelChip(it.level)} <span>${esc(it.title)}</span> <span class="chip gray">${esc(it.source || '规则')}</span> <span class="chip blue">${esc(it.code || '')}</span> ${st === 'accepted' ? '<span class="chip ok">已采纳</span>' : st === 'ignored' ? '<span class="chip gray">已忽略</span>' : ''}</div>
    <div class="id">${esc(it.detail || '')}</div>
    ${it.anchor ? `<div class="ev">命中原文：${esc(it.anchor.slice(0, 160))}</div>` : ''}
    <div class="loc">定位：${esc(it.section_title || it.section_id || '整本（未定位到具体段落）')} ${it.anchor ? '· 可只改该段落' : '· 仅提示'}${it.page ? ` · 第 ${it.page} 页` : ''} · 置信度 ${Math.round(conf * 100)}%${conf < 0.60 ? '（低，不自动改写）' : ''}</div>
    ${it.source_ref ? `<div class="loc">依据：${esc(it.source_ref)}</div>` : ''}
    ${id ? `<div class="row" style="margin-top:7px">
      <button class="btn mini ${st === 'accepted' ? 'primary' : ''}" data-act="checkStatus" data-id="${esc(id)}" data-status="accepted">采纳</button>
      <button class="btn mini" data-act="checkStatus" data-id="${esc(id)}" data-status="ignored">忽略（不自动改）</button>
      <button class="btn mini" data-act="checkStatus" data-id="${esc(id)}" data-status="pending">重置</button>
      ${it.section_id ? `<button class="btn mini" data-act="gotoSec" data-sid="${esc(it.section_id)}">去这一章改</button>` : ''}
      ${it.page ? `<button class="btn mini" data-act="locate" data-page="${it.page}" data-q="${esc((it.anchor || '').slice(0, 20))}">回跳原文 · 第 ${it.page} 页</button>` : ''}
    </div>` : ''}
  </div>`;
}

function renderCheck() {
  const p = S.project;
  const all = p.checks || [];
  const last = all.length ? Math.max(...all.map(c => c.round)) : 0;
  const issues = all.filter(c => c.round === last)
    .map(c => ({ ...c, section_title: (p.sections.find(s => s.id === c.section_id) || {}).title }));
  const opt = S.optResult;
  const optAfter = opt ? opt.after.map(i => ({ ...i, section_title: (p.sections.find(s => s.id === i.section_id) || {}).title })) : [];
  const f = S.issueFilter || 'all';
  const counts = {
    all: issues.length,
    open: issues.filter(i => (i.status || 'pending') === 'pending').length,
    high: issues.filter(i => i.level === '高' && i.status !== 'ignored').length,
    accepted: issues.filter(i => i.status === 'accepted').length,
  };
  const shown = issues.filter(i => {
    if (f === 'open') return (i.status || 'pending') === 'pending';
    if (f === 'high') return i.level === '高' && i.status !== 'ignored';
    if (f === 'accepted') return i.status === 'accepted';
    return true;
  });
  const job = S.optJob;
  const tab = (k, label) =>
    `<button class="kb-tab ${f === k ? 'on' : ''}" data-act="issueFilter" data-k="${k}">${label}<b>${counts[k] || 0}</b></button>`;
  const fixed = opt ? (opt.fixes || []).filter(x => x.status === 'fixed') : [];
  renderInto('tab-check', `
    <div class="card">
      <h2>检查与修改 <span class="chip ${counts.high ? 'high' : issues.length ? 'mid' : 'ok'}">${last ? `第 ${last} 轮 · ${issues.length} 条问题` : '还没检查'}</span></h2>
      <p class="desc">先查问题，再决定改哪些：<b>只改你点过「采纳」的</b>，其余一律不动；报价表、投标函等保护章节永不自动改写。</p>
      <div class="row">
        <button class="btn primary" data-act="check">${last ? '重新检查' : '开始检查'}</button>
        <button class="btn" data-act="optimize" ${counts.accepted && !job ? '' : 'disabled'}>${job ? '正在改…' : `按采纳的 ${counts.accepted} 条改好并复查`}</button>
        <span class="grow"></span>
        <button class="btn" data-act="goto" data-tab="write">回正文手动改</button>
      </div>
      ${job ? `<div class="wt-job" style="margin-top:10px"><span class="wt-spin"></span><span id="optStep">${esc(job.step || '正在改…')}</span><span class="grow"></span><b id="optPct">${job.progress || 0}%</b></div>` : ''}
      ${!counts.accepted && issues.length ? '<div class="hint" style="margin-top:9px">还没采纳任何问题：在下面的清单里点「采纳」，再回来点「改好并复查」。</div>' : ''}
    </div>

    ${readinessHtml()}

    <div class="card">
      <h2>导出文件 <span class="hint">检查与下载在同一页完成</span></h2>
      <div class="row">
        <button class="btn primary" data-act="exportWord">下载 Word</button>
        <a class="btn mini" href="/vue/check.html?pid=${encodeURIComponent(S.pid)}" target="_blank" rel="noopener">试用新版检查页</a>
        <span class="grow"></span>
        <span class="hint">有提醒时先列清单；确认知情后仍可下载，文件按结果登记为交付件或知情草稿。</span>
      </div>
    </div>

    <details class="fold"><summary>自动检查明细（逐条响应 / 数字出处 / 占位符 / 空章）</summary>${qualityPanelHtml()}</details>

    ${opt ? `<div class="card">
      <h2>本轮改动与复查 <span class="chip ${opt.after.length ? 'mid' : 'ok'}">${opt.before.length} → ${opt.after.length} 条</span></h2>
      <div class="big-compare">
        <div class="bc-item"><div class="num">${opt.before.length}</div><div class="cap">初检问题</div></div>
        <div class="bc-arrow" aria-hidden="true"></div>
        <div class="bc-item"><div class="num neg">${opt.after.length}</div><div class="cap">复检问题</div></div>
        <div class="bc-sep" aria-hidden="true"></div>
        <div class="bc-item"><div class="num">${opt.fixed_count}</div><div class="cap">已改写段落</div></div>
        <div class="bc-item"><div class="num">${opt.rounds}</div><div class="cap">执行轮数</div></div>
      </div>
      <p class="desc">减少 ${opt.reduced} 个问题；保护章节跳过 ${opt.protected_count} 处。${opt.batch_id ? '本轮整本已留快照，可在页面底部一键撤销。' : ''}</p>
      ${fixed.length ? `<details class="fold" open><summary>逐条改动（${fixed.length} 处）</summary>
        ${fixed.map(fx => `<div class="sec-item open"><div class="sec-head"><span class="st">${esc(fx.title)}</span><span class="chip ok">已改写</span><span class="sm">${esc(fx.section)}</span></div>
          <div class="sec-body">
            <div class="diff"><div class="before"><div class="diff-cap">改写前（命中段落）</div>${nl(fx.before)}</div><div class="after"><div class="diff-cap">改写后</div>${nl(fx.after)}</div></div>
            ${(fx.sources || []).length ? `<div class="hint" style="margin-top:8px">依据：${fx.sources.map(h => esc(h.scope + '·' + h.title)).join('、')}</div>` : ''}
          </div></div>`).join('')}
      </details>`
        : `<div class="empty" style="padding:16px 0">本轮没有改动正文：采纳的问题都没定位到具体段落，请到「标书正文」里手工改。</div>`}
      ${opt.after.length ? `<details class="fold"><summary>复检剩余问题（${opt.after.length}）</summary>
        <div style="margin-top:10px">${optAfter.map(issueHtml).join('')}</div>
      </details>` : '<div class="hint" style="margin-top:8px">复检没有剩余问题。</div>'}
    </div>` : ''}

    <div class="card">
      <div class="ck-filter">
        ${tab('all', '全部')}${tab('open', '未处理')}${tab('high', '高风险')}${tab('accepted', '已采纳')}
        <span class="grow"></span><span class="hint">这里是可逐条采纳的检查清单；整本体检结论见上方「能不能交」卡。点问题下面的「去这一章改」可跳到正文</span>
      </div>
      <div id="checkIssueList" data-empty="${issues.length ? 0 : 1}">
      ${shown.map(issueHtml).join('') || `<div class="empty">${issues.length ? '这个筛选下没有问题了' : (S.checkResult && S.checkResult.empty ? esc(S.checkResult.reason || '还没有正文可检查') : '还没有检查结果，点上面的「开始检查」')}</div>`}
      </div>
    </div>

    <details class="fold">
      <summary>版本与回退（${(S.versions || []).length} 个快照）</summary>
      <p class="desc" style="margin-top:10px">一键成稿、单章生成 / 改写、闭环优化都会先存快照；点「撤销本轮」只恢复这一批章节，不影响其他章节。</p>
      ${(S.versions || []).map(v => `
        <div class="kv-row"><span>${esc(v.reason || '章节快照')} · ${v.sections} 章 · ${esc((v.created_at || '').slice(5, 16))}</span>
        <span><button class="btn mini" data-act="restoreVersion" data-batch="${esc(v.batch_id)}">撤销本轮</button></span></div>`).join('') || '<div class="empty">还没有改写快照</div>'}
    </details>`);
  setTimeout(loadReadiness, 0);
}

/* ---------------- 基座能力 ---------------- */
function coreResultHtml() {
  const r = S.coreResult;
  if (!r) return '<div class="core-empty">点任意「运行」，用当前项目的招标文件、正文、我提供的资料与企业素材库跑一遍基座能力。</div>';
  if (r.ok === false) return `<div class="core-result bad"><b>${esc(r.name)}</b><div class="hint">${esc(r.error || '执行失败')}</div></div>`;
  const stats = (r.stats || []).filter(x => x.label !== 'download' && x.label !== 'output');
  const dl = (r.stats || []).find(x => x.label === 'download');
  const sections = (r.sections || []).filter(s => (s.rows || []).length).slice(0, 8);
  return `<div class="core-result">
    <div class="cr-head"><b>${esc(r.name)}</b>${r.skipped ? '<span class="chip warn">需要真实模型</span>' : '<span class="chip ok">已运行</span>'}</div>
    <div class="cr-summary">${esc(r.summary || '')}</div>
    ${stats.length ? `<div class="cr-stats">${stats.slice(0, 14).map(x => `<span class="cr-stat"><i>${esc(x.label)}</i>${esc(String(x.value ?? ''))}</span>`).join('')}</div>` : ''}
    ${dl ? `<a class="dl" href="${esc(String(dl.value))}" target="_blank">下载生成的文件</a>${r.note ? `<div class="cr-note">${esc(r.note)}</div>` : ''}` : ''}
    ${sections.map(sec => `<div class="cr-table"><div class="ct">${esc(sec.title)}</div><div class="tbl-wrap"><table><thead><tr>${Object.keys(sec.rows[0] || {}).map(k => `<th>${esc(k)}</th>`).join('')}</tr></thead><tbody>${sec.rows.slice(0, 12).map(row => `<tr>${Object.values(row).map(v => `<td>${esc(String(v ?? '').slice(0, 160))}</td>`).join('')}</tr>`).join('')}</tbody></table></div></div>`).join('')}
    ${r.text ? `<pre class="cr-text">${esc(r.text.slice(0, 4000))}</pre>` : ''}
    ${(!stats.length && !sections.length && !r.text) ? '<div class="hint">本次没有可展示的内容（该项已执行完成）。</div>' : ''}
  </div>`;
}

function renderCore() {
  const a = S.autoStatus || { enabled: true, stages: [], recent: [] };
  const eff = (S.settings || {}).effective || {};
  const stageHtml = (a.stages || []).map(s => `
    <div class="auto-stage">
      <div class="as-head"><div><b>${esc(s.label)}</b><span class="hint"> · ${esc(s.trigger)} · 生效 ${s.caps.length} 项</span></div>
        <button class="btn mini" data-act="runStage" data-stage="${esc(s.key)}" ${a.enabled ? '' : 'disabled'}>重跑该节点</button></div>
      <div class="as-desc">${esc(s.desc)}</div>
      <div class="as-caps">${(s.caps_detail || []).map(c => `<span class="cap-pill">${esc(c.name)}</span>`).join('') || '<span class="hint">该阶段未勾选能力</span>'}</div>
      <div class="as-last">${s.last && s.last.at ? `最近 ${esc(s.last.at)} · ${esc(s.last.summary || '')}` : '尚未自动触发'}</div>
    </div>`).join('');
  const recentHtml = (a.recent || []).slice(0, 8).map(r =>
    `<li><span class="hint">${esc(r.at)}</span> ${esc(r.stage || '')} · ${esc(r.summary || '')}</li>`).join('');
  const runsHtml = (S.brainRuns || []).slice(0, 5).map(r =>
    `<li><span class="hint">${esc((r.created_at || '').slice(5, 16))}</span> ${esc(r.planner || '')} · ${esc(r.goal || '')} · ${(r.trace || []).length} 步<br><span class="hint">「${esc((r.message || '').slice(0, 50))}」</span></li>`).join('');
  renderInto('tab-core', `
    <div class="card"><h2>运行记录 <span class="chip gray">只读</span></h2>
      <p class="desc">基座怎么用在「设置」里配置，主链路走到对应节点自动跑。这里只核对实际跑过什么，不用再手点能力。</p>
      <div class="row" style="align-items:center;gap:10px;flex-wrap:wrap">
        <span class="chip ${a.enabled ? 'ok' : 'warn'}">自动调用：${a.enabled ? '已开启' : '已关闭'}</span>
        <span class="hint">停用 ${eff.disabled || 0} 项 · 大脑可自动挑选 ${eff.brain_selectable || 0} 项 · 导出门禁 ${(eff.export_caps || []).length} 项</span>
        <button class="btn mini" data-act="goto" data-tab="settings">去设置</button>
      </div></div>
    <div class="card"><h2>各阶段生效能力 <span class="hint">${(a.stages || []).length} 个节点</span></h2>
      <div class="auto-stages">${stageHtml || '<div class="empty">状态加载中…</div>'}</div></div>
    <div class="card"><h2>最近自动运行</h2>${recentHtml ? `<ul>${recentHtml}</ul>` : '<div class="empty">还没有自动运行记录</div>'}</div>
    <div class="card"><h2>最近大脑轨迹 <span class="hint">来自 agent_runs 留痕</span></h2>
      ${runsHtml ? `<ul>${runsHtml}</ul>` : '<div class="empty">还没有大脑执行记录；在对话里说一句话就会留下轨迹</div>'}</div>
    <div class="card"><h2>单项调试结果</h2><div id="coreResult">${coreResultHtml()}</div></div>`);
}

/* ---------------- 设置：基座策略唯一入口 ---------------- */
function settingsDraft() {
  const c = S.settings || {};
  const allow = c.brain_auto_allow;
  return {
    auto_enabled: c.auto_enabled !== false,
    export_quality: c.export_quality !== false,
    allow_model_caps_auto: !!c.allow_model_caps_auto,
    disabled_caps: [...(c.disabled_caps || [])],
    brain_auto_allow: Array.isArray(allow) ? [...allow] : '*',
    _mode: Array.isArray(allow) ? 'whitelist' : 'all',
    _allowList: Array.isArray(allow) ? [...allow] : [],
    stages: Object.fromEntries(Object.entries(c.stage_config || {}).map(([k, v]) => [k, [...v]])),
    export_caps: [...(c.export_caps || [])],
  };
}
function capName(key) {
  const c = ((S.settings || {}).caps || []).find(x => x.key === key);
  return c ? c.name : key;
}
function capToggle(key, checked, attr, extra = '') {
  const dk = (attr + '|' + key).replace(/[^a-zA-Z0-9_-]/g, '_');
  return `<label class="set-cap"><input type="checkbox" data-draft-key="${esc(dk)}" ${attr}="${esc(key)}" ${checked ? 'checked' : ''} ${extra}/><span>${esc(capName(key))}</span></label>`;
}
/* ---------------- 企业与成员 ---------------- */

function renderUser() {
  const me = S.me || {};
  const av = $('#userAvatar'), nm = $('#userName');
  if (av) av.textContent = (me.name || me.username || '·').slice(0, 1);
  if (nm) nm.textContent = me.name || me.username || '—';
  const og = $('#userOrg');
  if (og) og.textContent = me.org_name || '—';
  const tk = $('#userTick');
  if (tk) tk.textContent = `${me.role_label || ''}${me.username ? ' · ' + me.username : ''}`;
  // 成员看不到也改不了平台级设置，避免点了才报 403
  const adv = $('#advSettings');
  if (adv) {
    adv.classList.toggle('is-readonly', !S.isOwner);
    const pop = adv.querySelector('.adv-pop');
    let tip = adv.querySelector('.adv-ro-tip');
    if (!S.isOwner && pop && !tip) {
      tip = document.createElement('div');
      tip.className = 'hint adv-ro-tip';
      tip.textContent = '当前账号是成员：运行方式与模型由企业管理员统一设置。';
      pop.appendChild(tip);
    } else if (S.isOwner && tip) tip.remove();
  }
}

async function loadOrg() {
  if (S.orgLoading) return;
  S.orgLoading = true;
  try { S.org = await api('/api/org'); }
  catch (e) { S.org = null; }
  finally { S.orgLoading = false; }
  if (S.activeTab === 'settings') renderSettings();
}

function orgCardHtml() {
  const o = S.org;
  const me = (o && o.me) || S.me || {};
  if (!o) {
    return `<div class="card"><h2>企业与成员 <span class="chip gray">加载中</span></h2>
      <div class="empty">正在读取本企业账号…</div></div>`;
  }
  const org = o.org || {}, stats = o.stats || {};
  const members = o.members || [];
  const rows = members.map(m => {
    const self = m.id === me.id;
    const off = m.status === 'disabled';
    const acts = !S.isOwner || self ? (self ? '<span class="hint">当前账号</span>' : '')
      : `<button class="btn mini" data-act="orgRole" data-uid="${esc(m.id)}" data-role="${m.role === 'owner' ? 'member' : 'owner'}">${m.role === 'owner' ? '降为成员' : '设为管理员'}</button>
         <button class="btn mini" data-act="orgReset" data-uid="${esc(m.id)}">重置密码</button>
         <button class="btn mini" data-act="orgStatus" data-uid="${esc(m.id)}" data-status="${off ? 'active' : 'disabled'}">${off ? '启用' : '停用'}</button>
         <button class="btn mini danger" data-act="orgDelUser" data-uid="${esc(m.id)}">删除</button>`;
    return `<div class="mem-row ${off ? 'off' : ''}">
      <span class="avatar sm">${esc((m.name || m.username || '?').slice(0, 1))}</span>
      <div class="mem-main">
        <div class="mem-name">${esc(m.name || m.username)}${self ? ' <span class="chip gray">我</span>' : ''}
          <span class="chip ${m.role === 'owner' ? 'blue' : 'gray'}">${esc(m.role_label)}</span>
          ${off ? '<span class="chip warn">已停用</span>' : ''}</div>
        <div class="mem-sub">${esc(m.username)} · ${m.last_login ? '上次登录 ' + esc(String(m.last_login).slice(5, 16)) : '还没登录过'}</div>
      </div>
      <div class="mem-acts">${acts}</div>
    </div>`;
  }).join('');

  return `<div class="card">
    <h2>企业与成员 <span class="chip blue">数据隔离</span>
      <span class="hint">${stats.projects ?? 0} 个项目 · ${stats.kb ?? 0} 篇知识库</span></h2>
    <p class="desc">每个企业单独一个数据库文件，项目、资料、知识库互不可见。这里给同事开账号，他们登录后看到的是同一家企业的数据。</p>
    <div class="org-head">
      <div><div class="org-name">${esc(org.name || '—')}</div>
        <div class="mem-sub">${stats.members ?? members.length} 个成员 · 创建于 ${esc(String(org.created_at || '').slice(0, 10))}</div></div>
      ${S.isOwner ? `<button class="btn mini" data-act="orgRename">改企业名称</button>` : ''}
    </div>
    <div class="mem-list">${rows || '<div class="empty">还没有成员</div>'}</div>
    ${S.isOwner ? (S.orgNew ? `<div class="kb-new">
        <div class="mem-form">
          <div><label class="lbl">姓名</label><input type="text" id="nuName" placeholder="例如 张三" /></div>
          <div><label class="lbl">登录账号</label><input type="text" id="nuUser" placeholder="字母或数字，例如 zhangsan" /></div>
        </div>
        <div class="mem-form">
          <div><label class="lbl">初始密码</label><input type="text" id="nuPass" placeholder="至少 8 位，发给同事后让他自己改" /></div>
          <div><label class="lbl">角色</label><select id="nuRole" class="kb-kind-select">
            <option value="member">成员（用业务功能）</option>
            <option value="owner">管理员（还能改模型与成员）</option>
          </select></div>
        </div>
        <div class="row" style="margin-top:10px"><button class="btn primary" data-act="orgAddUser">创建账号</button>
          <button class="btn" data-act="orgNewToggle">取消</button>
          <span class="hint">账号全系统唯一；创建后把账号密码发给同事，让他登录后在右上角改密码。</span></div>
      </div>` : `<div class="row" style="margin-top:12px">
        <button class="btn" data-act="orgNewToggle">给同事开账号</button>
        <button class="btn" data-act="pwdChange">改我自己的密码</button>
        <span class="grow"></span>
        <span class="hint">没有自助注册入口，只有管理员能加人。</span>
      </div>`) : `<div class="row" style="margin-top:12px">
        <button class="btn" data-act="pwdChange">改我自己的密码</button>
        <span class="hint">成员管理只有企业管理员能操作。</span></div>`}
  </div>`;
}

function configScopeHtml() {
  const c = S.configScope; if (!c) return '';
  const names = {
    'models.json': '模型与密钥', 'model_health.json': '模型健康状态',
    'privacy.json': '数据出域设置', 'budget.json': '预算与限额',
    'mode.txt': '运行模式', 'agent_config.json': '智能体策略' };
  const chip = (f) => f.source === 'none' ? '<span class="chip low">本企业未配置</span>'
    : f.inherited ? '<span class="chip blue">继承平台默认</span>'
    : '<span class="chip ok">本企业配置</span>';
  const rows = Object.entries(c.files || {}).map(([n, f]) =>
    `<div class="kv-row"><span>${esc(names[n] || n)}</span><span>${chip(f)}</span></div>`).join('');
  return `<div class="card"><h2>配置归属 <span class="hint">多企业共用一台服务器时按企业隔离</span></h2>
    <div class="desc">当前企业 <b>${esc(c.org)}</b>${c.platform ? '（平台默认企业）' : ''}${S.isPlatformAdmin ? ' · 平台管理员' : S.isOwner ? ' · 企业管理员' : ''}。模型与密钥只认本企业自己的文件；策略类可继承平台默认，本企业一旦保存就独立，改动不会影响别的企业。</div>
    <div style="margin-top:8px">${rows}</div>
    <div class="hint" style="margin-top:8px">${esc(c.note || '')} · 旧公共配置归属：${esc((c.ownership || {}).owner || 'default')}</div></div>`;
}

function embeddingCardHtml() {
  const c = S.embedding;
  if (!c) return '<details class="set-more"><summary>语义检索开关 <span class="hint">配置读取中</span></summary></details>';
  const cov = c.coverage || {};
  const ro = S.isOwner ? '' : 'disabled';
  const state = c.reason || (c.ready ? '已开启，生成与解读时自动生效' : '未开启');
  const provs = (c.providers || []).map(p =>
    `<option value="${esc(p.id)}"${p.id === c.provider ? ' selected' : ''}>${esc(p.label)}</option>`).join('');
  const cur = (c.providers || []).find(p => p.id === c.provider) || {};
  const dims = (Array.isArray(c.supported_dims) && c.supported_dims.length)
    ? c.supported_dims : [c.dimensions || cur.default_dim || 1024];
  return `<details class="set-more" id="embeddingMore">
    <summary>语义检索开关 <span class="hint">默认关闭 · 关着零费用、与现在完全一样</span></summary>
    <div class="card" style="margin-top:8px">
      <p class="desc">开启后，招标文件和知识库里「换了个说法」的条款也能被找到；关闭时仍按字面检索。密钥按企业单独保存，接口返回一律脱敏。任选一家兼容 /embeddings 的平台即可。</p>
      <div class="set-row"><label>向量平台<select id="embProvider" class="grow" ${ro}>${provs}</select></label></div>
      <div class="set-row"><label>接口地址<input id="embBase" class="grow" value="${esc(c.base_url || '')}" placeholder="https://api.siliconflow.cn/v1" ${ro}/></label></div>
      <div class="set-row"><label>模型名<input id="embModel" class="grow" value="${esc(c.model || '')}" placeholder="BAAI/bge-m3" ${ro}/></label></div>
      <div class="set-row"><label>向量维度<input id="embDim" class="grow" type="number" list="embDimList" value="${esc(String(c.dimensions || cur.default_dim || 1024))}" ${ro}/></label></div>
      <datalist id="embDimList">${dims.map(d => `<option value="${d}"></option>`).join('')}</datalist>
      <div class="set-row"><label><input type="checkbox" id="embEnabled" ${c.enabled ? 'checked' : ''} ${ro}/> 启用语义检索</label></div>
      <div class="set-row"><label>API Key　<input type="password" id="embKey" class="grow" autocomplete="off" placeholder="${c.api_key_configured ? '已保存，留空表示不修改' : '填写该平台的 API Key'}" ${ro}/></label></div>
      <div class="row"><button class="btn primary" data-act="embeddingSave" ${ro}>保存</button><button class="btn" data-act="embeddingTest" ${ro}>测通</button><span id="embMsg" class="hint">${esc(state)}</span></div>
      <p class="desc" id="embHint">${esc(cur.hint || '')}</p>
      <p class="desc">平台：${esc(String(c.provider_label || ''))}　模型：${esc(String(c.model || ''))}　维度：${esc(String(c.dimensions || ''))}　向量缓存：${esc(String(cov.current_model || 0))} 段 / 共 ${esc(String(cov.total || 0))} 段。换平台或模型会让旧向量失效，下次检索时自动重建。${S.isOwner ? '' : '当前账号是成员，设置由企业管理员统一管理。'}</p>
    </div>
  </details>`;
}

function renderSettings() {
  if (!S.settings) { renderInto('tab-settings', '<div class="empty">配置加载中…</div>'); return; }
  const d = S.settingsDraft || (S.settingsDraft = settingsDraft());
  const caps = S.settings.caps || [];
  const groups = S.settings.groups || [];
  const stageHtml = (S.settings.stages || []).filter(st => st.key !== 'export').map(st => `
    <div class="set-stage" data-stage="${esc(st.key)}">
      <div class="set-stage-head"><b>${esc(st.label)}</b><span class="hint"> · ${esc(st.trigger)} · 当前生效 ${st.caps.length} 项</span></div>
      <div class="set-stage-desc">${esc(st.desc)}</div>
      <details class="set-more"><summary>已选 ${(d.stages[st.key] || []).length} 项，点开调整</summary>
        <div class="set-caps">${caps.map(c => capToggle(
          c.key, (d.stages[st.key] || []).includes(c.key),
          `data-cfg-stage="${esc(st.key)}" data-cap`, '')).join('')}</div>
      </details>
    </div>`).join('');
  const disabledHtml = groups.map(g => {
    const items = caps.filter(c => c.group === g);
    if (!items.length) return '';
    return `<div class="set-group"><div class="mini-title">${esc(g)}</div><div class="set-caps">${items.map(c =>
      capToggle(c.key, d.disabled_caps.includes(c.key), 'data-cfg-disabled', '')).join('')}</div></div>`;
  }).join('');
  const allowHtml = caps.map(c => capToggle(
    c.key, d._allowList.includes(c.key), 'data-cfg-allow', '')).join('');
  const debugOptions = caps.map(c => `<option value="${esc(c.key)}" ${S.coreKey === c.key ? 'selected' : ''}>${esc(c.name)}</option>`).join('');
  if (!S.org && !S.orgLoading) loadOrg().catch(() => {});
  renderInto('tab-settings', `
    <div class="row" style="margin-bottom:10px"><a class="btn mini" href="/vue/settings.html">试用新版设置</a></div>
    ${orgCardHtml()}
    ${configScopeHtml()}
    ${S.isOwner ? '' : '<div class="card"><h2>下面是本企业配置</h2><p class="desc">运行方式、模型接入、策略与门禁按企业隔离，当前账号只能查看，改动请联系企业管理员。</p></div>'}
    <div class="card set-savebar">
      <div><h2>基座与智能体策略</h2><p class="desc">这一页是配置，不是运行按钮。保存后立即生效：自动编排、大脑挑选工具、导出门禁、单项调试全按这份配置执行，不用重启。</p></div>
      <div class="row"><button class="btn primary" data-act="settingsSave">保存设置</button><button class="btn" data-act="settingsReset">放弃修改</button></div>
    </div>
    <div class="card">
      <h2>总开关与大脑范围</h2>
      <div class="set-row"><label><input type="checkbox" data-draft-key="cfg_auto" data-cfg="auto_enabled" ${d.auto_enabled ? 'checked' : ''}/> 启用基座自动调用（关掉后所有阶段与 stage.* 都不再跑）</label></div>
      <div class="set-row"><label><input type="checkbox" data-draft-key="cfg_model" data-cfg="allow_model_caps_auto" ${d.allow_model_caps_auto ? 'checked' : ''}/> 允许需要真实模型的 AI 增强能力参与自动调用（默认关，慢且费 token）</label></div>
      <div class="set-row"><label><input type="checkbox" data-draft-key="cfg_export_quality" data-cfg="export_quality" ${d.export_quality ? 'checked' : ''}/> 导出前列出中等生成质量缺陷（响应覆盖 / 实质响应 / 评分项，需逐条确认）</label></div>
      <div class="set-row"><span class="hint">章节级事实硬伤（无据数值 / 占位符 / 空章）始终会列为重点提醒；不修也能点「仍然导出」，文件会记下这次提醒。</span></div>
      <div class="set-row"><span class="hint">大脑可以自动挑选</span>
        <select data-cfg="allow_mode" data-draft-key="cfg_allow_mode" style="width:auto">
          <option value="all" ${d._mode === 'all' ? 'selected' : ''}>全部已启用的本地能力</option>
          <option value="whitelist" ${d._mode === 'whitelist' ? 'selected' : ''}>只挑下面勾选的能力</option>
        </select></div>
      <div class="set-group ${d._mode === 'whitelist' ? '' : 'dim'}" id="allowList"><div class="mini-title">大脑自动挑选白名单</div>
        <details class="set-more"><summary>已勾选 ${d._allowList.length} 项，点开调整</summary>
          <div class="set-caps">${allowHtml}</div></details></div>
    </div>
    <div class="card"><h2>各阶段自动跑哪些能力 <span class="hint">勾选即生效</span></h2>
      <p class="desc">阶段到了就按这里跑，结论回流到体检问题、写作约束与交付门禁；停用的能力即使勾了也不会执行。</p>
      ${stageHtml}</div>
    <div class="card"><h2>导出前门禁项</h2>
      <p class="desc">Word 与 Markdown 共用这一组门禁；高风险问题必须逐条确认才给下载。</p>
      <div class="set-caps">${caps.map(c => capToggle(c.key, d.export_caps.includes(c.key), 'data-cfg-export', '')).join('')}</div></div>
    <div class="card"><h2>停用的能力</h2>
      <p class="desc">勾上的能力任何路径都不执行：大脑点名、阶段自动跑、单项调试、导出门禁一视同仁。</p>
      <details class="set-more"><summary>按分组查看与勾选</summary>${disabledHtml}</details></div>
    <div class="card"><h2>其他设置</h2>
      <div class="row" style="flex-wrap:wrap;gap:8px">
        <button class="btn" data-act="openModelsForSettings">模型接入与分工</button>
        <button class="btn" data-act="openPrivacy">数据出域</button>
        <button class="btn" data-act="goto" data-tab="usage">预算护栏与用量</button>
        <button class="btn" data-act="goto" data-tab="core">查看运行记录</button>
      </div>
      <p class="desc">配置文件：${esc((S.settings.effective || {}).file || 'data/agent_config.json')}（只存策略，不存密钥）。</p>
      ${embeddingCardHtml()}</div>
    <div class="card"><h2>单项调试 <span class="chip gray">非主用法</span></h2>
      <p class="desc">只用于单看某一项结果或跑需要真实模型的能力；平时不需要点，智能体会按上面的配置自己调用。</p>
      <div class="row"><select id="debugCap" style="max-width:280px">${debugOptions}</select>
        <input type="text" id="debugPayload" class="grow" placeholder="可选参数：检索词 / 技能名 / 报价表文本" value="${esc(S.corePayload || '')}"/>
        <button class="btn" data-act="debugRun">运行一次</button></div>
      <div id="coreResult">${coreResultHtml()}</div></div>`);
}

function onSettingsChange(e) {
  const t = e.target;
  if (!t.dataset || !t.dataset.cfg && !t.dataset.cfgStage && !t.dataset.cfgDisabled &&
      !t.dataset.cfgAllow && !t.dataset.cfgExport) return;
  const d = S.settingsDraft || (S.settingsDraft = settingsDraft());
  if (t.dataset.cfg === 'auto_enabled') d.auto_enabled = t.checked;
  else if (t.dataset.cfg === 'allow_model_caps_auto') d.allow_model_caps_auto = t.checked;
  else if (t.dataset.cfg === 'export_quality') d.export_quality = t.checked;
  else if (t.dataset.cfg === 'allow_mode') {
    d._mode = t.value;
    const box = $('#allowList'); if (box) box.classList.toggle('dim', t.value !== 'whitelist');
  } else if (t.dataset.cfgStage) {
    const stage = t.dataset.cfgStage, key = t.dataset.cap;
    const cur = new Set(d.stages[stage] || []);
    if (t.checked) cur.add(key); else cur.delete(key);
    d.stages[stage] = [...cur];
  } else if (t.dataset.cfgDisabled) {
    const s = new Set(d.disabled_caps);
    if (t.checked) s.add(t.dataset.cfgDisabled); else s.delete(t.dataset.cfgDisabled);
    d.disabled_caps = [...s];
  } else if (t.dataset.cfgAllow) {
    const s = new Set(d._allowList);
    if (t.checked) s.add(t.dataset.cfgAllow); else s.delete(t.dataset.cfgAllow);
    d._allowList = [...s];
  } else if (t.dataset.cfgExport) {
    const s = new Set(d.export_caps);
    if (t.checked) s.add(t.dataset.cfgExport); else s.delete(t.dataset.cfgExport);
    d.export_caps = [...s];
  }
}

/* ---------------- 导出提醒 ----------------
   后端对检查不符合返回 409（只是提醒信号）；这里列出完整清单，
   点「仍然导出」换知情令牌后照常下载，令牌一次性且绑定正文指纹。 */
/* 导出结果的持久反馈：知情导出的文件不是正式交付件，一闪而过的 toast
   不够，用户必须能看见「下载了什么、为什么不能直接提交」。 */
function exportNotice(msg, tone = 'warn') {
  const title = { pending: '正在导出', ok: '文件已开始下载',
                  error: '导出未完成', warn: '知情导出已完成' }[tone] || '导出提示';
  let box = document.getElementById('exportNotice');
  if (!box) {
    box = document.createElement('div');
    box.id = 'exportNotice';
    box.className = 'card';
    box.style.cssText = 'position:fixed;right:22px;bottom:22px;z-index:9998;'
      + 'max-width:420px;padding:14px 16px;box-shadow:var(--shadow);';
    document.body.appendChild(box);
  }
  box.innerHTML = `<div class="row gap-sm"><b style="font-size:13px">${title}</b>
    <button class="btn mini row-end" id="exportNoticeClose">知道了</button></div>
    <div class="li-sub" style="margin-top:6px">${esc(msg)}</div>`;
  const close = document.getElementById('exportNoticeClose');
  if (close) close.onclick = () => box.remove();
}

/* 一份正文都没有时不静默导出空壳 Word。契约不变：仍然可以下载，
   只是先让用户明确知道拿到的会是没有正文的文件。 */
async function confirmEmptyBody() {
  const secs = (S.project && S.project.sections) || [];
  if (secs.some(s => String(s.text || '').trim())) return true;
  return uiConfirm(
    '当前项目还没有任何正文章节。继续下载会得到一个没有正文的 Word，不能用于投标。\n\n'
    + '建议先导入招标文件并写标书。仍要导出空文件吗？',
    { title: '还没有正文' });
}

async function fetchExport(url, allowGate = true) {
  try {
    // 门禁预检走只读 /export/gate（200），不再拿下载端点的 409 当探测信号：
    // 浏览器会把 4xx 自动记成红色 console error，用户点一次下载就看到两次误导性 409。
    // 后端 409 契约原样保留（真冲突兜底），这里只是让正常流程不再主动撞它。
    const u0 = new URL(url, location.origin);
    const isExport = /\/export(?:\.docx)?$/.test(u0.pathname);
    if (allowGate && isExport && !u0.searchParams.has('token')) {
      let g = null;
      try { g = await api(`/api/projects/${S.pid}/export/gate`); } catch (_) { /* 预检失败仍走原流程 */ }
      if (g && g.fingerprint && (g.requires_confirm || g.blocked)) {
        u0.searchParams.delete('token');
        openExportGate(g, u0.pathname + u0.search);
        return;
      }
    }
    const r = await fetch(url);
    if (r.status === 409) {
      const g = await r.json();
      // 409 只是提醒信号：任何检查不符合都能点「仍然导出」换令牌重试，不做死路
      if (allowGate && g.fingerprint) {
        const u = new URL(url, location.origin);
        u.searchParams.delete('token');           // 重试时别把旧令牌叠进地址
        openExportGate(g, u.pathname + u.search);
        return;
      }
      throw new Error(g.error || '还没通过下载前的检查');
    }
    if (!r.ok) throw new Error('下载失败 ' + r.status);
    const b = await r.blob();
    const cd = r.headers.get('content-disposition') || '';
    const fallback = url.includes('export.docx') ? '投标文件.docx' : '投标文件.md';
    const name = cd.includes("filename*=UTF-8''")
      ? decodeURIComponent(cd.split("filename*=UTF-8''")[1]) : fallback;
    const ack = r.headers.get('X-Export-Ack') === '1';
    const issues = r.headers.get('X-Gate-Issues') || '0';
    const fileCheck = r.headers.get('X-File-Check') || '';
    const a = document.createElement('a');
    a.href = URL.createObjectURL(b); a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    toast(ack
      ? `已开始下载：检查有 ${issues} 项提醒${fileCheck === 'acknowledged' ? '，最终文件也有未通过项' : ''}，提交前请人工复核`
      : (url.includes('export.docx') ? 'Word 已开始下载' : '文件已开始下载'));
    if (ack) {
      const blocked = r.headers.get('X-Gate-Blocked') === '1';
      const kind = r.headers.get('X-Artifact-Kind') || '';
      exportNotice(`已下载「${name}」：${issues} 项提醒已随文件记录`
        + `${blocked ? '，其中含硬性否决项' : ''}`
        + `${fileCheck === 'acknowledged' ? '，最终文件检查未通过' : ''}。`
        + `产物登记为${kind === 'acknowledged_draft' ? '知情导出草稿' : '审阅件'}，`
        + '不可直接提交投标，请先人工复核。');
    }
  } catch (e) { toast(e.message); exportNotice('导出失败：' + e.message, 'error'); }
}

function updateExportGateHint() {
  if (!S.exportGate) return;
  const boxes = $$('#exportGateBody .export-check');
  const n = boxes.filter(b => b.checked).length;
  $('#exportGateHint').textContent = `已核对 ${n} / ${boxes.length}`;
}

function openExportGate(g, url) {
  S.exportGate = g; S.exportUrl = url;
  const body = $('#exportGateBody'), btn = $('#exportGateConfirmBtn'), hint = $('#exportGateHint');
  // 检查不符合只作提醒：不管是章节未达标、必需检查没跑完，还是高风险问题，
  // 都完整列出来，但主按钮始终可点，不再把用户挡在下载之外。
  if (btn) { btn.hidden = false; btn.textContent = '仍然导出'; }
  const all = g.issues || [];
  // 后端已按类别聚合：默认只给一句结论 + 几条最该看的，明细折叠，一条都不省略
  const groups = (g.groups || []).length ? g.groups
    : (all.length ? [{ key: 'other', label: '全部提醒', count: all.length,
                       high: all.filter(i => i.level === '高' || i.blocking).length,
                       items: all }] : []);
  const top = (g.top || []).slice(0, 6);
  const high = all.filter(i => i.level === '高' || i.blocking).length;
  const impactTag = i => i.business_level ? `<span class="chip low">${esc(i.business_level)}</span> ` : '';
  const card = i => `<div class="card" style="margin:8px 0">
      <h3 style="color:#c0392b">${impactTag(i)}需重点核对${i.group_label ? ' · ' + esc(i.group_label) : ''} · ${esc(i.title || '')}</h3>
      <div class="desc">${esc(i.detail || '')}</div>
      ${i.action ? `<div class="hint" style="margin-top:6px">${esc(i.action || '')}</div>` : ''}</div>`;
  const row = i => `<div class="hint" style="margin:6px 0">· ${impactTag(i)}<b>${esc(i.title || '')}</b>${i.action ? `（${esc(i.action)}）` : ''}
      ${i.detail ? `<div style="margin-left:12px;color:#667085">${esc(String(i.detail).slice(0, 140))}</div>` : ''}</div>`;
  const im = g.impact || {};
  const impactLine = g.impact ? `<p class="hint">按评审后果：硬性否决 ${im['硬性否决'] || 0} 项 · 硬性扣分 ${im['硬性扣分'] || 0} 项 · 加分项 ${im['加分项'] || 0} 项 · 参考项 ${im['参考项'] || 0} 项。${esc(g.impact_note || '')}</p>` : '';
  // 章数 / 条数 / 提醒条目数是三种口径，分开写清楚，别让用户以为数字打架
  const countsLine = (g.sections_undeliverable !== undefined || g.matrix_required !== undefined)
    ? `<p class="hint">整本口径：未达交付标准 ${g.sections_undeliverable || 0} 章 · 必需要求 ${g.matrix_required || 0} 条 / 逐条响应矩阵 ${g.matrix_rows || 0} 条；本卡列出的是 ${all.length} 项提醒条目。</p>` : '';
  body.innerHTML = `
    <div class="card"><h3>${esc(g.summary || `下载前的检查有 ${all.length} 项提醒${high ? `，其中 ${high} 项要重点核对` : ''}`)}</h3>
      <p class="desc">这些<b>只是提醒，不会禁止下载</b>。可以直接导出，也可以先回去改。导出的文件会记下这次提醒，提交前仍建议逐项人工复核。</p>
      ${impactLine}
      ${countsLine}
      ${all.length > 3 ? '<p class="hint">完整清单按类别收在下面，点开可逐条查看，一条都没有省略。</p>' : ''}</div>
    ${top.map(card).join('')}
    ${groups.map(gr => `<details class="fold" style="margin:10px 0">
        <summary>${esc(gr.label || '其他提醒')} · ${gr.count || 0} 项${gr.high ? `（${gr.high} 项重点）` : ''}</summary>
        <div style="margin-top:8px">${(gr.items || []).map(row).join('')}</div>
      </details>`).join('')}
    ${!all.length ? '<div class="card"><div class="desc">本轮没有需要列出的提醒。</div></div>' : ''}`;
  if (hint) hint.textContent = '提醒不影响下载';
  body.onchange = null;
  $('#exportModal').hidden = false;
}

async function confirmExportGate(el) {
  const g = S.exportGate;
  if (!g) return;
  busy(el, true, '导出中…');
  try {
    const r = await api(`/api/projects/${S.pid}/export/confirm`, 'POST',
                        { fingerprint: g.fingerprint, acknowledge: true });
    const sep = S.exportUrl.includes('?') ? '&' : '?';
    $('#exportModal').hidden = true;
    S.exportGate = null;
    // 弹窗关掉后页面不能一片安静：先给一条不会自动消失的进度反馈
    exportNotice('已记录知情确认，正在生成文件，请稍候…', 'pending');
    // 令牌过期 / 正文又变了也只重新弹提醒，不让用户在错误提示里卡死
    await fetchExport(`${S.exportUrl}${sep}token=${encodeURIComponent(r.token)}`, true);
  } catch (e) {
    toast(e.message);
    exportNotice('导出没有完成：' + e.message + '。请重新点下载再确认一次。', 'error');
  }
  finally { busy(el, false); }
}

/* ---------------- 动作 ---------------- */
const ACT = {
  /* ---- 语义检索开关（设置页「其他设置」里默认收起） ---- */
  async embeddingSave() {
    const on = !!document.getElementById('embEnabled')?.checked;
    const key = (document.getElementById('embKey')?.value || '').trim();
    const patch = {
      enabled: on,
      provider: document.getElementById('embProvider')?.value || '',
      base_url: (document.getElementById('embBase')?.value || '').trim(),
      model: (document.getElementById('embModel')?.value || '').trim(),
      dimensions: parseInt(document.getElementById('embDim')?.value || '0', 10) || 0,
    };
    if (key) patch.api_key = key;
    try {
      await api('/api/embedding', 'POST', patch);
      S.embedding = await api('/api/embedding');
      renderSettings();
      const more = document.getElementById('embeddingMore');
      if (more) more.open = true;
      const st = S.embedding || {};
      toast(!on ? '语义检索已关闭，恢复纯词法（零费用）'
        : (st.ready ? '语义检索已开启，生成与解读时自动生效'
                    : `已保存，但暂未生效：${st.reason || '配置不完整'}`));
    } catch (err) { toast(err.message || '保存失败'); }
  },
  async embeddingTest() {
    const msg = document.getElementById('embMsg');
    if (msg) msg.textContent = '正在测通…';
    try {
      const r = await api('/api/embedding/test', 'POST', {});
      S.embedding = await api('/api/embedding');
      renderSettings();
      const more = document.getElementById('embeddingMore');
      if (more) more.open = true;
      const want = parseInt(document.getElementById('embDim')?.value || '0', 10);
      if (!r.ok) toast(`测通失败：${r.error}`);
      // 维度不符时余弦算不出来（长度不同直接 0 分），这里必须当场拦住
      else if (want && Number(r.dim) !== want) toast(`测通成功，但接口返回 ${r.dim} 维，与当前设置 ${want} 维不一致——请把维度改成 ${r.dim} 再保存，否则检索会全部失效`);
      else toast(`测通成功，向量维度 ${r.dim}`);
    } catch (err) { toast(err.message || '测通失败'); }
  },
  /* ---- 企业与成员 ---- */
  openManual() { window.open('/manual.html', '_blank'); },
  async logout() {
    if (!await uiConfirm('退出登录？下次要重新输密码。')) return;
    try { await api('/api/auth/logout', 'POST', {}); } catch (_) { /* 忽略 */ }
    location.replace('/login.html');
  },
  orgNewToggle() { S.orgNew = !S.orgNew; renderSettings(); },
  async orgAddUser(el) {
    const name = (($('#nuName') || {}).value || '').trim();
    const username = (($('#nuUser') || {}).value || '').trim();
    const password = (($('#nuPass') || {}).value || '');
    const role = (($('#nuRole') || {}).value || 'member');
    if (!username) return toast('先填登录账号');
    if (password.length < 8) return toast('初始密码至少 8 位');
    busy(el, true, '创建中…');
    try {
      await api('/api/org/users', 'POST', { name, username, password, role });
      S.orgNew = false;
      await loadOrg();
      toast(`账号 ${username} 已创建，把密码发给同事`);
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async orgRole(el) {
    const role = el.dataset.role;
    if (!await uiConfirm(role === 'owner' ? '设为管理员？他能改模型、加人和删人。' : '降为成员？他将不能再改设置和管人。')) return;
    try {
      await api('/api/org/users/' + encodeURIComponent(el.dataset.uid), 'PUT', { role });
      await loadOrg(); toast('已更新角色');
    } catch (e) { toast(e.message); }
  },
  async orgStatus(el) {
    const off = el.dataset.status === 'disabled';
    if (off && !await uiConfirm('停用后这个账号立刻登不进去（会话一并失效），确定？')) return;
    try {
      await api('/api/org/users/' + encodeURIComponent(el.dataset.uid), 'PUT',
        { status: el.dataset.status });
      await loadOrg(); toast(off ? '已停用' : '已启用');
    } catch (e) { toast(e.message); }
  },
  async orgReset(el) {
    const pwd = await uiPrompt('给这个成员设个新密码（至少 8 位）：\n设好后他不会掉线，但要重新登录才用新密码。');
    if (pwd === null) return;
    if (pwd.length < 8) return toast('密码至少 8 位');
    try {
      await api('/api/org/users/' + encodeURIComponent(el.dataset.uid), 'PUT', { password: pwd });
      toast('密码已重置，记得告诉他');
    } catch (e) { toast(e.message); }
  },
  async orgDelUser(el) {
    if (!await uiConfirm('删除这个成员？他的账号立刻失效，已写的标书仍留在企业里。')) return;
    try {
      await api('/api/org/users/' + encodeURIComponent(el.dataset.uid), 'DELETE');
      await loadOrg(); toast('已删除');
    } catch (e) { toast(e.message); }
  },
  async orgRename() {
    const cur = (S.org && S.org.org && S.org.org.name) || '';
    const name = await uiPrompt('企业名称：', cur);
    if (name === null || !name.trim()) return;
    try {
      await api('/api/org', 'PUT', { name: name.trim() });
      await loadOrg(); toast('已改名');
    } catch (e) { toast(e.message); }
  },
  async pwdChange() {
    const old = await uiPrompt('当前密码：'); if (old === null) return;
    const nw = await uiPrompt('新密码（至少 8 位）：'); if (nw === null) return;
    if (nw.length < 8) return toast('密码至少 8 位');
    try {
      await api('/api/auth/password', 'POST', { old, new: nw });
      toast('密码已改，下次用新密码登录');
    } catch (e) { toast(e.message); }
  },

  goto(el) {
    const tab = typeof el === 'string' ? el : (el && el.dataset ? el.dataset.tab : '');
    if (tab) switchTab(tab);
  },
  /* 引导卡的四步：除了「上传」必须由用户选文件，其余都直接把动作做完。
     以前四个按钮一律是 goto，点「生成目录」只跳页、点「检查并下载」跳到
     一个根本没有下载入口的页面，用户还得自己再找一次同名按钮。 */
  async guideGo(el) {
    const step = Number(el.dataset.step || 1);
    const tab = el.dataset.tab || 'materials';
    await switchTab(tab);
    if (S.activeTab !== tab) return;   // 切页被未保存的正文拦下，不盲目往下跑
    if (step === 1) return;            // 上传：跳到资料页即可
    if (step === 2) {
      if (!((S.project.tender_text || '').trim())) return toast('还没有招标文件，先去「资料」导入');
      return ACT.outline(el);
    }
    if (step === 3) {
      // 老项目可能从没点过确认目录，但正文早已写完；这时不该把人挡在门外。
      const wrote = secList().some(s => String(s.text || '').trim());
      if (!String(S.project.outline_confirmed_at || '').trim() && !wrote) {
        toast('目录还没确认：先在左边核对章节，点「确认目录」再写正文');
        return;
      }
      return ACT.compose(el);
    }
    if (step === 4) return ACT.check(el);
  },
  ask(el) { return askBrain(el.dataset.text || ''); },
  async selectProject(el) { await selectProject(el.dataset.pid); },
  async newProject() {},
  async setMode() {},

  closeModels() { closeModels(); },

  /* ---- 全部项目 ---- */
  openProjects(el) { openProjectsPanel(el && el.dataset ? el.dataset.pid : ''); },
  closeProjects() { const m = $('#projectsModal'); if (m) m.hidden = true; },
  newProjectBtn() { newProject(); },
  projFilter(el) {
    const f = el.dataset.filter || 'all';
    S.projPanel = { ...(S.projPanel || {}), filter: f };
    $$('#projFilter button').forEach(b => b.classList.toggle('on', b.dataset.filter === f));
    renderProjectsPanel();
  },
  async projOpen(el) {
    const pid = el.dataset.pid;
    const m = $('#projectsModal'); if (m) m.hidden = true;
    if (pid && pid !== S.pid) await selectProject(pid);
  },
  projArchive(el) { archiveProjectFromPanel(el.dataset.pid, el.dataset.on === '1'); },
  projToKb(el) { toKbFromPanel(el.dataset.pid, el); },
  projDelete(el) { deleteProjectFromPanel(el.dataset.pid); },
  async modelUse(el) { await setActiveModel(el.dataset.id); },
  async modelTest(el) {
    const id = el.dataset.id;
    busy(el, true, '测试中…');
    try {
      const r = await api('/api/models/' + encodeURIComponent(id) + '/test', 'POST');
      S.testResult = { id, text: r.ok
        ? `OK · ${r.latency_ms}ms · 回复：${r.reply || '连通'}`
        : `失败 · ${r.error || (r.latency_ms + 'ms')}` };
      renderModels();
      toast(r.ok ? '连通正常' : '连不上，看卡片里的错误');
    } catch (err) { toast(err.message); }
    finally { busy(el, false); }
  },
  modelEdit(el) {
    const m = ((S.modelReg || {}).models || []).find(x => x.id === el.dataset.id);
    if (!m) return;
    fillForm({ id: m.id, preset: m.provider || 'custom', label: m.label,
               base_url: m.base_url, api_key: '', model: m.model,
               max_tokens: m.max_tokens, timeout: m.timeout });
    const t = $('#mLabel'); if (t) { t.focus(); t.scrollIntoView({ block: 'nearest' }); }
    toast('已带入表单，改完点「保存并接入」');
  },
  async modelDel(el) {
    const id = el.dataset.id;
    const m = ((S.modelReg || {}).models || []).find(x => x.id === id);
    if (!await uiConfirm(`删除模型「${(m || {}).label || id}」？`)) return;
    try {
      await api('/api/models/' + encodeURIComponent(id), 'DELETE');
      S.testResult = null; S.modelDraft = blankForm();
      await openModels(true);
      const st = await api('/api/state');
      S.mode = st.mode; S.model = st.model; S.models = st.models; renderHeader();
      toast('已删除');
    } catch (err) { toast(err.message); }
  },
  async modelSave(el) {
    const d = readForm();
    if (!d.base_url.trim() || !d.model.trim() || (!d.api_key.trim() && !d.id)) {
      toast('接口地址、模型名、API Key 都要填'); return;
    }
    busy(el, true, '保存中…');
    try {
      await api('/api/models', 'POST', {
        id: d.id, provider: d.preset, label: d.label.trim() || d.model.trim(),
        base_url: d.base_url.trim(), api_key: d.api_key.trim(), model: d.model.trim(),
        max_tokens: Number(d.max_tokens) || 4096, timeout: Number(d.timeout) || 120,
      });
      S.modelDraft = blankForm(); S.testResult = null;
      await openModels(true);
      const st = await api('/api/state');
      S.mode = st.mode; S.model = st.model; S.models = st.models; renderHeader();
      toast('已接入，可在顶部下拉切换');
    } catch (err) { toast(err.message); }
    finally { busy(el, false); }
  },
  modelReset() { S.modelDraft = blankForm(); fillForm(S.modelDraft); },

  quick(el) { sendChat(el.dataset.text, el.dataset.action); },
  rejob(el) {
    const jid = el.dataset.jid;
    if (!jid) return;
    const idx = CHAT.messages.findIndex(m => m.job && m.job.jid === jid);
    if (idx >= 0) CHAT.messages[idx].job = { ...(CHAT.messages[idx].job || {}), status: 'running', step: '重新连接…' };
    pollChatJob(jid, idx >= 0 ? idx : null);
  },
  async restoreVersion(el) {
    if (!(await secFlush())) return toast('正文还没保存成功，先处理保存或冲突提示再恢复快照');
    if (!await uiConfirm('把该批快照里的章节原样恢复？之后新做的改动会保留在别的章节上。')) return;
    try {
      const r = await api('/api/projects/' + S.pid + '/versions/' + encodeURIComponent(el.dataset.batch) + '/restore', 'POST', {});
      // 目录快照会换掉全部章节 id：别让编辑器还停在已失效的旧 id 上
      if ((r.invalidated || []).includes(S.secId)) { S.secId = ''; S.secConflict = null; }
      S.secCandidate = null;
      S.skipDraft = true;
      await refresh();
      if (r.partial) {
        const names = (r.skipped || []).map(x => x.reason).join('；');
        toast(`部分恢复：成功 ${r.restored} 章，跳过 ${(r.skipped || []).length} 章（${names}）`);
      } else if (r.outline) toast(`目录与 ${r.restored} 章正文已按快照重建，旧章节引用已失效`);
      else toast(`已恢复 ${r.restored} 章，章节版本已更新，旧窗口的旧版保存会被拒绝`);
    } catch (e) { toast(e.message); }
  },
  async checkStatus(el) {
    try {
      await api('/api/projects/' + S.pid + '/checks/' + encodeURIComponent(el.dataset.id) + '/status',
                'POST', { status: el.dataset.status });
      await refresh();
      toast(el.dataset.status === 'accepted' ? '已采纳' : el.dataset.status === 'ignored' ? '已忽略，本条不会自动改写' : '已重置');
    } catch (e) { toast(e.message); }
  },
  openPrivacy() { openPrivacy(); },
  closePrivacy() { closePrivacy(); },
  async privacyAck(el) {
    const allow = el.dataset.allow === '1';
    try {
      const p = await api('/api/privacy', 'POST', {
        acknowledged: true, allow_outbound: allow,
        scrub_sensitive: ($('#privacyScrub') || {}).checked !== false,
        max_chars: Number(($('#privacyMax') || {}).value) || 12000,
      });
      S.privacy = p;
      const st = await api('/api/state');
      S.mode = st.mode; S.model = st.model; S.models = st.models;
      renderHeader(); renderPrivacy();
      const m = $('#privacyModal'); if (m) m.hidden = true;
      toast(allow ? '已确认：仅必要片段外发' : '已禁止外发：全部模型调用走本地');
    } catch (e) { toast(e.message); }
  },
  async privacyToggle() {
    const on = !(S.privacy && S.privacy.allow_outbound);
    try {
      const p = await api('/api/privacy', 'POST', { acknowledged: true, allow_outbound: on });
      S.privacy = p;
      const st = await api('/api/state');
      S.mode = st.mode; S.model = st.model; S.models = st.models;
      renderHeader(); renderPrivacy();
      toast(on ? '已开启外发' : '已关闭外发，已切回演示模式');
    } catch (e) { toast(e.message); }
  },
  async usageRefresh() { await loadUsage(); renderUsage(); toast('已刷新'); },
  async budgetSave() {
    const v = Number(($('#budgetLimit') || {}).value) || 200000;
    try { await api('/api/budget', 'POST', { daily_token_limit: v }); await loadUsage(); renderUsage(); toast('上限已保存'); }
    catch (e) { toast(e.message); }
  },
  async budgetToggle() {
    const on = !((S.budget && S.budget.budget) || {}).enabled;
    try { await api('/api/budget', 'POST', { enabled: on }); await loadUsage(); renderUsage(); toast(on ? '已启用护栏' : '已停用护栏'); }
    catch (e) { toast(e.message); }
  },
  async locate(el) {
    try {
      S.locate = await api('/api/projects/' + S.pid + '/locate?page=' +
                           encodeURIComponent(el.dataset.page) + '&q=' +
                           encodeURIComponent(el.dataset.q || ''));
      const em = $('#evidenceModal'); if (em) em.hidden = true;
      openLocate();
    } catch (e) { toast(e.message); }
  },
  closeLocate() { closeLocate(); },
  async showEvidence(el) {
    try {
      const r = await api(`/api/projects/${S.pid}/evidence?sid=${encodeURIComponent(el.dataset.sid)}`);
      const history = await api(`/api/projects/${S.pid}/sections/${encodeURIComponent(el.dataset.sid)}/generation-runs`);
      $('#evidenceBody').innerHTML = `
        <div class="card"><h3>本次输入来源 / 支撑证据 / 历史生成记录</h3>
          <p class="desc">输入来源证明本次生成看过哪些材料。支持关系须在逐条要求中核对；历史记录和候选材料不会自动视为事实证明。</p>
          ${history.source_unknown ? '<p class="hint">当前正文来源未完整记录或已经人工修改，需复核。</p>' : ''}
          ${(r.support_links || []).map(l => `<div class="hint">${esc(l.requirement_text)} · ${esc(l.relation === 'supports' ? '人工核对支持' : l.relation === 'conflicts' ? '关联冲突' : '候选关联')}${l.fresh ? '' : ' · 材料已变化，待复核'} · ${esc(l.reviewer)} · ${esc(l.note)}</div>`).join('')}
        </div>
        ${r.rows.map(e => `<div class="card">
          <h3>${esc(e.scope)} · ${esc(e.title)} <span class="chip gray">${esc(e.relation === 'legacy' ? '历史来源未知' : e.status === 'candidate' ? '本次输入候选' : e.status === 'review' ? '待复核' : e.status === 'historical' ? '历史来源' : e.status === 'missing' ? '待补依据' : '来源记录')}</span></h3>
          <div class="hint">${esc(e.ref)}${e.page ? ` · 第 ${e.page} 页` : ''}${e.doc_id ? ` · ${esc(e.doc_id)}` : ''}</div>
          <div class="desc" style="margin-top:6px">${esc(e.snippet)}</div>
          ${e.page ? `<button class="btn mini" style="margin-top:8px" data-act="locate" data-page="${e.page}" data-q="${esc((e.snippet || '').slice(0, 18))}">回跳原文 · 第 ${e.page} 页</button>` : ''}
        </div>`).join('') || '<div class="empty">本章还没有来源记录</div>'}
        ${history.rows.map(run => `<details class="card">
          <summary>${run.current ? '当前正文 · ' : ''}${esc(run.task)} · ${esc(run.status)} · ${esc(run.created_at)}</summary>
          <p class="hint">开始版本 ${run.base_ver} · 提交版本 ${run.committed_ver} · ${esc(run.data.model?.id || run.data.model?.type || '')}</p>
          ${(run.sources || []).map(src => `<details><summary>${esc(src.title)} · ${src.is_example ? '仅作参考' : '待核对'}${src.truncated ? ' · 部分截断' : ''}${src.source_changed ? ' · 当前原文已变化' : ''}</summary><pre style="white-space:pre-wrap">${esc(src.snippet || '')}</pre></details>`).join('')}
          <details><summary>本次实际输入</summary><pre style="white-space:pre-wrap">${esc(JSON.stringify(run.data.messages?.length ? run.data.messages : run.data.variables, null, 2))}</pre></details>
        </details>`).join('')}`;
      $('#evidenceModal').hidden = false;
    } catch (e) { toast(e.message); }
  },
  closeEvidence() { $('#evidenceModal').hidden = true; },
  async reqSupport(el) {
    try { await loadReqSupport(el.dataset.rid); } catch (e) { toast(e.message); }
  },
  async reqSupportCandidates() {
    try {
      const r = await api(`/api/projects/${S.pid}/reqmatrix/support-candidates`, 'POST', {});
      await loadReqMatrix(); toast(`新增 ${r.created} 条候选关联，请逐条核对`);
    } catch (e) { toast(e.message); }
  },
  async reqSupportPolicy(el) {
    try {
      const card = el.closest('[data-support-rid]');
      const body = {};
      card.querySelectorAll('[data-field]').forEach(input => {
        if (['target','relation','note'].includes(input.dataset.field)) return;
        body[input.dataset.field] = input.type === 'checkbox' ? input.checked : input.value;
      });
      await api(`/api/projects/${S.pid}/reqmatrix/${encodeURIComponent(el.dataset.rid)}/support-policy`, 'PUT', body);
      await loadReqMatrix();
      await loadReqSupport(el.dataset.rid);
      toast('本条支撑类型已核对');
    } catch (e) { toast(e.message); }
  },
  async reqSupportAdd(el) {
    try {
      const card = el.closest('[data-support-rid]');
      const chosen = card.querySelector('[data-field="target"]').value;
      if (!chosen) { toast('请先选择材料'); return; }
      const [target_type, target_id] = chosen.split(':');
      await api(`/api/projects/${S.pid}/reqmatrix/${encodeURIComponent(el.dataset.rid)}/support`, 'POST', {
        target_type, target_id, relation: card.querySelector('[data-field="relation"]').value,
        note: card.querySelector('[data-field="note"]').value
      });
      await loadReqMatrix(); await loadReqSupport(el.dataset.rid, true);
    } catch (e) { toast(e.message); }
  },
  async reqSupportDelete(el) {
    try {
      await api(`/api/projects/${S.pid}/reqmatrix/${encodeURIComponent(el.dataset.rid)}/support/${encodeURIComponent(el.dataset.lid)}`, 'DELETE');
      await loadReqMatrix(); await loadReqSupport(el.dataset.rid, true);
    } catch (e) { toast(e.message); }
  },
  async runCap(key, el) {
    S.coreKey = key;
    const v = ((($('#debugPayload') || {}).value || ($('#corePayload') || {}).value) || '').trim();
    S.corePayload = v;
    const payload = v ? { query: v, text: v, skill: v, before: v, after: v } : {};
    if (/^\d+$/.test(v)) payload.pages = Number(v);
    busy(el, true, '运行中…');
    try {
      S.coreResult = await api('/api/core/run/' + key, 'POST', { pid: S.pid, payload });
      renderTab(S.activeTab);
      const out = $('#coreResult'); if (out) out.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    } catch (e) { toast(e.message); }
    finally { busy(el, false); }
  },
  async debugRun(el) {
    const key = ($('#debugCap') || {}).value || '';
    if (!key) return toast('先选一项能力');
    return ACT.runCap(key, el);
  },
  async settingsSave(el) {
    const d = S.settingsDraft || settingsDraft();
    const stages = { ...d.stages }; delete stages.export;  // 导出门禁由下面的独立卡片管
    const patch = {
      auto_enabled: d.auto_enabled,
      export_quality: d.export_quality,
      allow_model_caps_auto: d.allow_model_caps_auto,
      disabled_caps: d.disabled_caps,
      brain_auto_allow: d._mode === 'whitelist' ? d._allowList : '*',
      stages,
      export_caps: d.export_caps,
    };
    busy(el, true, '保存中…');
    try {
      S.settings = await api('/api/settings', 'POST', patch);
      S.settingsDraft = null;
      S.skipDraft = true;             // 保存后按新配置重画，不把旧勾选写回来
      await loadAutoStatus();
      const st = await api('/api/state'); S.brain = st.brain || S.brain;
      renderHeader(); renderSettings();
      toast('设置已保存，立即生效');
    } catch (e) { toast(e.message); }
    finally { busy(el, false); }
  },
  settingsReset() {
    S.settingsDraft = null; S.skipDraft = true;
    renderSettings(); toast('已放弃未保存的修改');
  },
  openModelsForSettings() { return openModels(); },
  async runStage(el) {
    busy(el, true, '运行中…');
    try {
      const r = await api('/api/core/auto/stage/' + el.dataset.stage, 'POST', { pid: S.pid, force: true });
      await loadAutoStatus(); renderTab(S.activeTab);
      const card = r.card || {};
      toast(card.count ? `已重跑 ${card.count} 项能力，发现 ${card.findings} 条线索` : '自动编排已关闭或该阶段没有启用能力');
    } catch (e) { toast(e.message); }
    finally { busy(el, false); }
  },
  send() {
    if (CHAT.uploading) return toast('文件还在解析，解析成功后「发送」会开放；可以先在下面把话写好');
    if (CHAT.sending) return toast('上一句还在执行，内容还在输入框，等它跑完再发');
    const ta = $('#chatInput'); const t = ta ? ta.value.trim() : '';
    if (!t && !CHAT.pending) return toast('请输入内容');
    if (!t && CHAT.pending) toast('没写说明，会按文件内容自动判断用途；写一句会更准');
    if (ta) ta.value = '';
    sendChat(t, '');
  },
  hideGuide() { CHAT.guideHidden = true; saveUI({ guideHidden: true }); renderChat(); },
  attach() {
    S.uploadTarget = '';
    // 已有待发送附件先留着：即使这次取消选文件，也不该把上一个附件弄丢
    $('#fileInput').click();
  },
  uploadTo(el) { S.uploadTarget = el.dataset.target; $('#fileInput').click(); },
  pickTarget(el) {
    // 只设定用途，不发送、不入库；附件仍然要跟着用户的问题一起走
    if (!CHAT.pending) return;
    const t = el.dataset.target;
    if (t === 'auto') delete CHAT.pending.target;
    else CHAT.pending.target = t;
    CHAT.uploadHint = null;
    CHAT.attachNote = '';
    renderChat();
    toast(t === 'auto' ? '已恢复自动判断，点「发送」时和问题一起提交'
      : `已指定为「${ATTACH_LABEL[t] || t}」，点「发送」时和问题一起提交`);
  },
  cancelAttach() { CHAT.pending = null; CHAT.uploadHint = null; CHAT.attachNote = ''; renderChat(); },
  async download(el) {
    if (!(await secFlush())) return toast('正文还没保存成功，先别下载，避免拿到旧内容');
    return fetchExport(el.dataset.url);
  },
  async export() {
    if (!(await secFlush())) return toast('正文还没保存成功，先别下载，避免拿到旧内容');
    if (!(await confirmEmptyBody())) return;
    return fetchExport(`/api/projects/${S.pid}/export`);
  },
  async exportWord() {
    if (!(await secFlush())) return toast('正文还没保存成功，先别下载，避免拿到旧内容');
    if (!(await confirmEmptyBody())) return;
    const blind = ($('#blindExport') || {}).checked ? '?blind=true' : '';
    return fetchExport(`/api/projects/${S.pid}/export.docx${blind}`);
  },
  closeExportGate() { $('#exportModal').hidden = true; S.exportGate = null; S.exportUrl = ''; },
  goFixChapter() {
    $('#exportModal').hidden = true; S.exportGate = null; S.exportUrl = '';
    switchTab('write');
    toast('改好正文后再点下载，系统会重新检查');
  },

  async reqSync(el) {
    busy(el, true, '重抽中…');
    try {
      const r = await api(`/api/projects/${S.pid}/requirements/sync`, 'POST', {});
      await afterReqChange(`已重抽，新增 ${(r.added || {}).hard || 0} 条硬性要求等；人工修订的行未动`);
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async reqRestore(el) {
    busy(el, true, '恢复中…');
    try {
      await api(`/api/projects/${S.pid}/requirements/sync`, 'POST', { restore: true });
      await afterReqChange('已找回此前删除的自动要求；人工修订行保持不变');
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  reqAddIn(el) {
    const kind = el.dataset.kind || 'hard';
    const sel = $('#reqNewKind'); if (sel) sel.value = kind;
    const box = $('#reqNewText');
    if (box) { box.scrollIntoView({ block: 'center' }); box.focus(); }
    const label = ((S.reqTable || {}).labels || {})[kind] || kind;
    toast(`已选好「${label}」，填上要求内容后点「加入要求表」`);
  },
  async reqAdd(el) {
    const text = ($('#reqNewText') || {}).value || '';
    if (!text.trim()) return toast('请填写要求内容');
    busy(el, true);
    try {
      await api(`/api/projects/${S.pid}/requirements`, 'POST', {
        kind: ($('#reqNewKind') || {}).value || 'hard', text: text.trim(),
        score: ($('#reqNewScore') || {}).value || '' });
      await afterReqChange('已加入要求表，后续写作与核查立即按修订版执行');
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async reqSave(el) {
    const rid = el.dataset.rid;
    const pick = (role) => ($(`[data-role="${role}"][data-rid="${rid}"]`) || {}).value || '';
    busy(el, true);
    try {
      await api(`/api/projects/${S.pid}/requirements/${rid}`, 'PUT',
                { text: pick('reqText').trim(), score: pick('reqScore').trim() });
      await afterReqChange('已保存，本行标为人工修订，重抽不会覆盖');
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async reqToggle(el) {
    busy(el, true);
    try {
      await api(`/api/projects/${S.pid}/requirements/${el.dataset.rid}`, 'PUT',
                { status: el.dataset.status });
      await afterReqChange(el.dataset.status === 'disabled' ? '已停用，不再参与判定' : '已启用');
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async reqDel(el) {
    if (!await uiConfirm('确定删除这一条要求吗？\n\n只删这一条（小项），同一组里的其他要求不受影响。'
                 + '删过的自动要求可用「恢复被删要求」找回。')) return;
    busy(el, true, '删除中…');
    try {
      await api(`/api/projects/${S.pid}/requirements/${encodeURIComponent(el.dataset.rid)}`, 'DELETE');
      await afterReqChange('已删除这一条要求');
    } catch (e) { toast('删除失败：' + e.message); } finally { busy(el, false); }
  },
  async reqGroupDel(el) {
    const kind = el.dataset.kind || '';
    const label = ((S.reqTable || {}).labels || {})[kind] || kind;
    const n = (((S.reqTable || {}).by_kind || {})[kind] || {}).rows || 0;
    if (!await uiConfirm(`确定删除大项「${label}」吗？\n\n这一组下面的 ${n} 条要求会一起删掉。`
                 + '需要找回时点「恢复被删要求」。')) return;
    busy(el, true, '删除中…');
    try {
      const r = await api(`/api/projects/${S.pid}/requirements/group/${encodeURIComponent(kind)}`, 'DELETE');
      await afterReqChange(`已删除大项「${label}」，共 ${r.deleted || 0} 条`);
    } catch (e) { toast('删除失败：' + e.message); } finally { busy(el, false); }
  },
  confirmExportGate(el) { return confirmExportGate(el); },

  async interpret() { return askBrain('重新解读招标文件'); },
  async saveTender(el) {
    busy(el, true, '保存中…');
    try { await api(`/api/projects/${S.pid}/tender`, 'POST', { text: $('#tenderText').value }); await refresh(); toast('招标文件已保存'); }
    catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async addMaterial(el) {
    const title = ($('#matTitle') || {}).value ? $('#matTitle').value.trim() : '';
    const content = ($('#matContent') || {}).value ? $('#matContent').value.trim() : '';
    if (!content) return toast('请填写资料内容');
    busy(el, true);
    try {
      await api(`/api/projects/${S.pid}/materials`, 'POST', { title: title || '未命名资料', content });
      S.matNew = false;
      await refresh(); toast('已加入本项目资料');
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  matNewToggle() { S.matNew = !S.matNew; renderMaterials().catch(() => {}); },
  docView(el) {
    S.docOpen = S.docOpen === el.dataset.did ? '' : el.dataset.did;
    renderMaterials().catch(() => {});
  },
  async delMaterial(el) { await api('/api/docs/' + el.dataset.did, 'DELETE'); await refresh(); toast('已删除'); },
  async delKb(el) { await api('/api/docs/' + el.dataset.did, 'DELETE'); await refresh(); toast('已删除'); },

  /* ---- R05：解析待核对 ---- */
  /* ---- B01/B02/B03/B04：业务完整性操作 ---- */
  async reqMatrixConfirm(el) {
    await api('/api/projects/' + S.pid + '/reqmatrix/' + encodeURIComponent(el.dataset.rid) + '/confirm', 'POST', { reviewer: '人工复核' });
    toast('已人工确认这条要求'); await loadReqMatrix();
  },
  async reqMatrixNa(el) {
    const reason = await uiPrompt('为什么这条要求不适用？请写明理由（会记录责任人与时间）');
    if (!reason || !reason.trim()) return toast('标不适用必须写理由');
    await api('/api/projects/' + S.pid + '/reqmatrix/' + encodeURIComponent(el.dataset.rid) + '/na', 'POST', { reason });
    toast('已标不适用并记录理由'); await loadReqMatrix();
  },
  async tenderVersionAdd(el) {
    const text = (($('#tvText') || {}).value || '').trim();
    if (!text) return toast('先粘贴这一版的正文');
    busy(el, true, '保存中…');
    try {
      const r = await api('/api/projects/' + S.pid + '/tender-versions', 'POST', {
        kind: ($('#tvKind') || {}).value || 'amend',
        text, filename: ($('#tvName') || {}).value || '',
      });
      toast(`已存为第 ${r.version.version} 版；失效要求 ${r.retired_requirements} 条、待重新复核 ${r.stale_confirmations} 条`);
      await refresh(); await loadTenderVersions();
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async showAssetGap(el) {
    const box = $('#assetGapBox'); if (!box) return;
    if (box.dataset.loaded === '1') { box.hidden = !box.hidden; return; }
    busy(el, true, '读取中…');
    try {
      const r = await api('/api/projects/' + S.pid + '/asset-gap');
      box.innerHTML = assetGapHtml(r);
      box.dataset.loaded = '1'; box.hidden = false;
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  assetEdit(el) { S.assetEdit = el.dataset.aid || ''; loadAssets(); },
  assetEditCancel() { S.assetEdit = ''; loadAssets(); },
  async assetAdd(el) {
    const p = assetPayload();
    if (!p.title) return toast('先填资产名称');
    busy(el, true, '登记中…');
    try {
      await api('/api/projects/' + S.pid + '/assets', 'POST', p);
      toast('资产已登记（默认未核验，请对照原件确认）'); await loadAssets();
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async assetSave(el) {
    const p = assetPayload(), aid = el.dataset.aid || '';
    if (!aid || !p.title) return toast('先填资产名称');
    busy(el, true, '保存中…');
    try {
      await api('/api/projects/' + S.pid + '/assets/' + encodeURIComponent(aid), 'PUT', p);
      S.assetEdit = ''; toast('资产已更新'); await loadAssets();
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async assetDel(el) {
    const aid = el.dataset.aid || '';
    if (!aid || !await uiConfirm('删除这条企业资产？所有项目都看不到它了，确定？')) return;
    try {
      await api('/api/projects/' + S.pid + '/assets/' + encodeURIComponent(aid), 'DELETE');
      if (S.assetEdit === aid) S.assetEdit = '';
      toast('资产已删除'); await loadAssets();
    } catch (e) { toast(e.message); }
  },
  async assetVerify(el) {
    try {
      await api('/api/projects/' + S.pid + '/assets/'
        + encodeURIComponent(el.dataset.aid || '') + '/verify', 'POST', {});
      toast('已标记对照原件核验'); await loadAssets();
    } catch (e) { toast(e.message); }
  },
  async factAdd(el) {
    const value = (($('#factValue') || {}).value || '').trim();
    if (!value) return toast('先填数值');
    await api('/api/projects/' + S.pid + '/facts', 'POST', {
      kind: ($('#factKind') || {}).value || 'enterprise_fact',
      entity: ($('#factEntity') || {}).value || '', field: ($('#factField') || {}).value || '',
      value, unit: ($('#factUnit') || {}).value || '',
      valid_until: ($('#factUntil') || {}).value || '', source_loc: ($('#factLoc') || {}).value || '',
    });
    toast('事实已登记（默认未核验，需对照原件确认）'); await loadFacts();
  },
  async factVerify(el) {
    await api('/api/projects/' + S.pid + '/facts/' + encodeURIComponent(el.dataset.fid) + '/verify', 'POST', {});
    toast('已核验，该事实现在可作为数值依据'); await loadFacts();
  },
  async attachUpload(el) {
    const file = (($('#attFile') || {}).files || [])[0];
    if (!file) return toast('先选择要上传的原件文件');
    const note = (($('#attNote') || {}).value || '').trim();
    busy(el, true, '上传中…');
    try {
      const fd = new FormData(); fd.append('file', file);
      const q = '?note=' + encodeURIComponent(note);
      const r = await fetch('/api/projects/' + S.pid + '/attachments' + q, { method: 'POST', body: fd });
      const j = await r.json();
      if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : '上传失败');
      toast('原件已留存并登记'); await loadFacts();
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  openAttachment(el) {
    location.href = '/api/projects/' + S.pid + '/attachments/' + encodeURIComponent(el.dataset.aid) + '/open';
  },
  async formConfirm(el) {
    await api('/api/projects/' + S.pid + '/forms/confirm', 'POST', {
      section_id: el.dataset.sid, target: el.dataset.target, reviewer: '人工复核',
    });
    toast('已记录人工确认（绑定当前正文版本，改动后需重新确认）'); await loadForms();
  },
  dismissUploadFail() { CHAT.uploadFail = null; renderChat(); },
  downloadArtifact(el) { if (el.dataset.aid) location.href = '/api/projects/' + S.pid + '/artifacts/' + encodeURIComponent(el.dataset.aid) + '/download'; },

  /* ---- C01：暗标规则 / 分册确认 / 真实投标包 ---- */
  async bundleSaveRules(el) {
    const split = (id) => (($('#' + id) || {}).value || '')
      .split(/[、,，;；\n]/).map(x => x.trim()).filter(Boolean);
    busy(el, true, '保存中…');
    try {
      const r = await api('/api/projects/' + S.pid + '/bundle/rules', 'POST', {
        enterprise_full: (($('#blFull') || {}).value || '').trim(),
        enterprise_short: split('blShort'), persons: split('blPersons'),
        contacts: split('blContacts'), addresses: split('blAddr'),
      });
      S.bundleRules = r.rules; S.bundleScan = null; renderBundle();
      toast(r.rules.configured ? '规则已登记，扫描按本项目身份词全包核对'
        : '规则已保存；没有身份词典仍不算已配置，不能宣称完全暗标合规');
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async bundleScan(el) {
    busy(el, true, '扫描中…');
    try {
      const r = await api('/api/projects/' + S.pid + '/bundle/scan', 'POST', {});
      S.bundleScan = r.scan; renderBundle(); toast(r.scan.summary);
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async bundleExport(el) {
    if (!S.bundleConfirmed) return toast('先逐册核对并勾选确认，规划不算确认');
    const vols = bundleCollect();
    if (!vols.length) return toast('没有可分册的章节');
    S.bundleVolumes = vols;
    busy(el, true, '检查中…');
    try {
      const g = await api('/api/projects/' + S.pid + '/export/gate');
      // 一律带知情令牌：分册最终 Word 检查没过也只作为提醒，不把投标包挡住
      const c = await api('/api/projects/' + S.pid + '/export/confirm', 'POST',
        { fingerprint: g.fingerprint, acknowledge: true });
      const token = c.token;
      if ((g.issues || []).length)
        toast(`检查有 ${g.issues.length} 项提醒（高风险 ${g.high} 项），按你的要求继续生成投标包`);
      S.bundleGate = null; renderBundle();
      await bundleDownload(token);
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async bundleGateConfirm(el) {
    if (!S.bundleGate) return;
    busy(el, true, '导出中…');
    try {
      const r = await api('/api/projects/' + S.pid + '/export/confirm', 'POST',
        { fingerprint: S.bundleGate.fingerprint, acknowledge: true });
      S.bundleGate = null;
      await bundleDownload(r.token);
      renderBundle();
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },

  /* ---- C02：项目团队与分工 ---- */
  async teamAdd(el) {
    const uid = (($('#teamNewUser') || {}).value || '').trim();
    if (!uid) return toast('先选一个本企业成员');
    busy(el, true, '加入中…');
    try {
      renderTeam(await api('/api/projects/' + S.pid + '/team', 'POST',
        { user_id: uid, role: ($('#teamNewRole') || {}).value || 'reader' }));
      toast('已加入项目；此后本项目按成员名册授权');
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async teamRemove(el) {
    if (!await uiConfirm('把这位成员移出项目？移除后他将不能再访问本项目。')) return;
    try {
      renderTeam(await api('/api/projects/' + S.pid + '/team/' + encodeURIComponent(el.dataset.uid), 'DELETE'));
      toast('已移除');
    } catch (e) { toast(e.message); }
  },
  async teamMeta(el) {
    busy(el, true, '保存中…');
    try {
      renderTeam(await api('/api/projects/' + S.pid + '/team/meta', 'PUT', {
        owner_id: (($('#teamOwner') || {}).value || ''),
        due_at: (($('#teamDue') || {}).value || ''),
        final_version: (($('#teamFinal') || {}).value || ''),
      }));
      toast('项目信息已保存');
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async teamSections(el) {
    busy(el, true, '保存中…');
    try {
      renderTeam(await api('/api/projects/' + S.pid + '/team/sections', 'PUT',
        { items: teamSectionItems() }));
      toast('章节分工与复核状态已保存');
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },

  /* ---- C03：长任务取消 / 检查点 / 重试恢复 ---- */
  async jobCancel(el) {
    const jid = (S.composeJob || {}).jid;
    if (!jid) return toast('当前没有在跑的任务');
    if (!await uiConfirm('取消整本生成？取消后未处理的章节会留在检查点里，可稍后恢复。')) return;
    busy(el, true, '取消中…');
    try {
      const r = await api(`/api/projects/${S.pid}/jobs/${jid}/cancel`, 'POST', {});
      toast(r.ok ? r.note : '任务已经结束，没有可取消的执行');
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async jobCheckpoints(el) {
    const jid = el.dataset.jid;
    if (!jid) return toast('先跑一次整本生成');
    const box = $('#taskCtlBox'); if (box) box.innerHTML = '<div class="hint">加载中…</div>';
    try {
      renderTaskCtl(await api(`/api/projects/${S.pid}/jobs/${jid}/checkpoints`));
    } catch (e) { toast(e.message); }
  },
  async jobRetry(el) { return startTaskJob(el, 'retry'); },
  async jobResume(el) { return startTaskJob(el, 'resume'); },
  async keepOriginal(el) {
    const f = CHAT.uploadFail && CHAT.uploadFail.file;
    if (!f) return toast('文件已不在，请重新选择后点这个按钮');
    if (!S.pid) return toast('先进入一个项目');
    busy(el, true, '留存中…');
    try {
      const fd = new FormData(); fd.append('file', f);
      const r = await fetch('/api/projects/' + S.pid + '/originals', { method: 'POST', body: fd });
      const j = await r.json();
      if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : (j.detail?.msg || '留存失败'));
      CHAT.uploadFail = null; renderChat();
      toast(j.note || '已按待处理留存原件');
      if (S.activeTab === 'materials') await renderMaterials();
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async confirmDoc(el) {
    const did = el.dataset.did;
    const note = ($('#reviewNote-' + did) || {}).value || '';
    try {
      await api('/api/docs/' + encodeURIComponent(did) + '/confirm?pid=' + encodeURIComponent(S.pid), 'POST', { note });
      toast('已人工确认，该资料现在可作为依据'); await renderMaterials();
    } catch (e) { toast(e.message); }
  },
  async addPageText(el) {
    const did = el.dataset.did;
    const page = Number(($('#pageNo-' + did) || {}).value || 0);
    const text = (($('#pageText-' + did) || {}).value || '').trim();
    if (!page || !text) return toast('填页码和该页正文再补录');
    try {
      const r = await api('/api/docs/' + encodeURIComponent(did) + '/pages?pid=' + encodeURIComponent(S.pid), 'POST', { page, text });
      toast(r.needs_review ? '已补录本页，还有其它页待补' : '全部未识别页已补录，待核对解除');
      await renderMaterials();
    } catch (e) { toast(e.message); }
  },
  async confirmTender() {
    try {
      await api('/api/projects/' + S.pid + '/tender/confirm', 'POST', {});
      toast('招标文件解析已人工确认'); await renderMaterials();
    } catch (e) { toast(e.message); }
  },

  /* ---- 知识库 ---- */
  kbFilter(el) {
    S.kbFilter = el.dataset.kind || 'all';
    S.kbEdit = ''; S.kbOpen = '';
    kbListRefresh();
  },
  kbView(el) { S.kbEdit = ''; S.kbOpen = S.kbOpen === el.dataset.did ? '' : el.dataset.did; kbListRefresh(); },
  kbEdit(el) { S.kbOpen = ''; S.kbEdit = S.kbEdit === el.dataset.did ? '' : el.dataset.did; kbListRefresh(); },
  kbNewToggle() { S.kbNew = !S.kbNew; S.kbOpen = ''; S.kbEdit = ''; renderKbPage(); },
  kbUpload() { S.uploadTarget = 'kb'; $('#fileInput').click(); },
  async kbAdd() {
    const title = (($('#kbNewTitle') || {}).value || '').trim();
    const content = ($('#kbNewContent') || {}).value || '';
    const kind = ($('#kbNewKind') || {}).value || 'material';
    const validUntil = ($('#kbNewValidUntil') || {}).value || '';
    if (!content.trim()) return toast('正文还是空的');
    await api('/api/kb', 'POST', { title: title || '未命名素材', content, kind,
      valid_until: validUntil });
    S.kbNew = false;
    await loadKb(); await afterKbChange();
    toast(`已存入知识库 · ${KB_LABEL[kind] || '通用素材'}`);
  },
  async kbSave(el) {
    const title = (($('#kbETitle') || {}).value || '').trim();
    const content = ($('#kbEContent') || {}).value || '';
    const kind = ($('#kbEKind') || {}).value || 'material';
    const validUntil = ($('#kbEValidUntil') || {}).value || '';
    if (!content.trim()) return toast('正文还是空的');
    await api('/api/kb/' + encodeURIComponent(el.dataset.did), 'PUT',
      { title: title || '未命名素材', content, kind, valid_until: validUntil });
    S.kbEdit = '';
    await loadKb(); await afterKbChange();
    toast('已保存');
  },
  async kbDel(el) {
    const d = (S.kbList || []).find(x => x.id === el.dataset.did) || {};
    if (!await uiConfirm(`删除知识库里的「${d.title || '这一篇'}」？删了就找不回来了。`)) return;
    await api('/api/kb/' + encodeURIComponent(el.dataset.did), 'DELETE');
    if (S.kbEdit === el.dataset.did) S.kbEdit = '';
    if (S.kbOpen === el.dataset.did) S.kbOpen = '';
    if (S.kbChapOpen === el.dataset.did) S.kbChapOpen = '';
    await loadKb(); await afterKbChange();
    toast('已删除');
  },
  async kbChapters(el) {
    const did = el.dataset.did;
    if (S.kbChapOpen === did) { S.kbChapOpen = ''; return kbListRefresh(); }
    S.kbChapOpen = did; S.kbOpen = ''; S.kbEdit = '';
    kbListRefresh();
    if (!(S.kbChap[did] || []).length) {
      try {
        const r = await api(`/api/kb/${encodeURIComponent(did)}/chapters`);
        S.kbChap[did] = r.chapters || [];
      } catch (e) { toast(e.message); }
      kbListRefresh();
    }
  },
  async kbUseOutline(el) {
    if (!S.pid) return toast('先选一个项目');
    const did = el.dataset.did;
    const d = (S.kbList || []).find(x => x.id === did) || {};
    const have = (S.project && (S.project.sections || []).some(s => (s.text || '').trim()));
    if (have && !await uiConfirm(`用「${d.title || '这本范例'}」的目录重建章节？同名章节的正文会保留，其它可在「版本与回退」撤回。`)) return;
    const r = await api(`/api/projects/${S.pid}/outline/from-sample`, 'POST', { doc_id: did });
    await refresh();
    toast(`已按范例建成 ${r.count} 章，并把这本书设为参照`);
  },
  async kbSetSample(el) {
    if (!S.pid) return toast('先选一个项目');
    const did = el.dataset.did;
    const isRef = S.project && did === (S.project.sample_doc_id || '');
    await api(`/api/projects/${S.pid}/sample`, 'POST', { doc_id: isRef ? '' : did });
    S.project = await api('/api/projects/' + S.pid);
    S.skipDraft = true;
    if (S.activeTab === 'write') renderWrite(); else kbListRefresh();
    toast(isRef ? '已取消参照' : '已设为参照范例，写正文时优先学这本书');
  },
  async emptyUseSample() {
    const sel = $('#emptySamplePick');
    if (!sel || !sel.value) return toast('先选一本范例');
    const r = await api(`/api/projects/${S.pid}/outline/from-sample`, 'POST', { doc_id: sel.value });
    await refresh();
    toast(`已按范例建成 ${r.count} 章，并把这本书设为参照`);
  },
  async clearSample() {
    if (!S.pid) return;
    await api(`/api/projects/${S.pid}/sample`, 'POST', { doc_id: '' });
    S.project = await api('/api/projects/' + S.pid);
    S.skipDraft = true; renderWrite();
    toast('已取消参照');
  },
  async retrieve(el) {
    const q = $('#retrieveQ').value.trim(); if (!q) return toast('请输入检索内容');
    const r = await api(`/api/projects/${S.pid}/retrieve`, 'POST', { query: q });
    $('#retrieveResult').innerHTML = r.hits.length
      ? r.hits.map(h => `<div class="doc" style="margin-bottom:8px"><div class="dt">${esc(h.title)}<span class="chip blue">${esc(h.scope)}</span></div><div class="dc">${esc(h.snippet)}</div></div>`).join('')
      : '<div class="empty">未命中资料</div>';
  },

  /* ---- 标书正文：选章 / 逐章写 / 整本写 ---- */
  async secPick(el) {
    const sid = el.dataset.sid;
    if (sid === S.secId) return;
    if (!(await secFlush())) return toast('这一章还没保存成功，先处理保存或冲突提示');
    S.secId = sid; S.secHint = '';
    saveUI({ ['sec:' + S.pid]: sid });
    S.skipDraft = true; renderWrite();
    const ed = $('.ed-text');
    if (ed) ed.focus();
  },
  async secStep(el) {
    const list = secList();
    const i = list.findIndex(s => s.id === S.secId) + Number(el.dataset.dir || 0);
    if (i < 0 || i >= list.length) return;
    if (!(await secFlush())) return toast('这一章还没保存成功，先处理保存或冲突提示');
    S.secId = list[i].id; S.secHint = '';
    saveUI({ ['sec:' + S.pid]: S.secId });
    S.skipDraft = true; renderWrite();
  },
  async gotoSec(el) {
    if (S.activeTab === 'write' && !(await secFlush())) {
      return toast('这一章还没保存成功，先处理保存或冲突提示');
    }
    S.secId = el.dataset.sid; S.secHint = '';
    saveUI({ ['sec:' + S.pid]: S.secId });
    switchTab('write');
    S.skipDraft = true; renderWrite();
    const ed = $('.ed-text');
    if (ed) ed.focus();
  },
  async conflictMine() {
    const c = S.secConflict;
    if (!c) return;
    S.secConflict = null; showConflictBar(false);
    const ok = await saveSecNow(true);      // 强制写：不带 base_ver
    if (ok) toast('已用你的版本覆盖，这一章重新成为最新');
    else if (!S.secConflict) toast('已覆盖旧版本；编辑器里还有新改动，稍后会再存一次');
  },
  async conflictTheirs() {
    const c = S.secConflict;
    if (!c) return;
    S.secConflict = null;
    const sec = secList().find(x => x.id === c.sid);
    if (sec) { sec.text = c.serverText; sec.ver = c.ver; }
    S.secDirty = false;
    S.skipDraft = true; renderWrite();
    toast('已换成服务端版本；你刚才那版没有保留');
  },
  async candidateApply() {
    const c = S.secCandidate;
    if (!c || !c.text.trim()) return toast('这份 AI 稿是空的，无法覆盖');
    const ta = $('[data-role="secText"]');
    if (!ta || ta.dataset.sid !== c.sid) return toast('请先切回对应章节再覆盖');
    ta.value = c.text;
    S.secDirty = true;
    const ok = await saveSecNow(true);   // 用户看过稿并明确同意，才强写
    if (ok) {
      S.secCandidate = null;
      await refresh();
      toast('已用这份 AI 稿覆盖当前正文');
    } else toast('覆盖没有完成，请看保存状态或冲突提示');
  },
  async candidateDiscard() {
    S.secCandidate = null;
    S.skipDraft = true;
    renderWrite();
    toast('AI 稿已丢弃，正文保持原样');
  },
  /* ---- 手动调整目录：加章 / 改名 / 删章（不重建整份目录） ---- */
  async secAdd(el) {
    if (!(await secFlush())) return toast('正文还没保存成功，先处理保存提示');
    const title = (await uiPrompt('新章节名称（例：第三章 项目实施组织）') || '').trim();
    if (!title) return;
    const cur = secCurrent();
    try {
      const r = await api('/api/projects/' + S.pid + '/sections', 'POST',
        { title, after: cur ? cur.id : '' });
      S.skipDraft = true;
      await refresh();
      S.secId = r.id; S.skipDraft = true; renderWrite();
      toast('已加一章：' + r.title);
    } catch (e) { toast(e.message); }
  },
  async secRename(el) {
    const sid = el.dataset.sid || '';
    const s = secList().find(x => x.id === sid);
    if (!s) return;
    if (!(await secFlush())) return toast('正文还没保存成功，先处理保存提示');
    const title = (await uiPrompt('把这一章改名为：', s.title) || '').trim();
    if (!title || title === s.title) return;
    try {
      await api('/api/projects/' + S.pid + '/sections/' + encodeURIComponent(sid),
        'PUT', { title });
      S.skipDraft = true;
      await refresh();
      S.secId = sid; S.skipDraft = true; renderWrite();
      toast('已改名为：' + title);
    } catch (e) { toast(e.message); }
  },
  async secDelete(el) {
    const sid = el.dataset.sid || '';
    const list = secList();
    const i = list.findIndex(x => x.id === sid);
    if (i < 0) return;
    const s = list[i];
    const chars = (s.text || '').length;
    if (!await uiConfirm(`删除「${s.title}」${chars ? `及其 ${chars} 字正文` : ''}？\n删除前会自动留一份目录快照，可在「版本与回退」整份撤销。`)) return;
    if (!(await secFlush())) return toast('正文还没保存成功，先处理保存提示');
    const next = (list[i + 1] || list[i - 1] || {}).id || '';
    try {
      const r = await api('/api/projects/' + S.pid + '/sections/' + encodeURIComponent(sid), 'DELETE');
      S.skipDraft = true;
      await refresh();
      S.secId = next; S.skipDraft = true; renderWrite();
      toast(r.note || '章节已删除');
    } catch (e) { toast(e.message); }
  },
  /* 生成结果常驻卡上的三个动作：筛选、定位、收起。重渲前先把没保存的正文落库。 */
  async toggleOnlyEmpty() {
    if (!(await secFlush())) return toast('正文还没保存成功，先处理保存提示');
    S.onlyEmpty = !S.onlyEmpty;
    S.skipDraft = true; renderWrite();
  },
  async gotoNextEmpty() {
    if (!(await secFlush())) return toast('正文还没保存成功，先处理保存提示');
    const next = secList().find(s => !(s.text || '').trim() && !s.protected);
    if (!next) return toast('没有待写的自动章节了；保护章节需要手工填写');
    S.secId = next.id;
    S.skipDraft = true; renderWrite();
    toast('已定位到：' + next.title);
  },
  async dismissComposeResult() {
    if (!(await secFlush())) return toast('正文还没保存成功，先处理保存提示');
    S.composeResult = null; S.onlyEmpty = false;
    S.skipDraft = true; renderWrite();
  },

  /* 目录体检里逐条补一章：评分项是否单独成章是业务判断，必须用户点一次才算数。 */
  async addScoringChapter(el) {
    const name = (el.dataset.title || '').trim().slice(0, 60);
    if (!name) return;
    const title = '评分响应：' + name;
    if (!await uiConfirm(`会新增一章「${title}」，目录确认会失效、需要重新确认。\n` +
                         '补好后请在这一章里逐条落实该评分项。继续？')) return;
    if (!(await secFlush())) return toast('正文还没保存成功，先处理保存提示');
    try {
      const r = await api('/api/projects/' + S.pid + '/sections', 'POST', { title, after: '' });
      S.skipDraft = true;
      await refresh();
      S.secId = r.id; S.skipDraft = true; renderWrite();
      toast('已补一章：' + r.title + '；记得重新确认目录');
    } catch (e) { toast(e.message); }
  },
  async outlineConfirm(el) {
    // 有漏匹配的评分项时先让人看一眼，但绝不拦死：确认后仍可继续写正文。
    const missed = ((S.outlinePlan || {}).unmatched_scoring || [])
      .filter(x => typeof x === 'string' && x.trim());
    if (missed.length && !await uiConfirm(
      `目录里还有 ${missed.length} 个评分项没对上任何章节标题（例如「${missed[0]}」）。\n` +
      '可以先回「目录体检」逐条补章，也可以确认它们已并入相关章节。\n仍要确认目录吗？')) return;
    busy(el, true, '确认中…');
    try {
      const r = await api(`/api/projects/${S.pid}/outline/confirm`, 'POST', {});
      S.skipDraft = true;
      await refresh();
      toast(`目录已确认：${r.chapters} 章；改动章节后需重新确认`);
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async secMove(el) {
    const sid = el.dataset.sid || '';
    const dir = Number(el.dataset.dir || 0);
    if (!sid || !dir) return;
    if (!(await secFlush())) return toast('正文还没保存成功，先处理保存提示');
    try {
      await api(`/api/projects/${S.pid}/sections/${encodeURIComponent(sid)}/move`,
        'POST', { direction: dir });
      S.skipDraft = true;
      await refresh();
      S.secId = sid; S.skipDraft = true; renderWrite();
    } catch (e) { toast(e.message); }
  },
  async outline(el) {
    if (!((S.project.tender_text || '').trim())) {
      toast('还没有招标文件，先去「资料」导入');
      return switchTab('materials');
    }
    if (secList().length && !await uiConfirm('重新生成大纲会刷新章节结构；同名章节的正文会保留。继续？')) return;
    busy(el, true, '生成中…');
    try {
      const r = await api(`/api/projects/${S.pid}/outline`, 'POST', {});
      S.outlinePlan = r.plan || {};
      const missed = ((r.plan || {}).unmatched_scoring || []).length;
      S.skipDraft = true;
      await refresh();
      S.secId = ''; S.skipDraft = true; renderWrite();
      toast(`目录已生成：${(r.chapters || []).length} 章`
        + (missed ? `；有 ${missed} 个评分项没对上章节，见「目录体检」` : ''));
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async compose(el) {
    if (S.composeJob) return;
    // 整本写作会重排章节状态，先确认当前正文写完，否则未保存的改动会被直接冲掉
    if (!(await secFlush())) return toast('正文还没保存成功，先处理保存或冲突提示再整本写');
    const list = secList();
    if (!list.length) return toast('先生成目录');
    const force = el && el.dataset.force === '1';
    if (force && !await uiConfirm(
      '整本重写会用 AI 内容覆盖已有正文（重写前自动留快照，可撤销）。'
      + (S.mode === 'live' ? '\n\n' + composeCostNote(list.length) : '')
      + '\n\n继续？')) return;
    const ow = force || !!($('#overwrite') && $('#overwrite').checked);
    const empty = list.filter(s => !(s.text || '').trim() && !s.protected);
    if (!empty.length && !ow) {
      return toast(list.some(s => !(s.text || '').trim())
        ? '只剩保护章节（报价 / 投标函等），需要手工填写'
        : '全部章节都已有正文；要整本重写，请点「更多 · 重写整本」');
    }
    // 非覆盖重写时，live 模式给一次成本知情；force 已经在上面把成本一起问过了。
    if (!force && S.mode === 'live'
        && !await uiConfirm(composeCostNote(empty.length || list.length) + '\n\n继续写吗？',
                            { title: '开始写整本', okText: '开始生成' })) return;
    busy(el, true, '已提交…');
    try {
      const r = await api(`/api/projects/${S.pid}/jobs/compose`, 'POST', { overwrite: !!ow });
      S.lastJobId = r.job_id;
      S.composeJob = { jid: r.job_id, progress: 0, step: r.status === 'running' ? '正在生成…' : '排队中…' };
      S.skipDraft = true; renderWrite();
      toast(r.dedup
        ? '这个项目已经有一份整本生成在跑，已接上原来的任务，不会重复写'
        : `已开始写 ${empty.length || list.length} 章，可以留在这个页面看进度`);
      pollCompose(r.job_id);
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async genSec(el) {
    const sid = el.dataset.sid;
    const sec = secList().find(s => s.id === sid);
    if (!sec || S.genBusy) return;
    if (sec.protected) return toast('保护章节不自动生成，请手工填写');
    if ((sec.text || '').trim() && !await uiConfirm(`重写「${sec.title}」会用 AI 新内容替换现在的 ${(sec.text || '').length} 字，确定？`)) return;
    if (!(await secFlush())) return toast('正文还没保存成功，先处理保存或冲突提示');
    S.genBusy = sid; S.genTitle = sec.title; S.secDirty = false;
    if (S.secTimer) { clearTimeout(S.secTimer); S.secTimer = null; }
    S.skipDraft = true; renderWrite();
    try {
      const r = await api(`/api/projects/${S.pid}/section/${sid}/generate`, 'POST', {});
      S.secCandidate = r.conflict
        ? { sid, text: r.candidate || '', reason: r.message || '生成期间这一章被改过' }
        : null;
      S.skipDraft = true;
      await refresh();
      S.secId = sid;
      if (r.conflict) toast(S.secCandidate.reason + '；AI 稿在编辑器上方，可自行决定是否覆盖');
      else toast(r.fallback ? (r.message || '模型未返回，本章保持原样')
                       : r.quality && !r.quality.deliverable
                         ? (r.quality.message || '本章未达交付标准，导出时会列为重点提醒')
                         : `本章已生成，命中 ${(r.sources || []).length} 条资料`);
    } catch (e) { toast(e.message); }
    finally { S.genBusy = ''; S.genTitle = ''; S.skipDraft = true; renderWrite(); }
  },
  async rewriteSec(el) {
    const sid = el.dataset.sid;
    const sec = secList().find(s => s.id === sid);
    if (!sec || S.genBusy) return;
    if (sec.protected) return toast('保护章节不自动改写，请手工填写');
    if (!(sec.text || '').trim()) return toast('这一章还没写，先用「AI 生成本章」');
    if (!(await secFlush())) return toast('正文还没保存成功，先处理保存或冲突提示');
    const hint = (($('#secHint') || {}).value || '').trim();
    S.secHint = hint;
    S.genBusy = sid; S.genTitle = sec.title; S.secDirty = false;
    if (S.secTimer) { clearTimeout(S.secTimer); S.secTimer = null; }
    S.skipDraft = true; renderWrite();
    try {
      const r = await api(`/api/projects/${S.pid}/section/${sid}/rewrite`, 'POST', { instruction: hint });
      S.secCandidate = r.conflict
        ? { sid, text: r.candidate || '', reason: r.message || '改写期间这一章被改过' }
        : null;
      S.skipDraft = true;
      await refresh();
      S.secId = sid;
      if (r.conflict) toast(S.secCandidate.reason + '；AI 稿在编辑器上方，可自行决定是否覆盖');
      else toast(r.fallback ? (r.message || '模型未返回，本章保持原样')
                       : r.quality && !r.quality.deliverable
                         ? (r.quality.message || '本章未达交付标准，导出时会列为重点提醒')
                         : '本章已按资料改写');
    } catch (e) { toast(e.message); }
    finally { S.genBusy = ''; S.genTitle = ''; S.skipDraft = true; renderWrite(); }
  },
  async saveSec() {
    if (S.secConflict) return toast('先选择「用我的版本覆盖」或「换成服务端版本」');
    const ok = await saveSecNow();
    if (ok) toast('已保存');
    else if (!S.secConflict) toast('已保存，但编辑器里还有新改动，稍后会再存一次');
  },
  outlineMemToggle() {
    if (S.outlineMem) { S.outlineMem = null; S.skipDraft = true; renderWrite(); return; }
    S.outlineMem = 'loading'; S.skipDraft = true; renderWrite();
    api('/api/outlines?pid=' + encodeURIComponent(S.pid))
      .then(r => { S.outlineMem = r.items || []; renderWrite(); })
      .catch(e => { S.outlineMem = []; toast(e.message); renderWrite(); });
  },
  async outlineApply(el) {
    const it = (Array.isArray(S.outlineMem) ? S.outlineMem : []).find(x => x.pid === el.dataset.pid);
    if (!it) return;
    const written = secList().filter(s => (s.text || '').trim()).length;
    if (secList().length && !await uiConfirm(`用《${it.name}》的 ${it.count} 章目录替换当前目录？\n同名章节的正文保留，其余章节会移出目录（当前有 ${written} 章正文；替换前会自动留快照，可撤销）。`)) return;
    const r = await api(`/api/projects/${S.pid}/outline/apply`, 'POST', { titles: it.titles });
    S.outlineMem = null; S.bidImport = false;
    S.skipDraft = true;
    await refresh();
    S.secId = ''; S.skipDraft = true; renderWrite();
    toast(`已套用 ${r.count} 章目录，可以逐章写了`);
  },
  bidToggle() { S.bidImport = !S.bidImport; S.skipDraft = true; renderWrite(); },
  async importDraft(el) {
    const text = (($('#importText') || {}).value || '').trim();
    if (!text) return toast('请粘贴标书内容');
    busy(el, true, '导入中…');
    try {
      const r = await api(`/api/projects/${S.pid}/import`, 'POST', { text });
      S.bidImport = false;
      S.skipDraft = true;
      await refresh();
      S.secId = ''; S.skipDraft = true; renderWrite();
      toast(`已按标书切出 ${r.chapters} 章，正文原样保留`);
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },

  /* ---- 检查与修改 ---- */
  retryReadiness() {
    const box = $('#readinessBox');
    if (box) { delete box.dataset.done; box.innerHTML =
      '<h2>能不能交</h2><div class="hint">正在核对…</div>'; }
    S.readiness = null;
    loadReadiness();
  },
  issueFilter(el) { S.issueFilter = el.dataset.k || 'all'; renderCheck(); },
  async check(el) {
    busy(el, true, '检查中…');
    try {
      const r = await api(`/api/projects/${S.pid}/check`, 'POST', {});
      S.checkResult = { auto: r.auto, empty: !!r.empty, reason: r.reason || '' };
      await refresh();
      toast(r.empty ? (r.reason || '还没有正文可检查')
        : (r.total ? `检查完成：${r.total} 条问题，高风险 ${r.high} 条` : '检查完成：没有发现问题'));
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
  async optimize(el) {
    if (S.optJob) return;
    if (!((S.project.checks || []).some(c => c.status === 'accepted'))) {
      return toast('先在问题清单里点「采纳」，再回来改');
    }
    busy(el, true, '已提交…');
    try {
      const r = await api(`/api/projects/${S.pid}/jobs/optimize`, 'POST', { rounds: 2 });
      S.optJob = { jid: r.job_id, progress: 0, step: '排队中…' };
      S.skipDraft = true; renderCheck();
      pollOptimize(r.job_id);
    } catch (e) { toast(e.message); } finally { busy(el, false); }
  },
};

async function newProject() {
  const name = await uiPrompt('新项目名称：', '新投标项目', { title: '新建项目', okText: '创建' });
  if (!name) return;
  try {
    const p = await api('/api/projects', 'POST', { name });
    const m = $('#projectsModal'); if (m) m.hidden = true;
    await loadState(); await selectProject(p.id); toast('项目已创建');
  }
  catch (e) { toast(e.message); }
}

/* ---------------- 全部项目：归档 / 收进知识库 / 删除 ---------------- */
// 章节都有文字只是「正文已填写」，别让用户误以为质量/要求/附件都过了
const PJ_STATE = { empty: '还没建目录', doing: '进行中', done: '正文已填写' };

function pjWhen(s) {
  const t = (s || '').slice(0, 16).replace('T', ' ');
  return t || '—';
}

function renderProjectsPanel() {
  const body = $('#projectsBody');
  if (!body) return;
  const P = S.projPanel || { items: [], filter: 'all', q: '' };
  const kw = (P.q || '').trim().toLowerCase();
  const items = (P.items || []).filter(it => {
    if (P.filter === 'archived' && !it.archived) return false;
    if (P.filter === 'doing' && (it.archived || it.state !== 'doing')) return false;
    if (P.filter === 'done' && (it.archived || it.state !== 'done')) return false;
    if (kw && !(it.name || '').toLowerCase().includes(kw)) return false;
    return true;
  });
  const hint = $('#projHint');
  if (hint) hint.textContent = `全部 ${(P.items || []).length} 个，当前显示 ${items.length} 个`;
  if (!items.length) {
    body.innerHTML = `<div class="pj-empty">${kw ? `没有名字带「${esc(P.q)}」的项目` : '这个筛选下没有项目'}</div>`;
    return;
  }
  body.innerHTML = items.map(it => {
    const cls = [it.id === S.pid ? 'on' : '', it.archived ? 'arch' : ''].filter(Boolean).join(' ');
    const sub = [
      it.sections ? `${it.sections} 章 · 已写 ${it.written} 章` : '还没建目录',
      it.chars ? `${Number(it.chars).toLocaleString()} 字` : '',
      it.archived ? `归档于 ${pjWhen(it.archived_at)}` : `最近改动 ${pjWhen(it.updated_at)}`,
    ].filter(Boolean).join(' · ');
    const state = it.archived ? '已归档' : (it.state_label || PJ_STATE[it.state] || '');
    const chipCls = it.archived ? 'gray' : (it.state === 'done' ? 'ok' : 'low');
    return `<div class="pj-row ${cls}">
      <div class="pj-main" data-act="projOpen" data-pid="${esc(it.id)}" title="打开这个项目">
        <div class="pj-name">${esc(it.name)}<span class="chip ${chipCls}">${state}</span></div>
        <div class="pj-sub">${esc(sub)}</div>
      </div>
      <div class="pj-acts">
        <button class="btn mini" data-act="projToKb" data-pid="${esc(it.id)}" data-written="${Number(it.written || 0)}" ${it.written ? '' : 'title="还没有正文，先写完再收"'}>${it.kb_doc_id ? '更新知识库' : '收进知识库'}</button>
        <button class="btn mini" data-act="projArchive" data-pid="${esc(it.id)}" data-on="${it.archived ? '0' : '1'}">${it.archived ? '取消归档' : '归档'}</button>
        ${S.isOwner ? `<button class="btn mini danger" data-act="projDelete" data-pid="${esc(it.id)}">删除</button>` : ''}
      </div>
    </div>`;
  }).join('');
}

async function loadProjectsPanel() {
  const r = await api('/api/projects?scope=all');
  S.projPanel = { ...(S.projPanel || {}), items: r.items || [], counts: r.counts || {} };
  S.archivedCount = (r.counts || {}).archived || 0;
  renderProjectsPanel();
  renderSidebar();
}

async function openProjectsPanel(focusPid) {
  S.projPanel = { items: [], counts: {}, q: '', filter: 'all', focus: focusPid || '' };
  const m = $('#projectsModal'); if (m) m.hidden = false;
  const q = $('#projQ'); if (q) q.value = '';
  $$('#projFilter button').forEach(b => b.classList.toggle('on', b.dataset.filter === 'all'));
  renderProjectsPanel();
  try { await loadProjectsPanel(); } catch (e) { toast(e.message); }
}

async function archiveProjectFromPanel(pid, on) {
  try {
    await api(`/api/projects/${pid}/archive`, 'POST', { on });
    const wasCurrent = pid === S.pid;
    await loadProjectsPanel();
    await loadState();
    if (on && wasCurrent && !(S.projects || []).length) {
      toast('已归档。这是最后一个项目，新建一个或在这儿点「取消归档」');
    } else {
      toast(on ? '已归档，切到「已归档」能找回来' : '已取消归档');
    }
  } catch (e) { toast(e.message); }
}

async function toKbFromPanel(pid, el) {
  // 没有正文的项目不再静默禁用按钮：点了要明确告诉用户为什么不能收，别像坏了
  if (el && el.dataset && !Number(el.dataset.written || 0)) {
    toast('这本还没有正文，先写完或导入正文，再收进知识库');
    return;
  }
  const it = (S.projPanel.items || []).find(x => x.id === pid) || {};
  const open = Math.max(0, (it.sections || 0) - (it.written || 0));
  if (open) {
    if (!await uiConfirm(`这本还有 ${open} 章没有正文，收进去只能当不完整的范例。\n仍然收录吗？`,
      { title: '收进知识库', okText: '仍然收录' })) return;
  }
  busy(el, true, '收录中…');
  try {
    const r = await api(`/api/projects/${pid}/to-kb`, 'POST', {});
    const rest = (r.empty || []).length;
    toast(`${r.updated ? '已更新知识库里的' : '已收进知识库：'}《${r.title}》`
      + `${r.chapters ? `，拆成 ${r.chapters} 章` : ''}`
      + `${rest ? `；${rest} 章没正文，未收录` : ''}`
      + `${r.verification_label ? `；${r.verification_label}` : ''}`);
    await loadProjectsPanel();
  } catch (e) { toast(e.message); } finally { busy(el, false); }
}

async function deleteProjectFromPanel(pid) {
  const it = (S.projPanel.items || []).find(x => x.id === pid) || {};
  if (!await uiConfirm(`删除「${it.name || '这个项目'}」？\n\n正文 ${it.sections || 0} 章、上传的资料、检查记录、导出文件会一起删掉，删完不能恢复。\n只是想从列表收起来的话，用「归档」。`,
    { title: '删除项目', danger: true, okText: '永久删除' })) return;
  if (!await uiConfirm('再确认一次：真的要永久删除吗？',
    { title: '删除项目', danger: true, okText: '确认删除' })) return;
  try {
    await api('/api/projects/' + pid, 'DELETE');
    const wasCurrent = pid === S.pid;
    if (wasCurrent) { S.pid = ''; S.project = null; saveUI({ pid: '' }); }
    else S.projects = (S.projects || []).filter(p => p.id !== pid);
    await loadProjectsPanel();
    if (!wasCurrent) { renderSidebar(); toast('已删除'); return; }
    await loadState();
    if (!S.pid) {
      const m = $('#projectsModal'); if (m) m.hidden = true;
      toast('已删除。现在没有项目了，点「新建项目」开始');
      location.reload();
      return;
    }
    toast('已删除');
  } catch (e) { toast(e.message); }
}

async function setMode(mode) {
  if (mode === 'live' && S.privacy && !S.privacy.outbound_allowed) {
    openPrivacy(); toast('请先确认数据出域设置'); return;
  }
  try {
    await api('/api/mode', 'POST', { mode });
    const st = await api('/api/state');
    S.mode = st.mode; S.model = st.model; S.models = st.models;
    renderHeader(); toast(mode === 'live' ? '已切换到真实模型' : '已切换到演示模型');
  } catch (e) {
    toast(e.message);
    if (mode === 'live') openModels();
  }
}

/* ---------------- 多个真实模型：切换 / 接入 / 测试 / 分工 ---------------- */
function blankForm() {
  return { id: '', preset: 'custom', label: '', base_url: '', api_key: '', model: '',
           max_tokens: '4096', timeout: '120' };
}
function readForm() {
  const g = (id) => { const el = $('#' + id); return el ? el.value : ''; };
  return { id: g('mId'), preset: g('mPreset'), label: g('mLabel'), base_url: g('mBase'),
           api_key: g('mKey'), model: g('mModel'), max_tokens: g('mTokens'), timeout: g('mTimeout') };
}
function fillForm(d) {
  const s = (id, v) => { const el = $('#' + id); if (el) el.value = v ?? ''; };
  s('mId', d.id);
  const ps = $('#mPreset');
  if (ps) {
    const want = d.preset || 'custom';
    ps.value = Array.from(ps.options).some(o => o.value === want) ? want : 'custom';
  }
  s('mLabel', d.label);
  s('mBase', d.base_url); s('mKey', d.api_key || ''); s('mModel', d.model);
  s('mTokens', d.max_tokens || 4096); s('mTimeout', d.timeout || 120);
  const t = $('#modelFormTitle'); if (t) t.textContent = d.id ? '编辑模型（密钥留空表示不修改）' : '新增模型';
}
async function setActiveModel(id) {
  if (!id) return;
  try {
    const r = await api('/api/models/active', 'POST', { id });
    const st = await api('/api/state');
    S.mode = st.mode; S.model = st.model; S.models = st.models;
    renderHeader();
    if (S.modelReg) { S.modelReg.active = r.active; renderModels(); }
    toast('已切换到：' + ((S.models.list.find(x => x.id === id) || {}).label || id));
  } catch (e) { toast(e.message); renderHeader(); }
}
async function openModels(forceDraft = false) {
  try {
    S.modelReg = await api('/api/models');
    $('#modelModal').hidden = false;
    renderModels(forceDraft);
  } catch (e) { toast(e.message); }
}
function closeModels() { const m = $('#modelModal'); if (m) m.hidden = true; }

function renderModels(forceDraft = false) {
  const reg = S.modelReg; if (!reg) return;
  const draft = (!forceDraft && $('#mId')) ? readForm() : (S.modelDraft || blankForm());
  S.modelDraft = draft;
  const roleOpts = (sel) => '<option value="">跟随当前模型</option>' + reg.models.map(m =>
    `<option value="${esc(m.id)}" ${sel === m.id ? 'selected' : ''}>${esc(m.label)} · ${esc(m.model)}</option>`).join('');
  const roles = Object.entries(reg.role_labels || {}).map(([k, label]) =>
    `<label class="mm-role"><span>${esc(label)}</span><select class="role-select" data-role="${esc(k)}">${roleOpts((reg.roles || {})[k] || '')}</select></label>`).join('');
  const items = reg.models.map(m => {
    const t = (S.testResult && S.testResult.id === m.id) ? S.testResult.text : '';
    return `<div class="mm-item ${reg.active === m.id ? 'on' : ''}">
      <div class="mm-main">
        <div><b>${esc(m.label)}</b>
          ${reg.active === m.id ? '<span class="chip blue">当前</span>' : ''}
          <span class="chip ${m.ready ? 'ok' : 'warn'}">${m.ready ? '可用' : '配置不全'}</span></div>
        <div class="mm-sub">${esc(m.model)} · ${esc(m.base_url)} · ${esc(m.key_hint)}</div>
        ${t ? `<div class="mm-test ${t.startsWith('OK') ? 'ok' : 'bad'}">${esc(t)}</div>` : ''}
      </div>
      <div class="mm-act">
        <button class="btn mini" data-act="modelUse" data-id="${esc(m.id)}">设为当前</button>
        <button class="btn mini" data-act="modelTest" data-id="${esc(m.id)}">测试连通</button>
        <button class="btn mini" data-act="modelEdit" data-id="${esc(m.id)}">编辑</button>
        <button class="btn mini danger" data-act="modelDel" data-id="${esc(m.id)}">删除</button>
      </div></div>`;
  }).join('') || '<div class="empty">还没有接入模型，在下面填一个就能用。</div>';
  const presets = (reg.presets || []).map(p =>
    `<option value="${esc(p.provider)}">${esc(p.label)}</option>`).join('');
  $('#modelBody').innerHTML = `
    <div class="mm-block"><div class="mini-title">已接入 ${reg.models.length} 个模型</div>
      <div class="mm-list">${items}</div></div>
    <div class="mm-block"><div class="mini-title">按环节分工（不设就跟随当前模型；基座 AI 判定能力跟随「验标书 / 复核」环节）</div>
      <div class="mm-roles">${roles}</div></div>
    <div class="mm-block"><div class="mini-title" id="modelFormTitle">新增模型</div>
      <div class="mm-form">
        <input type="hidden" id="mId" />
        <label>服务商预设<select id="mPreset">${presets}</select></label>
        <label>名称<input id="mLabel" placeholder="例如：DeepSeek 主力" /></label>
        <label>接口地址<input id="mBase" placeholder="https://api.deepseek.com/v1" /></label>
        <label>API Key<input id="mKey" type="password" placeholder="只存本机；编辑时留空表示不改" /></label>
        <label>模型名<input id="mModel" placeholder="deepseek-chat" /></label>
        <label>最大输出<input id="mTokens" type="number" value="4096" /></label>
        <label>超时（秒）<input id="mTimeout" type="number" value="120" /></label>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="btn primary" data-act="modelSave">保存并接入</button>
        <button class="btn" data-act="modelReset">清空表单</button>
      </div></div>`;
  fillForm(draft);
}

async function onModelChange(e) {
  const t = e.target;
  if (t.id === 'mPreset') {
    const p = ((S.modelReg || {}).presets || []).find(x => x.provider === t.value);
    if (!p) return;
    if (p.base_url) $('#mBase').value = p.base_url;
    if (p.model) $('#mModel').value = p.model;
    if (!$('#mId').value && !$('#mLabel').value) $('#mLabel').value = p.label;
  }
  if (t.id === 'embProvider') {
    // 换平台必须把地址、模型名、维度一起换掉，否则会拿旧地址配新模型，400 报错
    const p = ((S.embedding || {}).providers || []).find(x => x.id === t.value);
    if (!p) return;
    const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    if (p.base_url) setVal('embBase', p.base_url);
    if (p.model) setVal('embModel', p.model);
    const dimInp = document.getElementById('embDim');
    const dimList = document.getElementById('embDimList');
    const dims = (p.dims && p.dims.length) ? p.dims : [p.default_dim || 1024];
    if (dimList) dimList.innerHTML = dims.map(d => `<option value="${d}"></option>`).join('');
    if (dimInp) dimInp.value = String(p.default_dim || dims[0]);
    const hintEl = document.getElementById('embHint');
    if (hintEl) hintEl.textContent = p.hint || '';
    return;
  }
  if (t.classList && t.classList.contains('role-select')) {
    try {
      await api('/api/models/role', 'POST', { role: t.dataset.role, id: t.value });
      if (S.modelReg) S.modelReg.roles[t.dataset.role] = t.value;
      toast('已更新这个环节用的模型');
    } catch (err) { toast(err.message); }
  }
}

/* 应用内弹窗的按钮与键盘：直接绑定，不经过 data-act 委托，
   避免在沙箱 / 事件被拦截时连确认都点不了 */
$('#dlgCancel').onclick = _dlgCancel;
$('#dlgOk').onclick = _dlgOk;
$('#uiDialog').addEventListener('click', (e) => {
  if (e.target.id === 'uiDialog') _dlgCancel();
});
document.addEventListener('keydown', (e) => {
  const box = $('#uiDialog');
  if (!box || box.hidden) return;
  if (e.key === 'Escape') { e.preventDefault(); _dlgCancel(); }
  else if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _dlgOk(); }
});

document.addEventListener('change', onBundleChange);
document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-act]'); if (!el) return;
  const fn = ACT[el.dataset.act]; if (!fn) return;
  e.preventDefault();
  Promise.resolve(fn(el)).catch(err => toast(err.message || '操作失败'));
});

// 离开保护：1.5 秒自动保存窗口内刷新/关页会丢稿，先明确提示。
// 草稿只在本页内存里，提示后仍需用户自己点保存；不做自作主张的同步提交。
window.addEventListener('beforeunload', (e) => {
  if (!S.secDirty && !S.secTimer) return;
  e.preventDefault();
  e.returnValue = '';
  return '';
});

boot().catch(e => toast('初始化失败：' + e.message));
