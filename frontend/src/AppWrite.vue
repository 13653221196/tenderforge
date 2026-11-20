<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from './api'
import AppShell from './AppShell.vue'

const UI_KEY = 'bidagent.ui.v1'
const STEPS = [
  { n: 1, label: '传文件' },
  { n: 2, label: '看要点' },
  { n: 3, label: '写竞标书' },
  { n: 4, label: '检查导出' },
]

const projects = ref([])
const pid = ref('')
const project = ref(null)
const plan = ref({})
const loading = ref(true)
const error = ref('')
const selectedId = ref('')
const job = ref(null)
const jobId = ref('')
const busy = ref(false)
const editing = ref(false)
const draftText = ref('')
const saving = ref(false)
const notice = ref('')
const conflict = ref(null)
const aiBrief = ref('')
const aiBusy = ref(false)
let timer = null

const sections = computed(() => project.value?.sections || [])
const written = computed(() => sections.value.filter((s) => String(s.text || '').trim()).length)
const emptyOpen = computed(() => sections.value.filter((s) => !String(s.text || '').trim() && !s.protected).length)
const emptyProt = computed(() => sections.value.filter((s) => !String(s.text || '').trim() && s.protected).length)
const selected = computed(() => sections.value.find((s) => s.id === selectedId.value) || sections.value[0] || null)
const previewText = computed(() => String(selected.value?.text || '').slice(0, 6000))
const selectedWords = computed(() => String(selected.value?.text || '').replace(/\s/g, '').length)
const wordsByTitle = computed(() => {
  const map = {}
  for (const c of (project.value?.outline || [])) {
    if (c && c.title) map[c.title] = Number(c.words || 0)
  }
  return map
})
const hasMore = computed(() => String(selected.value?.text || '').length > previewText.value.length)
const previewBlocks = computed(() => mdBlocks(previewText.value))
const tableCount = computed(() => previewBlocks.value.filter((b) => b.type === 'table').length)

const totalBudget = computed(() => sections.value.reduce((n, s) => n + (wordsByTitle.value[s.title] || 0), 0))
const totalWords = computed(() => sections.value.reduce((n, s) => n + charCount(s.text), 0))
const wordPct = computed(() => totalBudget.value ? Math.min(100, Math.round((totalWords.value / totalBudget.value) * 100)) : 0)

function charCount(text) {
  return String(text || '').replace(/\s/g, '').length
}

function chapPct(s) {
  const budget = wordsByTitle.value[s.title] || 0
  if (!budget) return charCount(s.text) ? 100 : 0
  return Math.min(100, Math.round((charCount(s.text) / budget) * 100))
}

function mdCells(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((x) => x.trim())
}

