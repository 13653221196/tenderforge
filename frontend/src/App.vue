<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from './api'
import AppShell from './AppShell.vue'

const UI_KEY = 'bidagent.ui.v1'
const PARSE_KEYS = [
  'effective_chars', 'pages', 'scan_pages', 'ocr_pages', 'unread_pages',
  'ocr_engine', 'parse_status', 'needs_review', 'usable', 'review_note',
  'limit_note', 'suffix', 'images', 'image_text', 'image_ocr_count',
  'image_ocr_engine',
]
const STEPS = [
  { n: 1, label: '传文件' },
  { n: 2, label: '看要点' },
  { n: 3, label: '写竞标书' },
  { n: 4, label: '检查导出' },
]

const projects = ref([])
const pid = ref('')
const project = ref(null)
const brief = ref(null)
const parsing = ref({ count: 0 })
const loading = ref(true)
const uploading = ref(false)
const error = ref('')
const toast = ref('')
const fileInput = ref(null)

const tenderChars = computed(() => {
  const n = String(project.value?.tender_text || '').replace(/\s+/g, '').length
  return n.toLocaleString()
})
const counts = computed(() => brief.value?.requirements?.counts || {})
const scoringTotal = computed(() => brief.value?.scoring?.total || 0)
const scoringRows = computed(() => (brief.value?.scoring?.rows || []).slice(0, 8))
const pendingCount = computed(() => Number(parsing.value?.count || brief.value?.pending_docs || 0))

const tenderParse = computed(() => {
  const raw = project.value?.tender_parse
  if (!raw) return null
  if (typeof raw === 'object') return raw
  try { return JSON.parse(raw) } catch { return null }
})
const tenderParseNote = computed(() => {
  const t = tenderParse.value
  if (!t) return ''
  if (t.needs_review && !t.confirmed_at) {
    const pages = (t.unread_pages || []).slice(0, 12).join('、')
    return `有页面没识别${pages ? `（第 ${pages} 页）` : ''}，已标“待核对”。回旧版确认后才进入依据池。`
  }
  if (t.ocr_pages) return `${t.ocr_pages} 页由本地 OCR 识别，页码可回跳。`
  return ''
})

