<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from './api'
import AppShell from './AppShell.vue'

const UI_KEY = 'bidagent.ui.v1'
const KIND_LABEL = { hard: '硬性要求', rejection: '废标条款', scoring: '评分项' }
const STATUS_LABEL = {
  missing: '正文没命中',
  candidate: '仅候选响应',
  evidence_ready: '证据已齐待复核',
  confirmed: '已确认',
  na: '不适用',
  review: '新版待复核',
}

const projects = ref([])
const pid = ref('')
const project = ref(null)
const matrix = ref({ items: [], total: 0, required: 0, confirmed: 0, na: 0, pending: 0, draft_progress: 0 })
const loading = ref(true)
const error = ref('')
const toast = ref('')
const busy = ref(false)
const filter = ref('pending')
const expanded = ref('')
const naItem = ref(null)
const naReason = ref('')
const naBusy = ref(false)

const sectionMap = computed(() => {
  const m = {}
  for (const s of project.value?.sections || []) m[s.id] = s.title
  return m
})
const items = computed(() => matrix.value?.items || [])
const filtered = computed(() => {
  const f = filter.value
  if (f === 'all') return items.value
  if (f === 'confirmed') return items.value.filter((x) => x.status === 'confirmed')
  if (f === 'na') return items.value.filter((x) => x.status === 'na')
  return items.value.filter((x) => !['confirmed', 'na'].includes(x.status))
})
const progress = computed(() => Math.round(Number(matrix.value?.draft_progress || 0) * 100))

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
    const [p, m] = await Promise.all([
      api(`/api/projects/${pid.value}`),
      api(`/api/projects/${pid.value}/reqmatrix`),
    ])
    project.value = p
    matrix.value = m || {}
    const url = new URL(location.href)
    url.searchParams.set('pid', pid.value)
    history.replaceState(null, '', url)
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function run(fn, msg = '') {
  if (busy.value) return
  busy.value = true
  error.value = ''
  toast.value = ''
  try {
    await fn()
    if (msg) toast.value = msg
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

function confirmItem(it) {
  return run(async () => {
    matrix.value = await api(`/api/projects/${pid.value}/reqmatrix/${it.id}/confirm`, {
      method: 'POST',
      body: { note: '' },
    })
  }, '已确认落实')
}

function openNa(it) {
  naItem.value = it
  naReason.value = ''
}

async function submitNa() {
  if (!naItem.value || naBusy.value) return
  if (!naReason.value.trim()) {
    error.value = '标“不适用”必须写明理由'
    return
  }
  naBusy.value = true
  error.value = ''
  try {
    matrix.value = await api(`/api/projects/${pid.value}/reqmatrix/${naItem.value.id}/na`, {
      method: 'POST',
      body: { reason: naReason.value.trim() },
    })
    naItem.value = null
    toast.value = '已标不适用'
  } catch (e) {
    error.value = e.message
  } finally {
    naBusy.value = false
  }
}

function genCandidates() {
  return run(async () => {
    const r = await api(`/api/projects/${pid.value}/reqmatrix/support-candidates`, { method: 'POST', body: {} })
    await loadAll()
    toast.value = `已生成 ${r.created || 0} 条候选关联，仍需人工核对`
  })
}

function toggleDetail(id) {
  expanded.value = expanded.value === id ? '' : id
}

function statusClass(s) {
  return s === 'confirmed' ? 'ok'
    : s === 'na' ? ''
      : s === 'evidence_ready' ? 'blue'
        : s === 'missing' ? 'prot' : 'warn'
}

function kindLabel(k) {
  return KIND_LABEL[k] || k || '要求'
}

function sectionTitle(id) {
  return sectionMap.value[id] || id || ''
}

onMounted(boot)
</script>

<template>
  <AppShell page="reqmatrix" title="逐条响应矩阵" subtitle="每条要求都要有结论：已确认 / 不适用 / 待落实" :projects="projects" v-model:pid="pid" @change="loadAll">
      <div v-if="error" class="notice bad">{{ error }}</div>
      <div v-if="toast" class="notice ok">{{ toast }}</div>
      <div v-if="loading && !project" class="card muted">正在核对，要求多时需要几秒…</div>
      <div v-else-if="!pid" class="card">还没有项目。请先新建项目。</div>

      <template v-else>
        <div v-if="matrix.empty_requirements" class="notice bad">
          招标文件已导入，但一条要求都没抽到。先回要求表点「从招标文件重抽」，不要当作全部已响应。
        </div>

        <section class="card">
          <div class="card-head">
            <h2>落实情况</h2>
            <span class="chip" :class="matrix.deliverable ? 'ok' : 'bad'">
              {{ matrix.deliverable ? '必需项已全部有结论' : '还有必需要求未落实' }}
            </span>
          </div>
          <p class="desc">起草进度 {{ progress }}%，只表示写到哪儿了，不等于达标；最终看“待落实”是否为 0。</p>
          <div class="grid">
            <div class="stat"><div class="k">必需要求</div><div class="v">{{ matrix.required || 0 }}</div><div class="s">参与门禁</div></div>
            <div class="stat"><div class="k">已确认</div><div class="v">{{ matrix.confirmed || 0 }}</div><div class="s">人工复核过</div></div>
            <div class="stat"><div class="k">不适用</div><div class="v">{{ matrix.na || 0 }}</div><div class="s">已写理由</div></div>
            <div class="stat" :class="{ bad: matrix.pending > 0 }"><div class="k">待落实</div><div class="v">{{ matrix.pending || 0 }}</div><div class="s">需要处理</div></div>
          </div>
          <div class="bar"><i :style="{ width: progress + '%' }"></i></div>
        </section>

        <section class="card">
          <div class="card-head">
            <h2>要求清单</h2>
            <div class="row">
              <button class="btn mini" :disabled="busy" @click="genCandidates">生成候选关联</button>
              <a class="btn mini" :href="`/vue/requirements.html?pid=${encodeURIComponent(pid)}`">去要求表改关联</a>
            </div>
          </div>
          <div class="kb-tabs" style="margin-top:12px">
            <button class="kb-tab" :class="{ on: filter === 'pending' }" @click="filter = 'pending'">待落实<b>{{ matrix.pending || 0 }}</b></button>
            <button class="kb-tab" :class="{ on: filter === 'confirmed' }" @click="filter = 'confirmed'">已确认<b>{{ matrix.confirmed || 0 }}</b></button>
            <button class="kb-tab" :class="{ on: filter === 'na' }" @click="filter = 'na'">不适用<b>{{ matrix.na || 0 }}</b></button>
            <button class="kb-tab" :class="{ on: filter === 'all' }" @click="filter = 'all'">全部<b>{{ matrix.total || 0 }}</b></button>
          </div>

          <div v-if="!filtered.length" class="empty">
            {{ filter === 'pending' ? '没有待落实的要求。' : '这个筛选下没有条目。' }}
          </div>
          <div v-else class="req-list" style="margin-top:12px">
            <div v-for="it in filtered" :key="it.id" class="req-row">
              <div class="req-main">
                <div class="req-head">
                  <span class="badge" :class="statusClass(it.status)">{{ STATUS_LABEL[it.status] || it.status }}</span>
                  <span class="chip">{{ kindLabel(it.kind) }}</span>
                  <span v-if="it.required" class="chip">必需</span>
                  <span class="grow"></span>
                  <span class="hint">{{ it.loc }}</span>
                </div>
                <div class="req-text">{{ it.text }}</div>
                <div v-if="it.evidence" class="req-evidence">命中：{{ it.evidence }}</div>
                <div class="req-meta">
                  <span v-if="it.section_id">章节：{{ sectionTitle(it.section_id) }}</span>
                  <span v-if="it.missing_numbers && it.missing_numbers.length">缺数值：{{ it.missing_numbers.join('、') }}</span>
                  <span>关联材料 {{ (it.support_links || []).length }} 项</span>
                  <span v-if="(it.support_gaps || []).length" class="danger-text">缺口 {{ it.support_gaps.length }} 项</span>
                </div>
                <div class="req-acts" style="margin-top:8px">
                  <button class="btn mini" :disabled="busy || !it.evidence_ok || it.status === 'confirmed' || it.status === 'na'" @click="confirmItem(it)">确认已落实</button>
                  <button class="btn mini" :disabled="busy || it.status === 'na'" @click="openNa(it)">标不适用</button>
                  <button class="btn mini" @click="toggleDetail(it.id)">{{ expanded === it.id ? '收起证据' : '看证据 / 缺口' }}</button>
                  <a v-if="it.section_id" class="btn mini" :href="`/vue/write.html?pid=${encodeURIComponent(pid)}`">去这一章改</a>
                </div>
                <div v-if="expanded === it.id" class="req-detail">
                  <div v-if="(it.support_gaps || []).length">
                    <b>缺口</b>
                    <ul class="lines"><li v-for="(g, i) in it.support_gaps" :key="i">{{ g }}</li></ul>
                  </div>
                  <div v-if="(it.support_links || []).length">
                    <b>关联材料</b>
                    <ul class="lines">
                      <li v-for="(l, i) in it.support_links" :key="i">{{ l.target_type }} · {{ l.target_id }} · {{ l.relation }}</li>
                    </ul>
                  </div>
                  <p class="hint">这里只做逐条结论；补材料去「资料」页，改关联去「招标要求表」。</p>
                </div>
              </div>
            </div>
          </div>
        </section>
      </template>

    <div v-if="naItem" class="modal-mask" @click.self="naItem = null">
      <div class="modal">
        <div class="modal-head">
          <div><b>标不适用</b><div class="hint">必须写理由；这条会记录复核人和时间。</div></div>
          <button class="btn mini" @click="naItem = null">关闭</button>
        </div>
        <div class="modal-body">
          <p class="hint" style="margin-bottom:8px">{{ naItem.text }}</p>
          <textarea v-model="naReason" rows="4" placeholder="例如：本项目不涉及该资质，招标文件另有说明" style="width:100%"></textarea>
          <div class="row" style="margin-top:12px">
            <button class="btn" @click="naItem = null">取消</button>
            <button class="btn primary" :disabled="naBusy" @click="submitNa">{{ naBusy ? '提交中…' : '确认不适用' }}</button>
          </div>
        </div>
      </div>
    </div>
  </AppShell>
</template>
