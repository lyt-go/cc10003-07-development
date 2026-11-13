<template>
  <section class="page" data-module="segment">
    <header class="page-head">
      <div>
        <h2>分群分析管理</h2>
        <p class="page-desc">
          按标签版本驱动重算人群规模：覆盖对象数、最近计算时间与标签版本写回同一份分群结果，
          规模越界时停在待校验并保留旧结果。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记人群分群</button>
        <button class="btn" type="button" @click="exportRows">导出分群分析清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="applyFilters">
      <label class="filter-item">
        <span>分群编号</span>
        <input v-model="filters.keyword" placeholder="按分群编号检索，如 SEGM-0002" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>引用标签</span>
        <input v-model="filters.tag" placeholder="如 TAGM-0003" />
      </label>
      <label class="filter-item">
        <span>所属批次</span>
        <input v-model="filters.batch" placeholder="如 BATCH-SEG-20261113-A" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <form class="filter-bar locate-bar" @submit.prevent="locateEntry">
      <label class="filter-item">
        <span>跨页定位</span>
        <input v-model="locateKeyword" placeholder="输入分群编号，在当前条件下定位" />
      </label>
      <button class="btn primary" type="submit">定位记录</button>
      <span v-if="locateHint" class="locate-hint">{{ locateHint }}</span>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in rows"
          :key="String(row.id)"
          :class="{ 'row-highlight': highlightedId === row.id }"
        >
          <td v-for="column in columns" :key="column">{{ display(row, column) }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="refreshSegment(row)">刷新规模</button>
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
          <td :colspan="columns.length + 1" class="empty-state">当前条件下暂无分群数据，可放宽过滤条件</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条分群分析记录 · 第 {{ page }} / {{ totalPages }} 页</span>
      <div class="pager">
        <button class="btn ghost" type="button" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
        <button class="btn ghost" type="button" :disabled="page >= totalPages" @click="goPage(page + 1)">下一页</button>
      </div>
    </footer>
    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
    <p v-if="message" class="message-text">{{ message }}</p>

    <section v-if="selectedSegment" class="detail-panel">
      <header class="detail-head">
        <h3>{{ selectedSegment.分群编号 }} · 批次明细</h3>
        <button class="link" type="button" @click="selectedSegment = null">收起</button>
      </header>
      <dl class="detail-grid">
        <div><dt>规则版本</dt><dd>{{ selectedSegment.规则版本 ?? '—' }}</dd></div>
        <div><dt>标签版本</dt><dd>{{ selectedSegment.标签版本 ?? '—' }}</dd></div>
        <div><dt>最终规模</dt><dd>{{ selectedSegment.最终规模 ?? '—' }}</dd></div>
        <div><dt>覆盖对象数</dt><dd>{{ selectedSegment.覆盖对象数 ?? '—' }}</dd></div>
        <div><dt>所属批次</dt><dd>{{ selectedSegment.所属批次 ?? '—' }}</dd></div>
        <div><dt>校验状态</dt><dd>{{ selectedSegment.校验状态 ?? '—' }} {{ selectedSegment.校验说明 }}</dd></div>
      </dl>
      <table class="data-table">
        <thead>
          <tr>
            <th>批次号</th><th>状态</th><th>结果对象数</th><th>校验状态</th>
            <th>标签版本</th><th>规则版本</th><th>刷新周期</th><th>开始时间</th><th>说明</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in batches" :key="String(batch.批次号)">
            <td>{{ batch.批次号 }}</td>
            <td>{{ batch.状态 }}</td>
            <td>{{ batch.结果对象数 }}</td>
            <td>{{ batch.校验状态 }}</td>
            <td>{{ batch.标签版本 ?? '—' }}</td>
            <td>{{ batch.规则版本 }}</td>
            <td>{{ batch.刷新周期 }}</td>
            <td>{{ batch.开始时间 }}</td>
            <td>{{ batch.校验说明 || '—' }}</td>
            <td class="row-actions">
              <button v-if="batch.状态 === '失败'" class="link" type="button" @click="retryBatch(batch)">
                从检查点续算
              </button>
            </td>
          </tr>
          <tr v-if="!batches.length">
            <td colspan="10" class="empty-state">该分群还没有重算批次</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Batch = Record<string, string | number | null>

const ENDPOINT = '/api/segment'
const columns = [
  '分群编号', '分群名称', '最终规模', '覆盖对象数', '标签版本', '所属批次',
  '最近计算时间', '校验状态', '状态',
]
const actions = ['提交规则校验', '生效分群', '失效分群']
const statuses = ['草稿', '规则校验中', '已生效', '已失效']

const rows = ref<Row[]>([])
const batches = ref<Batch[]>([])
const total = ref(0)
const page = ref(1)
const size = 20
const errorMessage = ref('')
const message = ref('')
const locateHint = ref('')
const locateKeyword = ref('')
const highlightedId = ref<number | null>(null)
const selectedSegment = ref<Row | null>(null)
const filters = reactive({ keyword: '', status: '', tag: '', batch: '' })

const stats = ref([
  { label: '人群分群总数', value: 0 },
  { label: '已生效分群', value: 0 },
  { label: '待校验批次', value: 0 },
])

const totalPages = ref(1)

function display(row: Row, column: string): string {
  const value = column === '状态' ? row.status : row[column]
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

function queryString(extra: Record<string, string> = {}): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries({ ...filters, ...extra })) {
    if (value) params.set(key, value)
  }
  return params.toString()
}