const next = computed(() => {
  const p = project.value
  if (!p) return { step: 1, level: 'warn', title: '正在加载项目', desc: '稍等…', action: '稍等', tab: 'materials' }
  if (!String(p.tender_text || '').trim()) {
    return { step: 1, level: 'warn', title: '还没有招标文件', desc: '先上传，系统才能抽要求、排目录。', action: '选择文件', pick: true }
  }
  if (pendingCount.value > 0) {
    return { step: 2, level: 'warn', title: `有 ${pendingCount.value} 份资料待核对`, desc: '未核对的内容不会进入依据池；核对后再往下走。', action: '回旧版核对', tab: 'materials' }
  }
  if (!(brief.value?.requirements?.total > 0)) {
    return { step: 2, level: 'warn', title: '还没抽出招标要求', desc: '回旧版点一次“提取要求”，再回来看要点。', action: '回旧版提取', tab: 'materials' }
  }
  const secs = p.sections || []
  if (!secs.length) {
    return { step: 3, level: 'warn', title: '要点已就绪，还没生成目录', desc: '回旧版“标书正文”生成目录，再逐章写。', action: '回旧版生成目录', tab: 'write' }
  }
  const wrote = secs.filter((s) => String(s.text || '').trim()).length
  if (!wrote) {
    return { step: 3, level: 'warn', title: `目录有 ${secs.length} 章，还没开始写`, desc: '回旧版点“一键写整本”，或逐章写。', action: '回旧版写标书', tab: 'write' }
  }
  if (wrote < secs.length) {
    return { step: 3, level: 'ok', title: `已写 ${wrote}/${secs.length} 章`, desc: '还有章节没写，写完再去检查。', action: '回旧版继续写', tab: 'write' }
  }
  return { step: 4, level: 'ok', title: `${secs.length} 章都写了`, desc: '下一步检查资格、评分、废标，再导出。', action: '回旧版检查导出', tab: 'check' }
})

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
  toast.value = ''
  try {
    const [p, b, pa] = await Promise.all([
      api(`/api/projects/${pid.value}`),
      api(`/api/projects/${pid.value}/brief`).catch(() => null),
      api(`/api/projects/${pid.value}/parsing`).catch(() => ({ count: 0 })),
    ])
    project.value = p
    brief.value = b
    parsing.value = pa || { count: 0 }
    const url = new URL(location.href)
    url.searchParams.set('pid', pid.value)
    history.replaceState(null, '', url)
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

function pick() {
  if (uploading.value) return
  fileInput.value?.click()
}

async function onFile(e) {
  const f = e.target.files?.[0]
  e.target.value = ''
  if (f) await upload(f)
}

async function onDrop(e) {
  if (uploading.value) return
  const f = e.dataTransfer?.files?.[0]
  if (f) await upload(f)
}

async function upload(file) {
  if (!pid.value) return
  uploading.value = true
  error.value = ''
  toast.value = ''
  try {
    const fd = new FormData()
    fd.append('file', file)
    const up = await api('/api/upload', { method: 'POST', form: fd })
    if (!up?.text || up.usable === false) {
      throw new Error('这份文件没有解析出可用文字，请换一份可复制的 Word / PDF')
    }
    const parse = {}
    for (const k of PARSE_KEYS) {
      if (up[k] !== undefined) parse[k] = up[k]
    }
    await api(`/api/projects/${pid.value}/tender`, {
      method: 'POST',
      body: { text: up.text, filename: up.filename, parse },
    })
    await loadAll()
    toast.value = `已导入「${up.filename}」${up.effective_chars ? `（有效 ${up.effective_chars} 字）` : ''}`
  } catch (e) {
    error.value = e.message
  } finally {
    uploading.value = false
  }
}

function gotoOld(tab) {
  try {
    const old = JSON.parse(localStorage.getItem(UI_KEY) || '{}') || {}
    localStorage.setItem(UI_KEY, JSON.stringify({ ...old, pid: pid.value, tab: tab || 'materials' }))
  } catch { /* 忽略本地存储异常 */ }
  location.href = '/'
}

function goNext() {
  if (next.value.pick) return pick()
  gotoOld(next.value.tab)
}

onMounted(boot)
</script>

<template>
  <AppShell page="materials" title="资料" subtitle="上传招标文件，先看要点" :projects="projects" v-model:pid="pid" @change="loadAll">
      <ol class="steps">
        <li v-for="s in STEPS" :key="s.n" :class="{ on: s.n === next.step, done: s.n < next.step }">
          <span class="dot">{{ s.n }}</span>{{ s.label }}
        </li>
      </ol>

      <div v-if="error" class="notice bad">{{ error }}</div>
      <div v-if="loading && !project" class="card muted">正在加载…</div>
      <div v-else-if="!pid" class="card">还没有项目。请回旧版新建项目后再来。</div>

      <template v-else-if="project">
        <div class="next" :class="next.level">
          <div class="next-text">
            <b>{{ next.title }}</b>
            <div class="hint">{{ next.desc }}</div>
          </div>
          <button class="btn primary" @click="goNext">{{ next.action }}</button>
        </div>

        <section class="card">
          <div class="card-head">
            <h2>1. 传招标文件</h2>
            <span v-if="project.tender_text" class="chip ok">已导入 {{ tenderChars }} 字</span>
          </div>
          <p class="desc">把招标文件拖进来，系统自动解析并抽取要求。扫描件会标“待核对”，不会直接当依据。</p>
          <div class="drop" :class="{ busy: uploading }" @click="pick" @dragover.prevent @drop.prevent="onDrop">
            <input ref="fileInput" type="file" hidden accept=".docx,.pdf,.xlsx,.xls,.csv,.txt,.md" @change="onFile" />
            <div class="drop-plus">＋</div>
            <div class="drop-title">{{ uploading ? '正在解析并导入…' : '拖入文件，或点击选择' }}</div>
            <div class="hint">支持 Word / PDF / Excel / 文本，单份不超过 20MB</div>
          </div>
          <div v-if="tenderParseNote" class="notice warn">{{ tenderParseNote }}</div>
          <div v-if="toast" class="notice ok">{{ toast }}</div>
        </section>

        <section class="card">
          <div class="card-head">
            <h2>2. 看招标要点</h2>
            <span class="chip">按规则抽取，仍需人工核对</span>
          </div>
          <p class="desc">先看这四行，确认没有明显漏项，再去写。</p>
          <div class="grid">
            <div class="stat">
              <div class="k">硬性要求</div>
              <div class="v">{{ counts.hard || 0 }}</div>
              <div class="s">必须逐条响应</div>
            </div>
            <div class="stat">
              <div class="k">评分项</div>
              <div class="v">{{ scoringTotal }}</div>
              <div class="s">决定得分高低</div>
            </div>
            <div class="stat">
              <div class="k">废标条款</div>
              <div class="v">{{ counts.rejection || 0 }}</div>
              <div class="s">碰到就交不了</div>
            </div>
            <div class="stat" :class="{ bad: pendingCount > 0 }">
              <div class="k">待核对</div>
              <div class="v">{{ pendingCount }}</div>
              <div class="s">未核对不进依据</div>
            </div>
          </div>

        </section>

        <details class="card fold">
          <summary>展开看明细：评分项 / 货物清单 / 招标摘要</summary>
          <div class="fold-body">
            <template v-if="scoringRows.length">
              <h3>评分项（前 {{ scoringRows.length }} 项）</h3>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr><th align="left">评分项</th><th>分值</th><th align="left">说明</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="(r, i) in scoringRows" :key="i">
                      <td>{{ r.item || r.name || '—' }}</td>
                      <td class="num">{{ r.score || '—' }}</td>
                      <td class="muted">{{ r.rule || '—' }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </template>
            <p v-else class="hint">还没有抽到评分项。</p>

            <div class="fold-grid">
              <div>
                <h3>货物清单</h3>
                <p v-if="brief?.goods?.found" class="hint">
                  共 {{ brief.goods.total }} 行{{ brief.goods.missing_qty_count ? `，${brief.goods.missing_qty_count} 行没读到数量` : '' }}
                </p>
                <p v-else class="hint">未识别到货物清单。</p>
              </div>
              <div>
                <h3>最高限价</h3>
                <p class="hint">{{ brief?.goods?.cap_note || '未读到' }}</p>
              </div>
              <div>
                <h3>证照状态</h3>
                <p class="hint">
                  共 {{ brief?.certificates?.total || 0 }} 项：过期 {{ brief?.certificates?.expired || 0 }}、临期 {{ brief?.certificates?.expiring || 0 }}、日期写错 {{ brief?.certificates?.invalid || 0 }}
                </p>
              </div>
            </div>

            <template v-if="brief?.lines?.length">
              <h3>招标摘要</h3>
              <ul class="lines">
                <li v-for="(l, i) in brief.lines" :key="i">{{ l }}</li>
              </ul>
            </template>
          </div>
        </details>
      </template>
  </AppShell>
</template>
