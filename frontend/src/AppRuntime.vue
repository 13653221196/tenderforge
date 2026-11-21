<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from './api'
import AppShell from './AppShell.vue'

const TABS = [
  { key: 'usage', label: '用量审计' },
  { key: 'queue', label: '运行队列' },
  { key: 'embedding', label: '向量检索' },
  { key: 'auto', label: '自动基座' },
]

const projects = ref([])
const pid = ref('')
const isOwner = ref(false)
const tab = ref('usage')
const loading = ref(true)
const error = ref('')
const toast = ref('')
const busy = ref(false)
const testing = ref(false)

const usage = ref({ rows: [], summary: {}, today_tokens: 0 })
const queue = ref({})
const emb = ref({})
const auto = ref({ stages: [], recent: [] })

const embForm = ref({
  enabled: false,
  base_url: '',
  model: '',
  dimensions: 1024,
  min_score: 0.35,
  kb_enabled: true,
  tender_enabled: true,
  api_key: '',
})

const summary = computed(() => usage.value.summary || {})
const coverage = computed(() => emb.value.coverage || {})

async function boot() {
  try {
    const st = await api('/api/state')
    projects.value = st.projects || []
    isOwner.value = !!st.is_owner
    const fromUrl = new URLSearchParams(location.search).get('pid')
    const hit = projects.value.find((p) => p.id === fromUrl)
    pid.value = hit ? hit.id : (projects.value[0]?.id || '')
    await loadAll()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function loadAll() {
  error.value = ''
  const [u, q, e, a] = await Promise.all([
    pid.value ? api(`/api/projects/${pid.value}/usage?limit=50`).catch(() => null) : null,
    api('/api/jobs/queue').catch(() => null),
    api('/api/embedding').catch(() => null),
    api(`/api/core/auto/status${pid.value ? `?pid=${encodeURIComponent(pid.value)}` : ''}`).catch(() => null),
  ])
  if (u) usage.value = u
  if (q) queue.value = q
  if (e) {
    emb.value = e
    embForm.value = {
      enabled: !!e.enabled,
      base_url: e.base_url || '',
      model: e.model || '',
      dimensions: e.dimensions || 1024,
      min_score: e.min_score ?? 0.35,
      kb_enabled: e.kb_enabled !== false,
      tender_enabled: e.tender_enabled !== false,
      api_key: '',
    }
  }
  if (a) auto.value = a
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

function saveEmbedding() {
  return run(() => api('/api/embedding', { method: 'POST', body: embForm.value }), '向量检索设置已保存')
}

async function testEmbedding() {
  if (testing.value) return
  testing.value = true
  error.value = ''
  toast.value = ''
  try {
    const r = await api('/api/embedding/test', { method: 'POST' })
    if (r.ok) toast.value = `连通正常，维度 ${r.dim || 0}，耗时 ${r.latency_ms || 0} ms`
    else error.value = `测试失败：${r.error || '未知错误'}`
  } catch (e) {
    error.value = e.message
  } finally {
    testing.value = false
  }
}

function fmt(s) {
  return String(s || '').slice(0, 16).replace('T', ' ')
}

function num(n) {
  return Number(n || 0).toLocaleString()
}

onMounted(boot)
</script>

<template>
  <AppShell page="runtime" title="运行与诊断" subtitle="用量审计、排队情况、向量检索、自动基座" :projects="projects" v-model:pid="pid" @change="loadAll">
    <div v-if="error" class="notice bad">{{ error }}</div>
    <div v-if="toast" class="notice ok">{{ toast }}</div>
    <div v-if="loading" class="card muted">正在加载…</div>

    <template v-else>
      <section class="card">
        <div class="card-head">
          <h2>运行状态</h2>
          <span class="chip">{{ isOwner ? '企业管理员' : '成员' }}</span>
        </div>
        <p class="desc">这里汇总调用记录与诊断信息；模型、企业、知识库在左侧「管理」和「核对与素材」里。</p>
        <div class="kb-tabs">
          <button v-for="t in TABS" :key="t.key" class="kb-tab" :class="{ on: tab === t.key }" @click="tab = t.key">{{ t.label }}</button>
        </div>
      </section>

      <template v-if="tab === 'usage'">
        <section class="card">
          <div class="card-head">
            <h2>用量审计</h2>
            <span class="chip">今日 {{ num(usage.today_tokens) }} token</span>
          </div>
          <div v-if="!pid" class="empty">还没有项目。</div>
          <template v-else>
            <div class="grid">
              <div class="stat"><b>{{ num(summary.calls) }}</b><span>调用次数</span></div>
              <div class="stat"><b>{{ num(summary.tin) }}</b><span>输入 token</span></div>
              <div class="stat"><b>{{ num(summary.tout) }}</b><span>输出 token</span></div>
              <div class="stat"><b>{{ Math.round(Number(summary.ms || 0) / 1000) }}s</b><span>累计耗时</span></div>
            </div>
            <div v-if="!usage.rows.length" class="empty">这个项目还没有调用记录。</div>
            <div v-else class="req-list" style="margin-top:12px">
              <div v-for="r in usage.rows" :key="r.id" class="req-row">
                <div class="req-main">
                  <div class="kb-title">
                    {{ r.task || r.actor || '调用' }}
                    <span v-if="r.ok === 0" class="badge warn">失败</span>
                  </div>
                  <div class="hint">{{ fmt(r.created_at) }} · {{ r.tool || '—' }} · {{ r.model || '—' }} · 入 {{ num(r.tokens_in) }} / 出 {{ num(r.tokens_out) }} · {{ r.elapsed_ms || 0 }} ms</div>
                </div>
              </div>
            </div>
          </template>
        </section>
      </template>

      <template v-else-if="tab === 'queue'">
        <section class="card">
          <div class="card-head">
            <h2>运行队列</h2>
            <span class="chip">{{ queue.current_workers || 1 }} 个工作线程</span>
          </div>
          <div class="grid">
            <div class="stat"><b>{{ queue.samples || 0 }}</b><span>排队样本</span></div>
            <div class="stat"><b>{{ queue.avg_wait_s || 0 }}s</b><span>平均等待</span></div>
            <div class="stat"><b>{{ queue.p95_wait_s || 0 }}s</b><span>P95 等待</span></div>
            <div class="stat"><b>{{ queue.max_wait_s || 0 }}s</b><span>最长等待</span></div>
          </div>
          <p class="desc" style="margin-top:12px">建议并发：{{ queue.suggested_workers || 1 }} 个线程。{{ queue.why || '' }}</p>
          <p v-if="queue.boundary" class="hint">{{ queue.boundary }}</p>
        </section>
      </template>

      <template v-else-if="tab === 'embedding'">
        <section class="card">
          <div class="card-head">
            <h2>向量检索</h2>
            <span class="chip" :class="emb.ready ? 'ok' : 'warn'">{{ emb.ready ? '已启用' : '纯词法' }}</span>
          </div>
          <p class="desc">{{ emb.reason || '关闭时不调用外部接口，只做本地词法检索。' }}</p>

          <div class="req-list">
            <div class="req-row">
              <div class="req-main">
                <label class="check"><input v-model="embForm.enabled" type="checkbox" :disabled="!isOwner" /> 启用向量检索</label>
                <label class="check"><input v-model="embForm.kb_enabled" type="checkbox" :disabled="!isOwner" /> 知识库素材参与召回</label>
                <label class="check"><input v-model="embForm.tender_enabled" type="checkbox" :disabled="!isOwner" /> 招标原文参与召回</label>
              </div>
            </div>
          </div>

          <div class="kb-form">
            <div class="kb-form-row">
              <input v-model="embForm.base_url" type="text" :disabled="!isOwner" placeholder="接口地址" />
              <input v-model="embForm.model" type="text" :disabled="!isOwner" placeholder="向量模型" />
              <input v-model="embForm.api_key" type="password" :disabled="!isOwner" placeholder="API Key（留空沿用已保存）" />
            </div>
            <div class="kb-form-row">
              <input v-model.number="embForm.dimensions" type="number" :disabled="!isOwner" placeholder="维度" />
              <input v-model.number="embForm.min_score" type="number" step="0.05" :disabled="!isOwner" placeholder="最低相似度" />
            </div>
            <div class="row">
              <button class="btn primary" :disabled="!isOwner || busy" @click="saveEmbedding">保存</button>
              <button class="btn" :disabled="!isOwner || testing" @click="testEmbedding">{{ testing ? '测试中…' : '测试连接' }}</button>
            </div>
          </div>

          <div class="hint" style="margin-top:12px">
            已索引 {{ num(coverage.total) }} 个片段，当前模型 {{ num(coverage.current_model) }} 个。
            <span v-if="emb.api_key_configured">密钥已配置 {{ emb.api_key_masked }}。</span>
            <span v-else>还没有配置密钥。</span>
          </div>
        </section>
      </template>

      <template v-else>
        <section class="card">
          <div class="card-head">
            <h2>自动基座</h2>
            <span class="chip" :class="auto.enabled ? 'ok' : 'warn'">{{ auto.enabled ? '自动执行中' : '已关闭' }}</span>
          </div>
          <p class="desc">导入、成稿、体检、导出前后会自动跑一组本地校验；这里只读展示，开关在「AI 模型」与「联网与预算」里。</p>
          <div class="req-list">
            <div v-for="s in auto.stages || []" :key="s.key" class="req-row">
              <div class="req-main">
                <div class="kb-title">
                  {{ s.label || s.key }}
                  <span class="chip">{{ (s.caps_detail || []).length }} 项能力</span>
                  <span v-if="s.enabled === false" class="badge warn">未启用</span>
                </div>
                <div class="hint">{{ s.trigger || '' }} {{ s.desc || '' }}</div>
                <div v-if="s.last && s.last.at" class="hint">最近：{{ fmt(s.last.at) }} · {{ s.last.summary || '' }}</div>
              </div>
            </div>
          </div>
        </section>

        <section v-if="(auto.recent || []).length" class="card">
          <div class="card-head"><h2>最近自动执行</h2><span class="hint">最多 10 条</span></div>
          <div class="req-list">
            <div v-for="(r, i) in auto.recent" :key="i" class="req-row">
              <div class="req-main">
                <div class="kb-title">{{ r.stage }}</div>
                <div class="hint">{{ fmt(r.at) }} · {{ r.summary || '' }}</div>
              </div>
            </div>
          </div>
        </section>
      </template>
    </template>
  </AppShell>
</template>