function resetFilters() {
  filters.keyword = ''
  filters.status = ''
  filters.tag = ''
  filters.batch = ''
  locateHint.value = ''
  page.value = 1
  void reload()
}

function applyFilters() {
  page.value = 1
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '人群分群登记入口尚未接入审批流'
}

function goPage(target: number) {
  if (target < 1 || target > totalPages.value) return
  page.value = target
  void reload(false)
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
      errorMessage.value = payload.message ?? '分群分析动作未生效'
      return
    }
    message.value = payload.message
    await reload(false)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分群分析操作失败'
  }
}

async function refreshSegment(row: Row) {
  errorMessage.value = ''
  message.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/refresh`, {
      method: 'POST',
      body: JSON.stringify({ values: {} }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      // 待校验或并发冲突：照样刷新列表，看到的是保留的旧结果与校验说明。
      errorMessage.value = payload.message ?? '规模重算未通过'
    } else {
      message.value = payload.message
    }
    await reload(false)
    await openDetail(row)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '规模重算失败'
  }
}

async function retryBatch(batch: Batch) {
  errorMessage.value = ''
  message.value = ''
  try {
    const response = await request(`${ENDPOINT}/batches/${batch.批次号}/retry`, { method: 'POST' })
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message ?? '批次续算失败'
      return
    }
    message.value = payload.message
    await reload(false)
    if (selectedSegment.value) await openDetail(selectedSegment.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批次续算失败'
  }
}

async function openDetail(row: Row) {
  selectedSegment.value = row
  batches.value = []
  try {
    const response = await request(`${ENDPOINT}/batches?segment=${row.分群编号}`)
    if (!response.ok) throw new Error('批次明细读取失败')
    const payload = await response.json()
    batches.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批次明细读取失败'
  }
}

async function locateEntry() {
  errorMessage.value = ''
  locateHint.value = ''
  highlightedId.value = null
  const keyword = locateKeyword.value.trim()
  if (!keyword) {
    errorMessage.value = '请先输入要定位的分群编号'
    return
  }
  try {
    const response = await request(`${ENDPOINT}/locate?${queryString({ keyword, size: String(size) })}`)
    if (!response.ok) {
      throw new Error(`当前条件下没有找到分群 ${keyword}`)
    }
    const located = await response.json()
    page.value = located.page
    await reload(false)
    highlightedId.value = located.entry.id
    locateHint.value = `已定位到第 ${located.page} 页，所属批次 ${located.entry.所属批次 || '—'}`
    await openDetail(located.entry)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '定位失败'
  }
}

async function reload(resetPage = true) {
  if (resetPage) page.value = 1
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}?${queryString({ page: String(page.value), size: String(size) })}`)
    if (!response.ok) throw new Error('人群分群列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    totalPages.value = Math.max(1, Math.ceil(total.value / size))
    stats.value = [
      { label: '人群分群总数', value: total.value },
      { label: '已生效分群', value: rows.value.filter((row) => row.status === '已生效').length },
      { label: '待校验批次', value: rows.value.filter((row) => row.校验状态 === '待校验').length },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分群分析列表读取失败'
  }
}

onMounted(() => reload())
</script>
