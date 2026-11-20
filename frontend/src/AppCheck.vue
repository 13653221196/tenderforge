<script setup>
import { computed, onMounted, ref } from 'vue'
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
const readiness = ref(null)
const gate = ref(null)
const showGate = ref(false)
const gateLoading = ref(false)
const checking = ref(false)
const exporting = ref(false)
const loading = ref(true)
const error = ref('')
const toast = ref('')

const checks = computed(() => {
  const all = project.value?.checks || []
  const last = all.length ? Math.max(...all.map((c) => Number(c.round) || 0)) : 0
  return all
    .filter((c) => Number(c.round) === last)
    .sort((a, b) => (a.level === '高' ? -1 : 1) - (b.level === '高' ? -1 : 1))
})
const checkRound = computed(() => checks.value[0]?.round || 0)
const counts = computed(() => readiness.value?.counts || {})
const levelClass = computed(() => {
  const l = readiness.value?.level
  return l === 'red' ? 'bad' : l === 'yellow' ? 'warn' : 'ok'
})
const gateIssues = computed(() => (gate.value?.issues || []).slice(0, 60))

const next = computed(() => {
  const r = readiness.value
  if (!r) return { level: 'warn', title: '正在核对', desc: '稍等…', action: '重新检查', act: 'recheck' }
  if (r.level === 'red') {
    return { level: 'bad', title: r.title || '还不能交', desc: r.reason || '先处理阻断项。', action: '回正文改', act: 'write' }
  }
  if (r.level === 'yellow') {
    return { level: 'warn', title: r.title || '可以导出，但要先确认', desc: r.reason || '有提醒需要人工确认。', action: '查看提醒并导出', act: 'export' }
  }
  return { level: 'ok', title: r.title || '可以导出', desc: r.reason || '检查层面没有阻断项。', action: '导出 Word', act: 'export' }
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
  try {
    const [p, r] = await Promise.all([
      api(`/api/projects/${pid.value}`),
      api(`/api/projects/${pid.value}/readiness`).catch(() => null),
    ])
    project.value = p
    readiness.value = r
    const url = new URL(location.href)
    url.searchParams.set('pid', pid.value)
    history.replaceState(null, '', url)
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function recheck() {
  if (checking.value) return
  checking.value = true
  error.value = ''
  toast.value = ''
  try {
    const r = await api(`/api/projects/${pid.value}/check`, { method: 'POST', body: {} })
    await loadAll()
    toast.value = r.empty ? (r.reason || '还没有正文可检查')
      : (r.total ? `检查完成：${r.total} 条问题，高风险 ${r.high} 条` : '检查完成：没有发现问题')
  } catch (e) {
    error.value = e.message
  } finally {
    checking.value = false
  }
}

async function openExport() {
  if (gateLoading.value) return
  gateLoading.value = true
  error.value = ''
  try {
    gate.value = await api(`/api/projects/${pid.value}/export/gate`)
    showGate.value = true
  } catch (e) {
    error.value = e.message
  } finally {
    gateLoading.value = false
  }
}

async function doExport() {
  if (!gate.value || exporting.value) return
  exporting.value = true
  error.value = ''
  try {
    const r = await api(`/api/projects/${pid.value}/export/confirm`, {
      method: 'POST',
      body: { fingerprint: gate.value.fingerprint, acknowledge: true },
    })
    if (!r?.token) throw new Error('没有拿到导出令牌，请重新检查后再试')
    const resp = await fetch(`/api/projects/${pid.value}/export.docx?token=${encodeURIComponent(r.token)}`, {
      credentials: 'same-origin',
    })
    if (!resp.ok) {
      let msg = `导出失败 ${resp.status}`
      try {
        const j = await resp.json()
        msg = j.error || j.detail || msg
      } catch { /* 非 JSON 错误 */ }
      throw new Error(msg)
    }
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${project.value?.name || '投标文件'}.docx`
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1500)
    showGate.value = false
    toast.value = '已导出 Word；请打开后逐页人工复核，再提交投标系统'
  } catch (e) {
    error.value = e.message
  } finally {
    exporting.value = false
  }
}

function gotoOld(tab = 'check') {
  try {
    const old = JSON.parse(localStorage.getItem(UI_KEY) || '{}') || {}
    localStorage.setItem(UI_KEY, JSON.stringify({ ...old, pid: pid.value, tab }))
  } catch { /* 忽略本地存储异常 */ }
  location.href = '/'
}

function goNext() {
  const n = next.value
  if (n.act === 'recheck') return recheck()
  if (n.act === 'export') return openExport()
  gotoOld('write')
}

onMounted(boot)
</script>

<template>
  <AppShell page="check" title="检查与交付" subtitle="先看能不能交，再导出" :projects="projects" v-model:pid="pid" @change="loadAll">
      <ol class="steps">
        <li v-for="s in STEPS" :key="s.n" :class="{ on: s.n === 4, done: s.n < 4 }">
          <span class="dot">{{ s.n }}</span>{{ s.label }}
        </li>
      </ol>

      <div v-if="error" class="notice bad">{{ error }}</div>
      <div v-if="toast" class="notice ok">{{ toast }}</div>
      <div v-if="loading && !project" class="card muted">正在加载…</div>
      <div v-else-if="!pid" class="card">还没有项目。请回旧版新建项目后再来。</div>

      <template v-else-if="project">
        <div class="next" :class="next.level">
          <div class="next-text">
            <b>{{ next.title }}</b>
            <div class="hint">{{ next.desc }}</div>
          </div>
          <button class="btn primary" :disabled="gateLoading || checking" @click="goNext">
            {{ gateLoading ? '正在核对…' : next.action }}
          </button>
        </div>

        <section class="card">
          <div class="card-head">
            <h2>能不能交</h2>
            <span class="chip big" :class="levelClass">{{ readiness?.title || '还没检查' }}</span>
          </div>
          <p class="desc">{{ readiness?.reason || '还没有检查结论，先点“重新检查”。' }}</p>
          <div class="grid">
            <div class="stat"><div class="k">章节</div><div class="v">{{ counts.chapters || 0 }}</div><div class="s">当前目录</div></div>
            <div class="stat" :class="{ bad: counts.blocking > 0 }"><div class="k">必改项</div><div class="v">{{ counts.blocking || 0 }}</div><div class="s">硬性阻断</div></div>
            <div class="stat" :class="{ bad: counts.high > 0 }"><div class="k">高风险</div><div class="v">{{ counts.high || 0 }}</div><div class="s">优先核对</div></div>
            <div class="stat" :class="{ bad: counts.undeliverable > 0 }"><div class="k">未达标章</div><div class="v">{{ counts.undeliverable || 0 }}</div><div class="s">空章 / 硬伤</div></div>
          </div>

          <div v-if="readiness?.cert_hint" class="notice warn">{{ readiness.cert_hint }}</div>
          <div v-if="readiness?.asset_gap_hint" class="notice warn">{{ readiness.asset_gap_hint }}</div>
          <div class="row" style="margin-top:14px">
            <button class="btn" :disabled="checking" @click="recheck">{{ checking ? '检查中…' : '重新检查' }}</button>
            <button class="btn primary" :disabled="gateLoading" @click="openExport">{{ gateLoading ? '正在核对…' : '导出 Word' }}</button>
            <a class="btn" :href="`/vue/files.html?pid=${encodeURIComponent(pid)}`">产出文件</a>
            <button class="btn" @click="gotoOld('write')">回正文手动改</button>
            <span class="grow"></span>
            <span class="hint">{{ readiness?.hint || '导出时会再核一次。' }}</span>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <h2>检查问题</h2>
            <span class="chip">{{ checkRound ? `第 ${checkRound} 轮 · ${checks.length} 条` : '还没检查' }}</span>
          </div>
          <div v-if="!checks.length" class="empty">
            {{ checkRound ? '这一轮没有问题。' : '还没有检查结果，点上面的“重新检查”。' }}
          </div>
          <div v-else class="issue-list">
            <div v-for="(c, i) in checks.slice(0, 20)" :key="c.id || i" class="issue">
              <span class="badge" :class="c.level === '高' ? 'prot' : c.level === '中' ? 'warn' : ''">{{ c.level }}</span>
              <div class="issue-main">
                <div class="issue-title">{{ c.title }}</div>
                <div class="hint">{{ c.detail }}</div>
              </div>
              <span class="hint">{{ c.status === 'accepted' ? '已采纳' : c.status === 'ignored' ? '已忽略' : '未处理' }}</span>
            </div>
            <div v-if="checks.length > 20" class="hint">还有 {{ checks.length - 20 }} 条，回旧版查看全部。</div>
          </div>
        </section>

        <details class="card fold">
          <summary>展开看体检明细与说明</summary>
          <div class="fold-body">
            <div class="fold-grid">
              <div><h3>正文检查</h3><p class="hint">{{ readiness?.checks?.quality || '—' }}</p></div>
              <div><h3>整本体检</h3><p class="hint">{{ readiness?.health_state || '—' }}</p></div>
              <div><h3>门禁条目</h3><p class="hint">{{ readiness?.checks?.gate_items || 0 }} 项</p></div>
            </div>
            <h3>最需要处理的项</h3>
            <div v-if="readiness?.findings?.length" class="issue-list">
              <div v-for="(f, i) in readiness.findings" :key="i" class="issue">
                <span class="badge" :class="f.level === '高' ? 'prot' : f.level === '中' ? 'warn' : ''">{{ f.level }}</span>
                <div class="issue-main"><div class="issue-title">{{ f.title }}</div><div class="hint">{{ f.detail }}</div></div>
              </div>
            </div>
            <p v-else class="hint">没有重点提醒。</p>
            <p class="hint" style="margin-top:12px">
              “可以导出”只代表机器检查层面没有阻断项，不等于可以直接投标；导出前仍要逐页人工复核。
            </p>
          </div>
        </details>
      </template>

    <div v-if="showGate" class="modal-mask" @click.self="showGate = false">
      <div class="modal">
        <div class="modal-head">
          <div>
            <b>导出前提醒</b>
            <div class="hint">共 {{ gate?.issues?.length || 0 }} 项；高风险 {{ gate?.high || 0 }} 项；必改 {{ gate?.blockers?.length || 0 }} 项。</div>
          </div>
          <button class="btn mini" @click="showGate = false">关闭</button>
        </div>
        <div class="modal-body">
          <div v-if="gateIssues.length" class="issue-list">
            <div v-for="(it, i) in gateIssues" :key="i" class="issue">
              <span class="badge" :class="it.level === '高' ? 'prot' : it.level === '中' ? 'warn' : ''">{{ it.level }}</span>
              <div class="issue-main">
                <div class="issue-title">{{ it.title }}</div>
                <div class="hint">{{ it.detail }}</div>
              </div>
            </div>
            <div v-if="(gate?.issues?.length || 0) > gateIssues.length" class="hint">
              还有 {{ gate.issues.length - gateIssues.length }} 项，回旧版查看全部。
            </div>
          </div>
          <div v-else class="empty">没有导出提醒，可以直接导出。</div>
          <div class="row" style="margin-top:14px">
            <button class="btn" :disabled="exporting" @click="showGate = false">先不导出</button>
            <button class="btn primary" :disabled="exporting" @click="doExport">
              {{ exporting ? '正在导出…' : '我已知情，导出 Word' }}
            </button>
          </div>
          <p class="hint" style="margin-top:10px">
            导出会按当前内容再做一次门禁；文件生成后仍需人工逐页复核。
          </p>
        </div>
      </div>
    </div>
  </AppShell>
</template>
