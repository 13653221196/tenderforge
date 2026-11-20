<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from './api'
import AppShell from './AppShell.vue'

const TABS = ['org', 'models', 'privacy']
const tabs = [
  { key: 'org', label: '企业与成员' },
  { key: 'models', label: 'AI 模型' },
  { key: 'privacy', label: '联网与预算' },
]

const me = ref({})
const isOwner = ref(false)
const org = ref({})
const members = ref([])
const stats = ref({})
const orgName = ref('')
const models = ref({ active: '', roles: {}, role_labels: {}, models: [], presets: [] })
const privacy = ref({ allow_outbound: false, scrub_sensitive: true, max_chars: 12000 })
const budgetData = ref({ budget: { enabled: true, daily_token_limit: 1000000, per_task_token_limit: 200000 }, today_tokens: 0 })
const tab = ref('org')
const loading = ref(true)
const error = ref('')
const toast = ref('')
const busy = ref(false)
const testing = ref('')
const showNewUser = ref(false)
const showNewModel = ref(false)
const newUser = ref({ username: '', name: '', password: '', role: 'member' })
const newModel = ref({ label: '', provider: 'custom', base_url: '', model: '', api_key: '', max_tokens: 4096, timeout: 120 })

const activeModelLabel = computed(() => {
  const m = (models.value.models || []).find((x) => x.id === models.value.active)
  return m ? m.label : '未选择模型'
})

const pageKey = computed(() => `settings-${tab.value}`)
const activeTabLabel = computed(() => (tabs.find((t) => t.key === tab.value) || tabs[0]).label)

function setTab(key) {
  tab.value = key
  const url = new URL(location.href)
  url.searchParams.set('tab', key)
  history.replaceState(null, '', url)
}

