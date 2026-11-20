<script setup>
import { computed } from 'vue'

const props = defineProps({
  title: { type: String, default: '' },
  subtitle: { type: String, default: '' },
  page: { type: String, default: '' },
  projects: { type: Array, default: () => [] },
  pid: { type: String, default: '' },
  showProject: { type: Boolean, default: true },
})
const emit = defineEmits(['update:pid', 'change'])

const NAV = [
  {
    group: '投标主线',
    items: [
      { key: 'home', label: '首页', page: 'home' },
      { key: 'materials', label: '资料', page: 'materials' },
      { key: 'write', label: '标书正文', page: 'write' },
      { key: 'check', label: '检查与交付', page: 'check' },
    ],
  },
  {
    group: '核对与素材',
    items: [
      { key: 'requirements', label: '招标要求表', page: 'requirements' },
      { key: 'reqmatrix', label: '逐条响应矩阵', page: 'reqmatrix' },
      { key: 'kb', label: '知识库', page: 'kb' },
    ],
  },
  {
    group: '交付',
    items: [{ key: 'files', label: '交付文件', page: 'files' }],
  },
  {
    group: '管理',
    items: [
      { key: 'settings-org', label: '企业与成员', href: '/vue/settings.html?tab=org' },
      { key: 'settings-models', label: 'AI 模型', href: '/vue/settings.html?tab=models' },
      { key: 'settings-privacy', label: '联网与预算', href: '/vue/settings.html?tab=privacy' },
      { key: 'runtime', label: '运行与诊断', page: 'runtime' },
    ],
  },
  {
    group: '其他',
    items: [{ key: 'legacy-chat', label: '对话式操作（旧版）', legacy: 'chat' }],
  },
]

const homeLink = computed(() => props.pid
  ? `/vue/home.html?pid=${encodeURIComponent(props.pid)}`
  : '/vue/home.html')

function link(item) {
  const withPid = ['home', 'materials', 'write', 'check', 'requirements', 'reqmatrix', 'files', 'kb', 'runtime'].includes(item.page)
  if (item.href) {
    if (!props.pid) return item.href
    const sep = item.href.includes('?') ? '&' : '?'
    return `${item.href}${sep}pid=${encodeURIComponent(props.pid)}`
  }
  const base = `/vue/${item.page}.html`
  return withPid && props.pid ? `${base}?pid=${encodeURIComponent(props.pid)}` : base
}

function onPid(e) {
  const v = e.target.value
  emit('update:pid', v)
  emit('change', v)
}

function openLegacy(tab) {
  try {
    const old = JSON.parse(localStorage.getItem('bidagent.ui.v1') || '{}') || {}
    localStorage.setItem('bidagent.ui.v1', JSON.stringify({ ...old, pid: props.pid || old.pid || '', tab }))
  } catch { /* 忽略本地存储异常 */ }
  location.href = '/'
}
</script>

<template>
  <div class="app-shell">
    <aside class="side">
      <a class="side-brand" :href="homeLink">
        <div class="logo">标</div>
        <div>
          <div class="side-title">TenderForge</div>
          <div class="side-sub">新版</div>
        </div>
      </a>
      <nav class="side-nav">
        <div v-for="g in NAV" :key="g.group" class="nav-group">
          <div class="nav-title">{{ g.group }}</div>
          <template v-for="it in g.items" :key="it.key">
            <button v-if="it.legacy" class="nav-item" type="button" @click="openLegacy(it.legacy)">{{ it.label }}</button>
            <a v-else class="nav-item" :class="{ on: it.key === page }" :href="link(it)">{{ it.label }}</a>
          </template>
        </div>
      </nav>
      <div class="side-foot"><a href="/">返回旧版</a></div>
    </aside>

    <div class="shell-main">
      <header class="shell-top">
        <div class="shell-heading">
          <div class="shell-title">{{ title }}</div>
          <div class="shell-sub">{{ subtitle }}</div>
        </div>
        <div class="shell-actions">
          <select v-if="showProject && projects.length" :value="pid" @change="onPid">
            <option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
          <slot name="actions" />
        </div>
      </header>
      <main class="wrap"><slot /></main>
    </div>
  </div>
</template>
