<template>
  <section class="page" data-module="tagmgr">
    <header class="page-head">
      <div>
        <h2>标签管理管理</h2>
        <p class="page-desc">
          管理业务标签的定义与计算逻辑；发布新版本会触发引用该标签的分群重算，
          并记录版本差异审计。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记标签定义</button>
        <button class="btn" type="button" @click="exportRows">导出标签管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>标签编号</span>
        <input v-model="filters.keyword" placeholder="按标签编号检索，如 TAGM-0003" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ display(row, column) }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="publish(row)">发布新版本</button>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无标签管理数据，可先登记标签定义</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条标签管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
    <p v-if="message" class="message-text">{{ message }}</p>

    <section class="detail-panel">
      <header class="detail-head">
        <h3>版本差异审计</h3>
        <span class="detail-sub">发布新版本时记录从哪个版本到哪个版本、影响了哪些分群与重算批次</span>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>标签编号</th><th>从版本</th><th>到版本</th><th>发布批次</th>
            <th>发布时间</th><th>版本差异</th><th>影响分群</th><th>重算批次</th><th>结果摘要</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="audit in audits" :key="String(audit.id)">
            <td>{{ audit.标签编号 }}</td>
            <td>{{ audit.从版本 }}</td>
            <td>{{ audit.到版本 }}</td>
            <td>{{ display(audit, '发布批次') }}</td>
            <td>{{ audit.发布时间 }}</td>
            <td>{{ formatDiff(audit.版本差异) }}</td>
            <td>{{ joinList(audit.影响分群) }}</td>
            <td>{{ joinList(audit.重算批次) }}</td>
            <td>{{ formatSummary(audit.结果摘要) }}</td>
          </tr>
          <tr v-if="!audits.length">
            <td colspan="9" class="empty-state">还没有版本发布记录</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/tagmgr'
const columns = [
  '标签编号', '标签名称', '标签分类', '版本号', '覆盖对象数', '刷新周期',
  '最近计算时间', '引用分群',
]
const actions = ['提交逻辑评审', '下线标签']
const statuses = ['草稿', '逻辑评审中', '已发布', '已下线']

const rows = ref<Row[]>([])
const audits = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const message = ref('')
const filters = ref<{ keyword: string; status: string }>({ keyword: '', status: '' })
const stats = ref([
  { label: '标签定义总数', value: 0 },
  { label: '已发布标签', value: 0 },
  { label: '被分群引用', value: 0 },
])

function display(row: Row, column: string): string {
  const value = row[column]
  if (Array.isArray(value)) return joinList(value)
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

function joinList(value: unknown): string {
  if (!Array.isArray(value) || !value.length) return '—'
  return value
    .map((item) => (typeof item === 'object' && item !== null ? String(item.分群编号 ?? '') : String(item)))
    .filter(Boolean)
    .join('、')
}

function formatDiff(diff: Record<string, { 旧值: unknown; 新值: unknown }> | null): string {
  if (!diff || !Object.keys(diff).length) return '无字段变化'
  return Object.entries(diff)
    .map(([key, change]) => `${key} ${change.旧值}→${change.新值}`)
    .join('；')
}

function formatSummary(summary: Record<string, number> | null): string {
  if (!summary) return '—'
  return `重算 ${summary.重算 ?? 0}、复用 ${summary.复用 ?? 0}、待校验 ${summary.待校验 ?? 0}`
}

function resetFilters() {
  filters.value = { keyword: '', status: '' }
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '标签定义登记入口尚未接入审批流'
}

function today(): string {
  const now = new Date()
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

async function publish(row: Row) {
  const date = today()
  const publishValues = {
    发布时间: `${date} ${new Date().toTimeString().slice(0, 8)}`,
    发布日期: date,
    重算日期: date,
  }
  errorMessage.value = ''
  message.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/publish`, {
      method: 'POST',
      body: JSON.stringify({ values: publishValues }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message ?? '标签发布失败'
      return
    }
    message.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '标签发布失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  message.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message ?? '标签管理动作未生效'
      return
    }
    message.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '标签管理操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  if (filters.value.keyword) params.set('keyword', filters.value.keyword)
  if (filters.value.status) params.set('status', filters.value.status)
  const query = params.toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) throw new Error('标签定义列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '标签定义总数', value: total.value },
      { label: '已发布标签', value: rows.value.filter((row) => row.status === '已发布').length },
      { label: '被分群引用', value: rows.value.filter((row) => (row.引用分群 ?? []).length).length },
    ]
    await loadAudits()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '标签管理列表读取失败'
  }
}

async function loadAudits() {
  try {
    const response = await request(`${ENDPOINT}/audit`)
    if (!response.ok) return
    const payload = await response.json()
    audits.value = payload.items ?? []
  } catch {
    audits.value = []
  }
}

onMounted(() => reload())
</script>