async function boot() {
  const fromUrl = new URLSearchParams(location.search).get('tab')
  if (TABS.includes(fromUrl)) tab.value = fromUrl
  loading.value = true
  try {
    const st = await api('/api/state')
    me.value = st.me || {}
    isOwner.value = !!st.is_owner
    await loadAll()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function loadAll() {
  error.value = ''
  const [o, m, p, b] = await Promise.all([
    api('/api/org').catch(() => null),
    api('/api/models').catch(() => null),
    api('/api/privacy').catch(() => null),
    api('/api/budget').catch(() => null),
  ])
  if (o) {
    org.value = o.org || {}
    orgName.value = o.org?.name || ''
    me.value = o.me || me.value
    members.value = o.members || []
    stats.value = o.stats || {}
  }
  if (m) models.value = m
  if (p) privacy.value = { ...privacy.value, ...p }
  if (b) budgetData.value = { ...budgetData.value, ...b, budget: { ...budgetData.value.budget, ...(b.budget || {}) } }
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

function saveOrgName() {
  return run(async () => {
    await api('/api/org', { method: 'PUT', body: { name: orgName.value.trim() } })
  }, '企业名称已保存')
}

function addUser() {
  const u = newUser.value
  if (!u.username.trim() || u.password.length < 8) {
    error.value = '账号不能为空，密码至少 8 位'
    return
  }
  return run(async () => {
    await api('/api/org/users', {
      method: 'POST',
      body: {
        username: u.username.trim(),
        name: u.name.trim(),
        password: u.password,
        role: u.role,
      },
    })
    newUser.value = { username: '', name: '', password: '', role: 'member' }
    showNewUser.value = false
  }, '成员已添加')
}

function setRole(u, role) {
  return run(() => api(`/api/org/users/${u.id}`, { method: 'PUT', body: { role } }), role === 'owner' ? '已设为管理员' : '已降为成员')
}

function toggleUser(u) {
  const status = u.status === 'disabled' ? 'active' : 'disabled'
  return run(() => api(`/api/org/users/${u.id}`, { method: 'PUT', body: { status } }), status === 'active' ? '已启用' : '已停用')
}

function resetPwd(u) {
  const pw = window.prompt(`给「${u.name || u.username}」设置新密码（至少 8 位）`)
  if (!pw) return
  if (pw.length < 8) {
    error.value = '密码至少 8 位'
    return
  }
  return run(() => api(`/api/org/users/${u.id}`, { method: 'PUT', body: { password: pw } }), '密码已重置')
}

function delUser(u) {
  if (!window.confirm(`删除成员「${u.name || u.username}」？`)) return
  return run(() => api(`/api/org/users/${u.id}`, { method: 'DELETE' }), '成员已删除')
}

function activateModel(m) {
  return run(() => api('/api/models/active', { method: 'POST', body: { id: m.id } }), `当前模型已切到「${m.label}」`)
}

async function testModel(m) {
  if (testing.value) return
  testing.value = m.id
  error.value = ''
  toast.value = ''
  try {
    const r = await api(`/api/models/${m.id}/test`, { method: 'POST' })
    if (r.ok) toast.value = `「${m.label}」连接正常，耗时 ${r.latency_ms || 0} ms`
    else error.value = `「${m.label}」测试失败：${r.error || '未知错误'}`
  } catch (e) {
    error.value = e.message
  } finally {
    testing.value = ''
  }
}

function delModel(m) {
  if (!window.confirm(`删除模型「${m.label}」？`)) return
  return run(() => api(`/api/models/${m.id}`, { method: 'DELETE' }), '模型已删除')
}

function addModel() {
  const m = newModel.value
  if (!m.base_url.trim() || !m.model.trim() || !m.api_key.trim()) {
    error.value = '地址、模型名、密钥都要填'
    return
  }
  return run(async () => {
    await api('/api/models', {
      method: 'POST',
      body: {
        label: m.label.trim() || m.model.trim(),
        provider: m.provider,
        base_url: m.base_url.trim(),
        model: m.model.trim(),
        api_key: m.api_key.trim(),
        max_tokens: Number(m.max_tokens) || 4096,
        timeout: Number(m.timeout) || 120,
      },
    })
    newModel.value = { label: '', provider: 'custom', base_url: '', model: '', api_key: '', max_tokens: 4096, timeout: 120 }
    showNewModel.value = false
  }, '模型已添加')
}

function setRoleModel(role, id) {
  return run(() => api('/api/models/role', { method: 'POST', body: { role, id } }), '环节模型已更新')
}

function savePrivacy() {
  return run(() => api('/api/privacy', {
    method: 'POST',
    body: {
      allow_outbound: !!privacy.value.allow_outbound,
      scrub_sensitive: !!privacy.value.scrub_sensitive,
      max_chars: Number(privacy.value.max_chars) || 12000,
    },
  }), '联网设置已保存')
}

function saveBudget() {
  return run(() => api('/api/budget', {
    method: 'POST',
    body: {
      enabled: !!budgetData.value.budget.enabled,
      daily_token_limit: Number(budgetData.value.budget.daily_token_limit) || 1000000,
      per_task_token_limit: Number(budgetData.value.budget.per_task_token_limit) || 200000,
    },
  }), '预算设置已保存')
}

function gotoOld() {
  try {
    const old = JSON.parse(localStorage.getItem('bidagent.ui.v1') || '{}') || {}
    localStorage.setItem('bidagent.ui.v1', JSON.stringify({ ...old, tab: 'settings' }))
  } catch { /* 忽略本地存储异常 */ }
  location.href = '/'
}

onMounted(boot)
</script>

<template>
  <AppShell :page="pageKey" :title="activeTabLabel" subtitle="企业、成员、模型、联网与预算" :show-project="false">
    <template #actions>
      <button class="btn" @click="gotoOld">返回旧版</button>
    </template>

    <div v-if="error" class="notice bad">{{ error }}</div>
    <div v-if="toast" class="notice ok">{{ toast }}</div>
    <div v-if="loading" class="card muted">正在加载…</div>

    <template v-else>
      <section class="card">
        <div class="card-head">
          <h2>当前账号</h2>
          <span class="chip">{{ isOwner ? '企业管理员' : '成员' }}</span>
        </div>
        <p class="desc">{{ me.name || me.username }} · {{ me.username }} · 当前模型：{{ activeModelLabel }}</p>
        <div class="kb-tabs">
          <button v-for="t in tabs" :key="t.key" class="kb-tab" :class="{ on: tab === t.key }" @click="setTab(t.key)">{{ t.label }}</button>
        </div>
      </section>


        <template v-if="tab === 'org'">
          <section class="card">
            <div class="card-head"><h2>企业信息</h2><span class="chip">{{ stats.members || 0 }} 人 · {{ stats.projects || 0 }} 个项目 · {{ stats.kb || 0 }} 篇知识库</span></div>
            <div class="new-row">
              <input v-model="orgName" type="text" :disabled="!isOwner" placeholder="企业名称" />
              <button class="btn primary" :disabled="!isOwner || busy" @click="saveOrgName">保存名称</button>
            </div>
          </section>

          <section class="card">
            <div class="card-head">
              <h2>成员</h2>
              <button class="btn" :disabled="!isOwner" @click="showNewUser = !showNewUser">{{ showNewUser ? '取消' : '＋ 添加成员' }}</button>
            </div>
            <div v-if="showNewUser" class="kb-form">
              <div class="kb-form-row">
                <input v-model="newUser.username" type="text" placeholder="登录账号，例如 zhangsan" />
                <input v-model="newUser.name" type="text" placeholder="显示名" />
                <input v-model="newUser.password" type="password" placeholder="初始密码（至少 8 位）" />
                <select v-model="newUser.role">
                  <option value="member">成员</option>
                  <option value="owner">管理员</option>
                </select>
              </div>
              <div class="row"><button class="btn primary" :disabled="busy" @click="addUser">添加</button></div>
            </div>

            <div class="req-list" style="margin-top:12px">
              <div v-for="u in members" :key="u.id" class="req-row">
                <div class="req-main">
                  <div class="kb-title">
                    {{ u.name || u.username }}
                    <span class="chip">{{ u.role === 'owner' ? '管理员' : '成员' }}</span>
                    <span v-if="u.status === 'disabled'" class="badge warn">已停用</span>
                  </div>
                  <div class="hint">{{ u.username }} · {{ String(u.created_at || '').slice(0, 16).replace('T', ' ') }}</div>
                </div>
                <div v-if="isOwner" class="req-acts">
                  <button class="btn mini" :disabled="busy" @click="setRole(u, u.role === 'owner' ? 'member' : 'owner')">{{ u.role === 'owner' ? '降为成员' : '设为管理员' }}</button>
                  <button class="btn mini" :disabled="busy" @click="toggleUser(u)">{{ u.status === 'disabled' ? '启用' : '停用' }}</button>
                  <button class="btn mini" :disabled="busy" @click="resetPwd(u)">重置密码</button>
                  <button v-if="u.id !== me.id" class="btn mini danger" :disabled="busy" @click="delUser(u)">删除</button>
                </div>
              </div>
            </div>
          </section>
        </template>

        <template v-else-if="tab === 'models'">
          <section class="card">
            <div class="card-head">
              <h2>当前模型</h2>
              <span class="chip" :class="models.active ? 'ok' : 'warn'">{{ activeModelLabel }}</span>
            </div>
            <p class="desc">模型可随时切换；不配模型时只能用本地演示模板，不能作为投标依据。</p>
            <div v-if="!models.models.length" class="empty">还没有登记模型。点下面“添加模型”。</div>
            <div class="req-list" style="margin-top:12px">
              <div v-for="m in models.models" :key="m.id" class="req-row">
                <div class="req-main">
                  <div class="kb-title">
                    {{ m.label }}
                    <span class="chip">{{ m.provider }}</span>
                    <span v-if="m.id === models.active" class="badge ok">当前</span>
                    <span v-if="!m.ready" class="badge warn">配置不全</span>
                  </div>
                  <div class="hint">{{ m.model }} · {{ m.base_url }} · 密钥 {{ m.key_hint }}</div>
                </div>
                <div class="req-acts">
                  <button class="btn mini" :disabled="!isOwner || busy || m.id === models.active" @click="activateModel(m)">设为当前</button>
                  <button class="btn mini" :disabled="!isOwner || !!testing" @click="testModel(m)">{{ testing === m.id ? '测试中…' : '测试' }}</button>
                  <button class="btn mini danger" :disabled="!isOwner || busy" @click="delModel(m)">删除</button>
                </div>
              </div>
            </div>
            <div class="row" style="margin-top:12px">
              <button class="btn" :disabled="!isOwner" @click="showNewModel = !showNewModel">{{ showNewModel ? '取消' : '＋ 添加模型' }}</button>
            </div>
            <div v-if="showNewModel" class="kb-form">
              <div class="kb-form-row">
                <input v-model="newModel.label" type="text" placeholder="显示名，例如 通义千问" />
                <input v-model="newModel.base_url" type="text" placeholder="接口地址，例如 https://.../v1" />
                <input v-model="newModel.model" type="text" placeholder="模型名，例如 qwen-plus" />
              </div>
              <div class="kb-form-row">
                <input v-model="newModel.api_key" type="password" placeholder="API Key（只存服务器）" />
                <input v-model.number="newModel.max_tokens" type="number" placeholder="max_tokens" />
                <input v-model.number="newModel.timeout" type="number" placeholder="超时秒" />
              </div>
              <div class="row"><button class="btn primary" :disabled="!isOwner || busy" @click="addModel">保存模型</button></div>
            </div>
          </section>

          <section class="card">
            <div class="card-head"><h2>环节分工</h2><span class="hint">不选就跟随当前模型</span></div>
            <div class="role-row" v-for="(label, role) in models.role_labels" :key="role">
              <span>{{ label }}</span>
              <select :value="models.roles[role] || ''" :disabled="!isOwner" @change="setRoleModel(role, $event.target.value)">
                <option value="">跟随当前模型</option>
                <option v-for="m in models.models" :key="m.id" :value="m.id">{{ m.label }}</option>
              </select>
            </div>
          </section>
        </template>

        <template v-else>
          <section class="card">
            <div class="card-head"><h2>联网与隐私</h2><span class="chip" :class="privacy.outbound_allowed ? 'warn' : 'ok'">{{ privacy.outbound_allowed ? '允许联网' : '仅本机处理' }}</span></div>
            <p class="desc">{{ privacy.note || '关闭联网后不再调用外部模型，任何标书内容都不会外发。' }}</p>
            <div class="req-row">
              <div class="req-main">
                <label class="check"><input v-model="privacy.allow_outbound" type="checkbox" :disabled="!isOwner" /> 允许调用外部 AI 模型</label>
                <label class="check"><input v-model="privacy.scrub_sensitive" type="checkbox" :disabled="!isOwner" /> 自动脱敏身份证、银行账号、手机号、邮箱</label>
                <label class="check">单次发送上限 <input v-model.number="privacy.max_chars" type="number" :disabled="!isOwner" style="width:110px" /> 字</label>
              </div>
              <button class="btn primary" :disabled="!isOwner || busy" @click="savePrivacy">保存</button>
            </div>
          </section>

          <section class="card">
            <div class="card-head"><h2>预算</h2><span class="hint">今日已用 {{ (budgetData.today_tokens || 0).toLocaleString() }} token</span></div>
            <div class="req-row">
              <div class="req-main">
                <label class="check"><input v-model="budgetData.budget.enabled" type="checkbox" :disabled="!isOwner" /> 启用预算限制</label>
                <label class="check">每日上限 <input v-model.number="budgetData.budget.daily_token_limit" type="number" :disabled="!isOwner" style="width:150px" /> token</label>
                <label class="check">单任务上限 <input v-model.number="budgetData.budget.per_task_token_limit" type="number" :disabled="!isOwner" style="width:150px" /> token</label>
              </div>
              <button class="btn primary" :disabled="!isOwner || busy" @click="saveBudget">保存</button>
            </div>
          </section>
        </template>

        <p class="hint" style="text-align:center">
          向量检索与自动基座的状态在左侧「运行与诊断」里查看。
        </p>
      </template>
  </AppShell>
</template>
