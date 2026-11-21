<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from './api'
import AppShell from './AppShell.vue'

const KIND_LABEL = {
  deliverable: '交付件',
  'acknowledged-draft': '知情导出草稿',
  review: '审阅件',
  bundle: '投标包',
  docx: 'Word',
}

const projects = ref([])
const pid = ref('')
const project = ref(null)
const artifacts = ref([])
const batches = ref([])
const loading = ref(true)
const error = ref('')
const toast = ref('')
const busy = ref(false)
const tab = ref('files')

const files = computed(() => artifacts.value || [])
const currentFiles = computed(() => files.value.filter((a) => a.is_current || !a.is_history))
const historyFiles = computed(() => files.value.filter((a) => a.is_history))

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
    const [p, a, v] = await Promise.all([
      api(`/api/projects/${pid.value}`),
      api(`/api/projects/${pid.value}/artifacts`).catch(() => ({ items: [] })),
      api(`/api/projects/${pid.value}/versions`).catch(() => ({ batches: [] })),
    ])
    project.value = p
    artifacts.value = a.items || []
    batches.value = v.batches || []
    const url = new URL(location.href)
    url.searchParams.set('pid', pid.value)
    history.replaceState(null, '', url)
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function restore(batch) {
  if (busy.value) return
  if (!window.confirm(`恢复「${batch.reason || '这一批'}」？会覆盖当前章节正文。`)) return
  busy.value = true
  error.value = ''
  toast.value = ''
  try {
    const r = await api(`/api/projects/${pid.value}/versions/${encodeURIComponent(batch.batch_id)}/restore`, {
      method: 'POST',
      body: {},
    })
    await loadAll()
    toast.value = r.partial ? `部分恢复：${r.note || '有章节跳过'}`
      : `已恢复「${batch.reason || '这一批'}」；其他窗口的旧稿会失效`
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

function kindLabel(k) {
  return KIND_LABEL[k] || k || '文件'
}

function kindClass(k) {
  return k === 'deliverable' ? 'ok' : k === 'acknowledged-draft' ? 'warn' : ''
}

function downloadUrl(a) {
  return `/api/projects/${encodeURIComponent(pid.value)}/artifacts/${encodeURIComponent(a.id)}/download`
}

function fmt(s) {
  return String(s || '').slice(0, 16).replace('T', ' ')
}

onMounted(boot)
</script>

<template>
  <AppShell page="files" title="交付文件" subtitle="导出过什么、是哪一个版本、能不能回退" :projects="projects" v-model:pid="pid" @change="loadAll">
      <div v-if="error" class="notice bad">{{ error }}</div>
      <div v-if="toast" class="notice ok">{{ toast }}</div>
      <div v-if="loading && !project" class="card muted">正在加载…</div>
      <div v-else-if="!pid" class="card">还没有项目。请先新建项目。</div>

      <template v-else>
        <section class="card">
          <div class="card-head">
            <h2>交付记录</h2>
            <span class="chip">{{ files.length }} 个文件 · {{ batches.length }} 个快照</span>
          </div>
          <p class="desc">这里只列已登记的文件；同名旧版本会标成历史版本，不是当前定稿。</p>
          <div class="kb-tabs" style="margin-top:12px">
            <button class="kb-tab" :class="{ on: tab === 'files' }" @click="tab = 'files'">产出文件<b>{{ files.length }}</b></button>
            <button class="kb-tab" :class="{ on: tab === 'versions' }" @click="tab = 'versions'">版本回退<b>{{ batches.length }}</b></button>
          </div>
        </section>

        <template v-if="tab === 'files'">
          <div v-if="!files.length" class="card empty">
            还没有登记过导出文件。去「检查与交付」导出一次 Word，这里就会出现记录。
          </div>
          <section v-for="a in files" :key="a.id" class="kb-item">
            <div class="kb-row">
              <div class="kb-main">
                <div class="kb-title">
                  <span class="badge" :class="kindClass(a.kind)">{{ kindLabel(a.kind) }}</span>
                  {{ a.name }}
                  <span v-if="a.is_history" class="badge warn">历史版本</span>
                  <span v-if="a.status === 'missing'" class="badge prot">文件缺失</span>
                </div>
                <div class="hint">
                  {{ a.fmt || '' }} · v{{ a.version || 1 }} · {{ fmt(a.created_at) }}
                  <span v-if="a.size"> · {{ Math.round(Number(a.size) / 1024) }} KB</span>
                </div>
              </div>
              <div class="req-acts">
                <a v-if="a.status !== 'missing'" class="btn mini primary" :href="downloadUrl(a)">下载</a>
                <span v-else class="hint">文件已被清理，不能下载</span>
              </div>
            </div>
          </section>
        </template>

        <template v-else>
          <div v-if="!batches.length" class="card empty">
            还没有章节快照。整本生成、单章生成/改写、优化都会先存快照。
          </div>
          <section v-for="b in batches" :key="b.batch_id" class="kb-item">
            <div class="kb-row">
              <div class="kb-main">
                <div class="kb-title">{{ b.reason || '章节快照' }}</div>
                <div class="hint">{{ b.sections || 0 }} 章 · {{ fmt(b.created_at) }}</div>
              </div>
              <div class="req-acts">
                <button class="btn mini danger" :disabled="busy" @click="restore(b)">恢复这一批</button>
              </div>
            </div>
          </section>
          <div class="hint" style="margin-top:8px">
            恢复只影响这一批章节；会推进版本号，其他窗口手里的旧稿会立即失效。
          </div>
        </template>
      </template>
  </AppShell>
</template>
