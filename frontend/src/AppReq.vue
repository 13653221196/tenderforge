<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from './api'
import AppShell from './AppShell.vue'

const UI_KEY = 'bidagent.ui.v1'
const KINDS = [
  { key: 'hard', label: '硬性要求' },
  { key: 'rejection', label: '废标条款' },
  { key: 'scoring', label: '评分项' },
]

const projects = ref([])
const pid = ref('')
const project = ref(null)
const table = ref({ rows: [], total: 0, effective: 0 })
const loading = ref(true)
const error = ref('')
const toast = ref('')
const busy = ref(false)
const addKind = ref('hard')
const addText = ref('')
const addScore = ref('')

const labels = computed(() => table.value?.labels || { hard: '硬性要求', rejection: '废标条款', scoring: '评分项' })
const groups = computed(() => KINDS.map((k) => ({
  ...k,
  rows: (table.value?.rows || []).filter((r) => r.kind === k.key),
  effective: Number((table.value?.by_kind || {})[k.key]?.effective || 0),
})))

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
      api(`/api/projects/${pid.value}/requirements`),
    ])
    project.value = p
    table.value = r || { rows: [], total: 0, effective: 0 }
    const url = new URL(location.href)
    url.searchParams.set('pid', pid.value)
    history.replaceState(null, '', url)
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function run(fn, okMsg = '') {
  if (busy.value) return
  busy.value = true
  error.value = ''
  toast.value = ''
  try {
    await fn()
    await loadAll()
    if (okMsg) toast.value = okMsg
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

function save(row) {
  return run(async () => {
    await api(`/api/projects/${pid.value}/requirements/${row.id}`, {
      method: 'PUT',
      body: { text: row.text, score: row.score || '' },
    })
  }, '已保存')
}

function toggle(row) {
  const next = (row.status || 'active') === 'disabled' ? 'active' : 'disabled'
  return run(async () => {
    await api(`/api/projects/${pid.value}/requirements/${row.id}`, {
      method: 'PUT',
      body: { status: next },
    })
  }, next === 'disabled' ? '已停用' : '已启用')
}

function del(row) {
  if (!window.confirm('删除这一条要求？删除后重抽不会自动补回。')) return
  return run(async () => {
    await api(`/api/projects/${pid.value}/requirements/${row.id}`, { method: 'DELETE' })
  }, '已删除')
}

function groupDel(kind, label) {
  if (!window.confirm(`删除「${label}」整组要求？这一组会一起删掉。`)) return
  return run(async () => {
    await api(`/api/projects/${pid.value}/requirements/group/${kind}`, { method: 'DELETE' })
  }, '已删除整组')
}

function sync(restore = false) {
  if (restore && !window.confirm('恢复此前删除的自动要求？')) return
  return run(async () => {
    await api(`/api/projects/${pid.value}/requirements/sync`, { method: 'POST', body: { restore } })
  }, restore ? '已恢复被删要求' : '已从招标文件重抽')
}

function add() {
  const text = addText.value.trim()
  if (text.length < 2) {
    error.value = '要求内容不能为空'
    return
  }
  return run(async () => {
    await api(`/api/projects/${pid.value}/requirements`, {
      method: 'POST',
      body: { kind: addKind.value, text, score: addScore.value || '' },
    })
    addText.value = ''
    addScore.value = ''
  }, '已加入要求表')
}

onMounted(boot)
</script>

<template>
  <AppShell page="requirements" title="招标要求表" subtitle="自动初稿，人工修订优先" :projects="projects" v-model:pid="pid" @change="loadAll">
      <div v-if="error" class="notice bad">{{ error }}</div>
      <div v-if="toast" class="notice ok">{{ toast }}</div>
      <div v-if="loading && !project" class="card muted">正在加载…</div>
      <div v-else-if="!pid" class="card">还没有项目。请先新建项目。</div>

      <template v-else>
        <div class="next" :class="table.effective ? 'ok' : 'warn'">
          <div class="next-text">
            <b>生效 {{ table.effective || 0 }} / {{ table.total || 0 }} 条</b>
            <div class="hint">自动抽取只是初稿；改过的行重抽不会覆盖，停用 = 不参与判定。</div>
          </div>
          <a class="btn primary" :href="`/vue/write.html?pid=${encodeURIComponent(pid)}`">去写标书</a>
        </div>

        <section class="card">
          <div class="card-head">
            <h2>要求表</h2>
            <span class="chip">人工 {{ table.manual || 0 }} · 自动 {{ table.auto || 0 }} · 停用 {{ table.disabled || 0 }}</span>
          </div>
          <div class="row" style="margin-top:12px">
            <button class="btn" :disabled="busy" @click="sync(false)">从招标文件重抽</button>
            <button class="btn" :disabled="busy" @click="sync(true)">恢复被删要求</button>
            <a class="btn" :href="`/vue/reqmatrix.html?pid=${encodeURIComponent(pid)}`">逐条响应矩阵</a>
            <a class="btn" :href="`/vue/materials.html?pid=${encodeURIComponent(pid)}`">去资料页补材料</a>
            <span class="grow"></span>
            <span class="hint">新增/删除关联、附件核验在「资料」页做。</span>
          </div>
        </section>

        <section class="card">
          <div class="card-head"><h2>新增一条具体要求</h2></div>
          <div class="new-row">
            <select v-model="addKind" style="max-width:130px">
              <option v-for="k in KINDS" :key="k.key" :value="k.key">{{ k.label }}</option>
            </select>
            <input v-model="addText" type="text" placeholder="要求原文，例如：投标人须具备安防工程一级资质" @keyup.enter="add" />
            <input v-model="addScore" type="text" placeholder="分值" style="max-width:88px" />
            <button class="btn primary" :disabled="busy" @click="add">加入要求表</button>
          </div>
        </section>

        <section v-for="g in groups" :key="g.key" class="card">
          <div class="card-head">
            <h2>{{ labels[g.key] || g.label }} <span class="chip">{{ g.rows.length }} 条 · 生效 {{ g.effective }} 条</span></h2>
            <button class="btn mini danger" :disabled="busy" @click="groupDel(g.key, labels[g.key] || g.label)">删除这一组</button>
          </div>

          <div v-if="!g.rows.length" class="empty">这一类还没有要求，点「从招标文件重抽」或手动新增。</div>
          <div v-else class="req-list">
            <div v-for="row in g.rows" :key="row.id" class="req-row" :class="{ off: row.status === 'disabled' }">
              <div class="req-main">
                <input v-model="row.text" type="text" :disabled="row.status === 'disabled'" />
                <div class="req-sub">
                  <span class="chip">{{ row.source === 'manual' ? '人工' : '自动' }}</span>
                  <span v-if="row.status === 'disabled'" class="badge warn">已停用</span>
                  <span v-if="row.note" class="hint">{{ row.note }}</span>
                </div>
              </div>
              <input v-model="row.score" class="req-score" type="text" placeholder="分值" :disabled="row.status === 'disabled'" />
              <div class="req-acts">
                <button class="btn mini" :disabled="busy" @click="save(row)">保存</button>
                <button class="btn mini" :disabled="busy" @click="toggle(row)">{{ row.status === 'disabled' ? '启用' : '停用' }}</button>
                <button class="btn mini danger" :disabled="busy" @click="del(row)">删除</button>
              </div>
            </div>
          </div>
        </section>
      </template>
  </AppShell>
</template>
