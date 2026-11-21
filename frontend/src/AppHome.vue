<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from './api'
import AppShell from './AppShell.vue'

const projects = ref([])
const pid = ref('')
const project = ref(null)
const loading = ref(true)
const error = ref('')
const showNew = ref(false)
const newName = ref('')
const creating = ref(false)

const sections = computed(() => project.value?.sections || [])
const written = computed(() => sections.value.filter((s) => String(s.text || '').trim()).length)
const tenderChars = computed(() => String(project.value?.tender_text || '').replace(/\s/g, '').length)

const next = computed(() => {
  const p = project.value
  if (!p) return { step: 1, title: '先新建一个项目', desc: '一个投标项目建一个项目，资料和正文互不混。', action: '新建项目', act: 'new' }
  if (!String(p.tender_text || '').trim()) {
    return { step: 1, title: '先上传招标文件', desc: '上传后系统自动抽要求、排目录。', action: '去上传招标文件', act: 'materials' }
  }
  if (!sections.value.length) {
    return { step: 3, title: '生成标书目录', desc: '回正文页按招标格式生成目录，再逐章写。', action: '去生成目录', act: 'write' }
  }
  if (!String(p.outline_confirmed_at || '').trim()) {
    return { step: 3, title: '目录还没确认', desc: '确认目录后才能开始写正文。', action: '去确认目录', act: 'write' }
  }
  if (written.value < sections.value.length) {
    return { step: 3, title: `还有 ${sections.value.length - written.value} 章没写`, desc: '继续写正文，已有章节不会被覆盖。', action: '继续写正文', act: 'write' }
  }
  return { step: 4, title: '正文齐了，去检查', desc: '检查资格、评分、废标，再导出 Word。', action: '去检查导出', act: 'check' }
})

