<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from './api'
import AppShell from './AppShell.vue'

const UI_KEY = 'bidagent.ui.v1'
const KIND_LABEL = { material: '通用素材', sample: '标书范例' }

const docs = ref([])
const counts = ref({ all: 0, material: 0, sample: 0 })
const loading = ref(true)
const error = ref('')
const toast = ref('')
const busy = ref(false)
const q = ref('')
const filter = ref('all')
const openId = ref('')
const editingId = ref('')
const chapters = ref({})
const showNew = ref(false)
const pid = ref('')
const edit = ref({ title: '', kind: 'material', valid_until: '', content: '' })
const newDoc = ref({ title: '', kind: 'material', valid_until: '', content: '' })

const filtered = computed(() => {
  const kw = q.value.trim().toLowerCase()
  return docs.value.filter((d) => {
    const kind = d.kind === 'sample' ? 'sample' : 'material'
    if (filter.value !== 'all' && kind !== filter.value) return false
    if (!kw) return true
    return String(d.title || '').toLowerCase().includes(kw)
      || String(d.content || '').toLowerCase().includes(kw)
  })
})
const totalChars = computed(() => docs.value.reduce((n, d) => n + String(d.content || '').length, 0))

async function boot() {
  loading.value = true
  try {
    pid.value = new URLSearchParams(location.search).get('pid') || ''
    await loadAll()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function loadAll() {
  error.value = ''
  const r = await api('/api/kb')
  docs.value = r.docs || []
  counts.value = r.counts || { all: docs.value.length, material: 0, sample: 0 }
}

async function run(fn, msg = '') {
  if (busy.value) return
  busy.value = true
  error.value = ''
  toast.value = ''
  try {
    await fn()
    await loadAll()
    if (msg) toast.value = msg
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

function startEdit(d) {
  editingId.value = d.id
  openId.value = ''
  edit.value = {
    title: d.title || '',
    kind: d.kind === 'sample' ? 'sample' : 'material',
    valid_until: String(d.valid_until || '').slice(0, 10),
    content: d.content || '',
  }
}

function saveEdit(id) {
  if (!edit.value.title.trim()) {
    error.value = '名称不能为空'
    return
  }
  return run(async () => {
    await api(`/api/kb/${id}`, {
      method: 'PUT',
      body: {
        title: edit.value.title.trim(),
        kind: edit.value.kind,
        valid_until: edit.value.valid_until || '',
        content: edit.value.content,
      },
    })
    editingId.value = ''
  }, '已保存')
}

function toggleView(d) {
  openId.value = openId.value === d.id ? '' : d.id
  editingId.value = ''
  if (openId.value === d.id && d.kind === 'sample' && !chapters.value[d.id]) loadChapters(d.id)
}

async function loadChapters(id) {
  try {
    const r = await api(`/api/kb/${id}/chapters`)
    chapters.value = { ...chapters.value, [id]: r.chapters || [] }
  } catch (e) {
    error.value = e.message
  }
}

function del(d) {
  if (!window.confirm(`删除「${d.title}」？删除后不能恢复。`)) return
  return run(() => api(`/api/kb/${d.id}`, { method: 'DELETE' }), '已删除')
}

function add() {
  if (!newDoc.value.title.trim()) {
    error.value = '名称不能为空'
    return
  }
  return run(async () => {
    await api('/api/kb', {
      method: 'POST',
      body: {
        title: newDoc.value.title.trim(),
        kind: newDoc.value.kind,
        valid_until: newDoc.value.valid_until || '',
        content: newDoc.value.content,
      },
    })
    newDoc.value = { title: '', kind: 'material', valid_until: '', content: '' }
    showNew.value = false
  }, '已加入知识库')
}

function kindOf(d) {
  return d.kind === 'sample' ? 'sample' : 'material'
}

function fmtDate(s) {
  return String(s || '').slice(0, 10)
}

onMounted(boot)
</script>

<template>
  <AppShell page="kb" title="知识库" subtitle="公司素材 + 标书范例，跨项目复用" :pid="pid" :show-project="false">
      <div v-if="error" class="notice bad">{{ error }}</div>
      <div v-if="toast" class="notice ok">{{ toast }}</div>

      <section class="card">
        <div class="card-head">
          <h2>知识库 <span class="chip">{{ counts.all || 0 }} 篇 · {{ totalChars.toLocaleString() }} 字</span></h2>
          <button class="btn primary" @click="showNew = !showNew">{{ showNew ? '取消' : '＋ 新建' }}</button>
        </div>
        <p class="desc">范例是整本标书，写某一章时参考同章结构；通用素材是资质、业绩、承诺等片段，写哪儿都能引用。</p>
        <div class="kb-tools">
          <input v-model="q" type="text" placeholder="搜标题或正文…" />
          <div class="kb-tabs">
            <button class="kb-tab" :class="{ on: filter === 'all' }" @click="filter = 'all'">全部<b>{{ counts.all || 0 }}</b></button>
            <button class="kb-tab" :class="{ on: filter === 'material' }" @click="filter = 'material'">通用素材<b>{{ counts.material || 0 }}</b></button>
            <button class="kb-tab" :class="{ on: filter === 'sample' }" @click="filter = 'sample'">标书范例<b>{{ counts.sample || 0 }}</b></button>
          </div>
        </div>
      </section>

      <section v-if="showNew" class="card">
        <div class="card-head"><h2>新建一篇</h2><span class="hint">粘贴正文即可保存</span></div>
        <div class="kb-form">
          <div class="kb-form-row">
            <input v-model="newDoc.title" type="text" placeholder="名称，例如：营业执照 / 某项目中标标书" />
            <select v-model="newDoc.kind">
              <option value="material">通用素材</option>
              <option value="sample">标书范例</option>
            </select>
            <input v-model="newDoc.valid_until" type="date" title="有效期（只给证照类填）" />
          </div>
          <textarea v-model="newDoc.content" rows="7" placeholder="粘贴正文…"></textarea>
          <div class="row"><button class="btn primary" :disabled="busy" @click="add">保存到知识库</button></div>
        </div>
      </section>

      <div v-if="loading && !docs.length" class="card muted">正在加载…</div>
      <div v-else-if="!filtered.length" class="card empty">
        {{ docs.length ? '没有符合条件的条目，换个词或切到「全部」。' : '知识库还是空的。点上面「新建」存一篇，或从已写完的标书里收进来。' }}
      </div>

      <section v-for="d in filtered" :key="d.id" class="kb-item">
        <div class="kb-row">
          <div class="kb-main">
            <div class="kb-title">
              <span class="badge" :class="kindOf(d) === 'sample' ? 'ok' : ''">{{ KIND_LABEL[kindOf(d)] }}</span>
              {{ d.title }}
            </div>
            <div class="hint">
              {{ String(d.content || '').length.toLocaleString() }} 字
              <span v-if="kindOf(d) === 'sample'"> · {{ d.n_top || 0 }} 章</span>
              <span v-if="d.valid_until"> · 有效期至 {{ fmtDate(d.valid_until) }}</span>
              <span v-if="d.created_at"> · {{ fmtDate(d.created_at) }}</span>
            </div>
          </div>
          <div class="req-acts">
            <button class="btn mini" @click="toggleView(d)">{{ openId === d.id ? '收起' : '查看' }}</button>
            <button v-if="kindOf(d) === 'sample'" class="btn mini" @click="loadChapters(d.id)">看目录</button>
            <button class="btn mini" @click="startEdit(d)">编辑</button>
            <button class="btn mini danger" :disabled="busy" @click="del(d)">删除</button>
          </div>
        </div>

        <div v-if="editingId === d.id" class="kb-form">
          <div class="kb-form-row">
            <input v-model="edit.title" type="text" />
            <select v-model="edit.kind">
              <option value="material">通用素材</option>
              <option value="sample">标书范例</option>
            </select>
            <input v-model="edit.valid_until" type="date" title="有效期（只给证照类填）" />
          </div>
          <textarea v-model="edit.content" rows="9"></textarea>
          <div class="row">
            <button class="btn primary" :disabled="busy" @click="saveEdit(d.id)">保存</button>
            <button class="btn" @click="editingId = ''">取消</button>
          </div>
        </div>

        <template v-else>
          <pre v-if="openId === d.id" class="kb-view">{{ d.content || '（这一篇没有正文）' }}</pre>
          <div v-if="chapters[d.id]" class="kb-chaps">
            <div v-for="(c, i) in chapters[d.id]" :key="i" class="kb-chap">
              <span class="hint">{{ i + 1 }}</span><span>{{ c.title }}</span>
            </div>
          </div>
        </template>
      </section>

      <p class="hint" style="text-align:center">
        需要「用它建目录 / 设为参照」？在「标书正文」里选素材即可。
      </p>
  </AppShell>
</template>