/** 只解析写作规范允许的四种排版：### 子标题、段落、- 列表、Markdown 表格。 */
function mdBlocks(text) {
  const lines = String(text || '').split('\n')
  const out = []
  const isRow = (s) => s.trim().startsWith('|') && s.trim().endsWith('|')
  let i = 0
  while (i < lines.length) {
    const t = lines[i].trim()
    if (!t) { i += 1; continue }
    if (isRow(t) && i + 1 < lines.length && /^\|[\s:|-]+\|$/.test(lines[i + 1].trim())) {
      const head = mdCells(t)
      i += 2
      const rows = []
      while (i < lines.length && isRow(lines[i])) { rows.push(mdCells(lines[i])); i += 1 }
      out.push({ type: 'table', head, rows })
      continue
    }
    if (t.startsWith('###')) { out.push({ type: 'h', text: t.replace(/^#+\s*/, '') }); i += 1; continue }
    if (/^[-*]\s+/.test(t)) {
      const items = []
      while (i < lines.length && /^[-*]\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^[-*]\s+/, ''))
        i += 1
      }
      out.push({ type: 'ul', items })
      continue
    }
    out.push({ type: 'p', text: t })
    i += 1
  }
  return out
}
const unmatched = computed(() => (plan.value?.unmatched_scoring || []).filter((x) => typeof x === 'string' && x.trim()))

const next = computed(() => {
  const p = project.value
  if (!p) return { step: 3, level: 'warn', title: '正在加载项目', desc: '稍等…', action: '稍等', tab: 'write' }
  if (!sections.value.length) {
    return { step: 3, level: 'warn', title: '还没有目录', desc: '回旧版按招标文件生成目录，再来写。', action: '回旧版生成目录', tab: 'write' }
  }
  if (!String(p.outline_confirmed_at || '').trim()) {
    return { step: 3, level: 'warn', title: '目录还没确认', desc: '确认后才能写正文；改目录后需要重新确认。', action: '确认目录', act: 'confirm' }
  }
  if (busy.value) {
    return { step: 3, level: 'warn', title: job.value?.step || '正在生成正文', desc: `进度 ${job.value?.progress || 0}%，已完成章节不会被覆盖。`, action: '生成中…', act: 'none' }
  }
  if (emptyOpen.value) {
    return {
      step: 3, level: 'warn',
      title: `还有 ${emptyOpen.value} 章没写`,
      desc: '整本生成会按目录逐章写，已有正文不会被覆盖。',
      action: written.value ? `写剩下 ${emptyOpen.value} 章` : '一键写整本',
      act: 'compose',
    }
  }
  if (emptyProt.value) {
    return { step: 3, level: 'warn', title: `还有 ${emptyProt.value} 章保护内容要手填`, desc: '投标函、报价表等只能人工填原件，系统不代写。', action: '回旧版手填', tab: 'write' }
  }
  return { step: 4, level: 'ok', title: '正文齐了', desc: '下一步检查资格、评分、废标，再导出。', action: '回旧版检查', tab: 'check' }
})

watch(sections, (list) => {
  if (!list.length) {
    selectedId.value = ''
    return
  }
  if (!list.some((s) => s.id === selectedId.value)) selectedId.value = list[0].id
}, { immediate: true })

async function boot() {
  try {
    const st = await api('/api/state')
    projects.value = st.projects || []
    const fromUrl = new URLSearchParams(location.search).get('pid')
    const hit = projects.value.find((p) => p.id === fromUrl)
    pid.value = hit ? hit.id : (projects.value[0]?.id || '')
    if (pid.value) await loadAll()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function loadAll() {
  if (!pid.value) return
  loading.value = true
  error.value = ''
  try {
    const [p, pl] = await Promise.all([
      api(`/api/projects/${pid.value}`),
      api(`/api/projects/${pid.value}/outline/plan`).catch(() => ({})),
    ])
    project.value = p
    plan.value = pl || {}
    const url = new URL(location.href)
    url.searchParams.set('pid', pid.value)
    history.replaceState(null, '', url)
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function confirmOutline() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await api(`/api/projects/${pid.value}/outline/confirm`, { method: 'POST', body: {} })
    await loadAll()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

async function compose() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    const r = await api(`/api/projects/${pid.value}/jobs/compose`, { method: 'POST', body: { overwrite: false } })
    jobId.value = r.job_id || ''
    job.value = { status: r.status || 'queued', progress: r.progress || 0, step: r.step || '排队中' }
    if (jobId.value) poll()
    else {
      busy.value = false
    }
  } catch (e) {
    error.value = e.message
    busy.value = false
  }
}

async function poll() {
  if (!jobId.value) return
  try {
    const j = await api(`/api/jobs/${jobId.value}`)
    job.value = j
    if (j.status === 'queued' || j.status === 'running') {
      timer = setTimeout(poll, 1200)
      return
    }
    busy.value = false
    jobId.value = ''
    await loadAll()
  } catch (e) {
    error.value = e.message
    busy.value = false
  }
}

function stopPoll() {
  if (timer) {
    clearTimeout(timer)
    timer = null
  }
}

function selectChapter(id) {
  if (editing.value && id !== selectedId.value) {
    if (!window.confirm('这一章还没保存，切换会丢掉改动，继续？')) return
    editing.value = false
    conflict.value = null
  }
  selectedId.value = id
}

function startEdit() {
  const s = selected.value
  if (!s) return
  draftText.value = String(s.text || '')
  conflict.value = null
  notice.value = ''
  editing.value = true
}

function cancelEdit() {
  editing.value = false
  conflict.value = null
  aiBrief.value = ''
}

async function aiWrite() {
  const s = selected.value
  if (!s || aiBusy.value) return
  const brief = aiBrief.value.trim()
  if (!brief) {
    error.value = '先写一句你想要的这一章内容，比如要包含哪些点、什么口径'
    return
  }
  const dirty = draftText.value.trim() !== String(s.text || '').trim()
  const tip = String(s.text || '').trim()
    ? 'AI 会按你的描述改写并直接保存这一章，可在「交付文件 → 版本回退」恢复。继续？'
    : 'AI 会按你的描述直接写这一章。继续？'
  if (dirty) {
    if (!window.confirm('编辑器里还有没保存的改动，AI 写完后会被覆盖。继续？')) return
  } else if (!window.confirm(tip)) {
    return
  }
  aiBusy.value = true
  error.value = ''
  notice.value = ''
  try {
    const r = await api(`/api/projects/${pid.value}/section/${s.id}/rewrite`, {
      method: 'POST',
      body: { instruction: brief },
    })
    if (r?.error) {
      error.value = r.error
      return
    }
    if (r?.conflict) {
      draftText.value = String(r.candidate || '')
      conflict.value = { ver: r.ver || 0 }
      error.value = r.message || '操作期间这一章被改过，AI 稿没有写入'
      return
    }
    if (r?.fallback || !String(r?.text || '').trim()) {
      error.value = r?.message || '模型没有返回正文，这一章保持原样'
      return
    }
    editing.value = false
    conflict.value = null
    aiBrief.value = ''
    await loadAll()
    selectedId.value = s.id
    notice.value = `AI 已写好「${s.title}」`
  } catch (e) {
    error.value = e.message
  } finally {
    aiBusy.value = false
  }
}

async function saveEdit(force = false) {
  const s = selected.value
  if (!s || saving.value) return
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    const body = force
      ? { text: draftText.value, allow_overwrite: true }
      : { text: draftText.value, base_ver: Number(s.ver || 0) }
    const r = await api(`/api/projects/${pid.value}/section/${s.id}`, { method: 'PUT', body })
    editing.value = false
    conflict.value = null
    await loadAll()
    selectedId.value = s.id
    notice.value = `已保存「${s.title}」，版本 v${r.ver}`
  } catch (e) {
    if (e.status === 409) {
      conflict.value = { ver: e.body?.ver }
      error.value = '这一章在你编辑期间被改动过，你的稿还没覆盖它。'
    } else if (e.status === 503) {
      error.value = '同时有别的保存正在写入，等一两秒再存。'
    } else {
      error.value = e.message
    }
  } finally {
    saving.value = false
  }
}

function gotoOld(tab, sid = '') {
  try {
    const old = JSON.parse(localStorage.getItem(UI_KEY) || '{}') || {}
    const patch = { ...old, pid: pid.value, tab: tab || 'write' }
    if (sid) patch['sec:' + pid.value] = sid
    localStorage.setItem(UI_KEY, JSON.stringify(patch))
  } catch { /* 忽略本地存储异常 */ }
  location.href = '/'
}

function goNext() {
  const n = next.value
  if (n.act === 'confirm') return confirmOutline()
  if (n.act === 'compose') return compose()
  if (n.act === 'none') return
  gotoOld(n.tab)
}

onMounted(boot)
onBeforeUnmount(stopPoll)
</script>

<template>
  <AppShell page="write" title="标书正文" subtitle="看目录 → 写正文 → 去检查" :projects="projects" v-model:pid="pid" @change="loadAll">
      <ol class="steps">
        <li v-for="s in STEPS" :key="s.n" :class="{ on: s.n === next.step, done: s.n < next.step }">
          <span class="dot">{{ s.n }}</span>{{ s.label }}
        </li>
      </ol>

      <div v-if="error" class="notice bad">{{ error }}</div>
      <div v-if="notice" class="notice ok">{{ notice }}</div>
      <div v-if="loading && !project" class="card muted">正在加载…</div>
      <div v-else-if="!pid" class="card">还没有项目。请回旧版新建项目后再来。</div>

      <template v-else-if="project">
        <div class="next" :class="next.level">
          <div class="next-text">
            <b>{{ next.title }}</b>
            <div class="hint">{{ next.desc }}</div>
          </div>
          <button class="btn primary" :disabled="busy" @click="goNext">{{ next.action }}</button>
        </div>

        <section class="card">
          <div class="card-head">
            <h2>正文进度</h2>
            <span class="chip" :class="{ ok: written === sections.length && sections.length }">
              {{ written }}/{{ sections.length }} 章已写
            </span>
          </div>
          <div class="grid">
            <div class="stat"><div class="k">总章数</div><div class="v">{{ sections.length }}</div><div class="s">按目录展开</div></div>
            <div class="stat"><div class="k">已写</div><div class="v">{{ written }}</div><div class="s">有正文的章节</div></div>
            <div class="stat" :class="{ bad: emptyOpen > 0 }"><div class="k">待写</div><div class="v">{{ emptyOpen }}</div><div class="s">可交给系统写</div></div>
            <div class="stat" :class="{ bad: emptyProt > 0 }"><div class="k">保护章待手填</div><div class="v">{{ emptyProt }}</div><div class="s">投标函 / 报价表等</div></div>
          </div>

          <div v-if="totalBudget" class="words">
            <div class="job-line">
              <span>正文字数</span><span class="grow"></span>
              <b>{{ totalWords }}</b>/{{ totalBudget }} 字 · {{ wordPct }}%
            </div>
            <div class="bar"><i :style="{ width: wordPct + '%' }" :class="{ low: wordPct < 60 }"></i></div>
            <div v-if="wordPct < 60 && written" class="hint">
              偏短：预算 {{ totalBudget }} 字，实际 {{ totalWords }} 字。可回旧版把偏短的章重新生成。
            </div>
          </div>

          <div v-if="busy && job" class="job">
            <div class="job-line"><span class="spin"></span><span>{{ job.step || '正在生成…' }}</span><span class="grow"></span><b>{{ job.progress || 0 }}%</b></div>
            <div class="bar"><i :style="{ width: (job.progress || 0) + '%' }"></i></div>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <h2>目录与正文</h2>
            <span class="hint">点左边任意一章，右边预览；改字回旧版。</span>
          </div>
          <div v-if="!sections.length" class="empty">
            还没有目录。请回旧版按招标文件生成目录后再来。
          </div>
          <div v-else class="split">
            <aside class="chap-pane">
              <div class="chap" v-for="(s, i) in sections" :key="s.id" :class="{ on: s.id === selected?.id }" @click="selectChapter(s.id)">
                <div class="chap-no">{{ i + 1 }}</div>
                <div class="chap-main">
                  <div class="chap-title">{{ s.title }}</div>
                  <div class="chap-sub">
                    <span v-if="s.protected" class="badge prot">保护章</span>
                    <span v-if="!s.protected && wordsByTitle[s.title]" class="badge">预算 {{ wordsByTitle[s.title] }} 字</span>
                    <span class="badge" :class="String(s.text || '').trim() ? 'ok' : 'warn'">
                      {{ String(s.text || '').trim() ? '已写 ' + charCount(s.text) + ' 字' : '未写' }}
                    </span>
                  </div>
                  <div v-if="!s.protected && wordsByTitle[s.title] && String(s.text || '').trim()" class="mini-bar">
                    <i :style="{ width: chapPct(s) + '%' }" :class="{ low: chapPct(s) < 60 }"></i>
                  </div>
                </div>
              </div>
            </aside>

            <section class="preview">
              <template v-if="selected">
                <div class="preview-head">
                  <div>
                    <div class="preview-title">
                      {{ selected.title }}
                      <span v-if="editing" class="badge">编辑中</span>
                    </div>
                    <div class="hint">
                      <template v-if="editing">
                        可以手改，也可以让 AI 按你的描述重写；支持 ### 小标题、- 列表、| 表格 | 写法。
                      </template>
                      <template v-else-if="selected.protected">
                        保护章节：请人工填写原件内容，系统不代写。
                      </template>
                      <template v-else>
                        {{ previewText ? `预览${tableCount ? ` · 含 ${tableCount} 个表格` : ''}` : '这一章还没写，可以直接写或回旧版生成。' }}
                      </template>
                    </div>
                  </div>
                  <div class="preview-acts">
                    <template v-if="editing">
                      <button class="btn" :disabled="saving || aiBusy" @click="cancelEdit">取消</button>
                      <button class="btn primary" :disabled="saving || aiBusy" @click="saveEdit(false)">{{ saving ? '保存中…' : '保存' }}</button>
                    </template>
                    <template v-else>
                      <button class="btn" @click="gotoOld('write', selected.id)">旧版高级编辑</button>
                      <button class="btn primary" @click="startEdit">
                        {{ selected.protected ? '填写这一章' : (selectedWords ? '编辑 / AI 写这一章' : '写这一章') }}
                      </button>
                    </template>
                  </div>
                </div>

                <template v-if="editing">
                  <div v-if="!selected.protected" class="ai-box">
                    <div class="ai-head">
                      <b>AI 按你的描述写这一章</b>
                      <span class="hint">写清要包含的内容和口径，AI 直接写进这一章（可回退）</span>
                    </div>
                    <textarea
                      v-model="aiBrief"
                      class="ai-input"
                      :disabled="aiBusy"
                      placeholder="例：写运维服务方案，包含 7×24 值班、SLA 99.9%、故障分级响应时限（一般 30 分钟、重大 10 分钟），并给出人员配置表"
                    ></textarea>
                    <div class="preview-acts" style="margin-top:8px">
                      <button class="btn primary" :disabled="aiBusy" @click="aiWrite">
                        {{ aiBusy ? 'AI 正在写，可能要 1—2 分钟…' : (selectedWords ? '按描述改写这一章' : '按描述生成这一章') }}
                      </button>
                      <span class="hint">不写描述也可以，直接在下面手动改。</span>
                    </div>
                  </div>

                  <textarea v-model="draftText" class="editor" :disabled="aiBusy" spellcheck="false" placeholder="在这里写这一章的正文……"></textarea>
                  <div class="hint" style="margin-top:6px">
                    当前 {{ charCount(draftText) }} 字<template v-if="wordsByTitle[selected.title]"> · 预算 {{ wordsByTitle[selected.title] }} 字</template>
                  </div>
                  <div v-if="conflict" class="notice bad" style="margin-top:10px">
                    <template v-if="conflict.ver">服务器上的版本已经是 v{{ conflict.ver }}。</template>
                    这一章在你操作期间被改动过，结果还没写入。
                    <button class="btn" style="margin-left:8px" :disabled="saving || aiBusy" @click="saveEdit(true)">用我手上的稿覆盖</button>
                    <button class="btn" style="margin-left:6px" @click="cancelEdit(); loadAll()">放弃并重新加载</button>
                  </div>
                </template>

                <template v-else>
                  <div v-if="previewText" class="md">
                    <template v-for="(b, i) in previewBlocks" :key="i">
                      <h3 v-if="b.type === 'h'">{{ b.text }}</h3>
                      <ul v-else-if="b.type === 'ul'">
                        <li v-for="(it, j) in b.items" :key="j">{{ it }}</li>
                      </ul>
                      <div v-else-if="b.type === 'table'" class="md-table">
                        <table>
                          <thead><tr><th v-for="(h, j) in b.head" :key="j">{{ h }}</th></tr></thead>
                          <tbody>
                            <tr v-for="(r, ri) in b.rows" :key="ri">
                              <td v-for="(c, ci) in r" :key="ci">{{ c }}</td>
                            </tr>
                          </tbody>
                        </table>
                      </div>
                      <p v-else>{{ b.text }}</p>
                    </template>
                  </div>
                  <div v-else class="empty">
                    {{ selected.protected ? '需要人工填写原件内容，点右上角「填写这一章」。' : '还没有正文。可以直接写这一章，或用上方「一键写整本」。' }}
                  </div>
                  <div v-if="hasMore" class="hint">只显示前 {{ previewText.length }} 字，共 {{ selectedWords }} 字。</div>
                </template>
              </template>
              <div v-else class="empty">左侧选一章查看。</div>
            </section>
          </div>
        </section>

        <details class="card fold">
          <summary>展开看目录体检：评分项有没有漏、篇幅预算</summary>
          <div class="fold-body">
            <div class="fold-grid">
              <div><h3>评分办法</h3><p class="hint">{{ plan?.scoring_items || 0 }} 项</p></div>
              <div><h3>篇幅预算</h3><p class="hint">{{ (plan?.total_words || 0).toLocaleString() }} 字 / 约 {{ plan?.estimated_pages || 0 }} 页</p></div>
              <div><h3>按招标格式展开</h3><p class="hint">{{ plan?.used_format || 0 }} 章</p></div>
            </div>
            <template v-if="unmatched.length">
              <h3>没对上章节的评分项（{{ unmatched.length }}）</h3>
              <ul class="lines">
                <li v-for="(n, i) in unmatched.slice(0, 30)" :key="i">{{ n }}</li>
              </ul>
              <p class="hint">可能漏了得分点，也可能已并入相关章节，请逐条核对；需要补章可回旧版操作。</p>
            </template>
            <p v-else class="hint" style="margin-top:12px">没有发现未匹配的评分项。</p>
          </div>
        </details>
      </template>
  </AppShell>
</template>