async function boot() {
  loading.value = true
  try {
    const r = await api('/api/projects?scope=active')
    projects.value = r.items || []
    const fromUrl = new URLSearchParams(location.search).get('pid')
    const hit = projects.value.find((p) => p.id === fromUrl)
    pid.value = hit ? hit.id : (projects.value[0]?.id || '')
    if (pid.value) await loadProject()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function loadProject() {
  if (!pid.value) {
    project.value = null
    return
  }
  error.value = ''
  try {
    project.value = await api(`/api/projects/${pid.value}`)
    const url = new URL(location.href)
    url.searchParams.set('pid', pid.value)
    history.replaceState(null, '', url)
  } catch (e) {
    error.value = e.message
  }
}

async function select(id) {
  if (pid.value === id) return
  pid.value = id
  await loadProject()
}

async function create() {
  const name = newName.value.trim()
  if (!name || creating.value) return
  creating.value = true
  error.value = ''
  try {
    const p = await api('/api/projects', { method: 'POST', body: { name } })
    showNew.value = false
    newName.value = ''
    await boot()
    if (p?.id) {
      pid.value = p.id
      await loadProject()
    }
  } catch (e) {
    error.value = e.message
  } finally {
    creating.value = false
  }
}

function go() {
  const n = next.value
  if (n.act === 'new') {
    showNew.value = true
    return
  }
  if (!pid.value) return
  location.href = `/vue/${n.act}.html?pid=${encodeURIComponent(pid.value)}`
}

onMounted(boot)
</script>

<template>
  <AppShell page="home" title="首页" subtitle="招标文件 → 竞标书" :projects="projects" v-model:pid="pid" @change="loadProject">
    <template #actions>
      <button class="btn primary" @click="showNew = true">＋ 新建项目</button>
    </template>
      <div v-if="error" class="notice bad">{{ error }}</div>
      <div v-if="loading && !projects.length" class="card muted">正在加载…</div>

      <section v-if="project" class="card hero">
        <div class="hero-head">
          <div>
            <div class="hero-kicker">当前项目</div>
            <div class="hero-title">{{ project.name }}</div>
            <div class="hero-next"><b>{{ next.title }}</b><span class="hint">{{ next.desc }}</span></div>
          </div>
          <button class="btn primary" @click="go">{{ next.action }}</button>
        </div>
        <ol class="steps" style="margin:16px 0 0">
          <li :class="{ on: next.step === 1, done: next.step > 1 }"><span class="dot">1</span>传文件</li>
          <li :class="{ on: next.step === 2, done: next.step > 2 }"><span class="dot">2</span>看要点</li>
          <li :class="{ on: next.step === 3, done: next.step > 3 }"><span class="dot">3</span>写竞标书</li>
          <li :class="{ on: next.step === 4 }"><span class="dot">4</span>检查导出</li>
        </ol>
      </section>

      <section v-else-if="!loading" class="card hero">
        <div class="hero-head">
          <div>
            <div class="hero-title">还没有项目</div>
            <div class="hint">新建一个项目，上传招标文件后就能开始。</div>
          </div>
          <button class="btn primary" @click="showNew = true">新建项目</button>
        </div>
      </section>

      <section v-if="project" class="card">
        <div class="card-head"><h2>三个入口</h2><span class="hint">不用记菜单，按顺序走即可</span></div>
        <div class="quick">
          <a class="quick-item" :class="{ done: tenderChars > 0 }" :href="`/vue/materials.html?pid=${encodeURIComponent(pid)}`">
            <div class="quick-no">1</div>
            <div class="quick-main"><div class="quick-title">资料</div><div class="hint">{{ tenderChars ? `已导入招标文件 ${tenderChars.toLocaleString()} 字` : '待上传招标文件' }}</div></div>
          </a>
          <a class="quick-item" :class="{ done: sections.length && written === sections.length }" :href="`/vue/write.html?pid=${encodeURIComponent(pid)}`">
            <div class="quick-no">2</div>
            <div class="quick-main"><div class="quick-title">写竞标书</div><div class="hint">{{ sections.length ? `已写 ${written}/${sections.length} 章` : '待生成目录' }}</div></div>
          </a>
          <a class="quick-item" :class="{ done: sections.length && written === sections.length }" :href="`/vue/check.html?pid=${encodeURIComponent(pid)}`">
            <div class="quick-no">3</div>
            <div class="quick-main"><div class="quick-title">检查与导出</div><div class="hint">资格 / 评分 / 废标 / Word</div></div>
          </a>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <h2>我的项目</h2>
          <button class="btn" @click="showNew = !showNew">{{ showNew ? '取消' : '＋ 新建项目' }}</button>
        </div>
        <div v-if="showNew" class="new-row">
          <input v-model="newName" type="text" placeholder="例如：某市政务云服务采购项目" @keyup.enter="create" />
          <button class="btn primary" :disabled="creating" @click="create">{{ creating ? '创建中…' : '创建' }}</button>
        </div>
        <div v-if="!projects.length" class="empty">还没有项目，点“新建项目”开始。</div>
        <div v-else class="proj-list">
          <div v-for="p in projects" :key="p.id" class="proj" :class="{ on: p.id === pid }" @click="select(p.id)">
            <div class="proj-name">{{ p.name }}</div>
            <div class="proj-meta">
              {{ p.state_label || '进行中' }} · {{ p.written || 0 }}/{{ p.sections || 0 }} 章
              <span v-if="p.updated_at"> · {{ String(p.updated_at).slice(0, 16).replace('T', ' ') }}</span>
            </div>
          </div>
        </div>
      </section>

      <p class="hint" style="text-align:center">
        需要逐条改要求？<a :href="`/vue/requirements.html?pid=${encodeURIComponent(pid)}`" style="color:var(--primary)">打开招标要求表</a>
        · 公司素材？<a href="/vue/kb.html" style="color:var(--primary)">打开知识库</a>
      </p>
  </AppShell>
</template>

<style scoped>
@media (max-width: 760px) {
  .topbar {
    flex-wrap: wrap;
    gap: 12px;
  }
  .hero-head {
    flex-direction: column;
    align-items: stretch;
  }
  .quick {
    grid-template-columns: 1fr;
  }
  .new-row {
    flex-direction: column;
  }
}
</style>
