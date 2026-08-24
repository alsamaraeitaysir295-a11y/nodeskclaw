<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { AlertCircle, CheckCircle2, Copy, Download } from 'lucide-vue-next'
import type { ToolInvokeResponse } from '@/services/externalAgents'
import { renderMarkdown } from '@/utils/markdown'

const props = defineProps<{
  /** invoke 响应（成功或失败）。 */
  response: ToolInvokeResponse | null
  /** 后端告知的目标展示形态。 */
  display: 'table' | 'json' | 'text'
  /** 点号路径（如 'data' 或 'result.items'），缺省时退 JSON 树。 */
  itemsPath?: string | null
  /** 可选：字段级错误，仅错误时展示。 */
  fieldErrors?: Record<string, string> | null
  /** 触发重试：把头部操作外置。 */
  showRetry?: boolean
}>()

const emit = defineEmits<{
  (e: 'retry'): void
}>()

const { t } = useI18n()

// ── 路径解析（spec §7.2 / §6.2 第 5 步）────────────────────────────────────────────
// 后端通过 items_path 告知前端数据数组位置；解析失败或非数组则回退到 JSON 树。
function resolveItemsPath(data: any, path?: string | null): any | null {
  if (!data || !path) return null
  const parts = path.split('.').filter(Boolean)
  let cur: any = data
  for (const k of parts) {
    if (cur == null || typeof cur !== 'object') return null
    cur = cur[k]
  }
  return cur
}

const itemsArray = computed<any[] | null>(() => {
  if (!props.response || props.response.success !== true) return null
  const data = (props.response as any).data
  const v = resolveItemsPath(data, props.itemsPath)
  return Array.isArray(v) ? v : null
})

// ── display=text：取后端抽取好的 text（如 RAG 的 answer），渲染 Markdown ──────
// 推理模型输出的 <think>...</think> 前缀对用户是噪音，渲染前剥除。
const renderedText = computed<string | null>(() => {
  if (!props.response || props.response.success !== true) return null
  if (props.display !== 'text') return null
  const raw = (props.response as any).text
  if (typeof raw !== 'string' || !raw.trim()) return null
  const stripped = raw.replace(/<think>[\s\S]*?<\/think>\s*/gi, '').trim()
  const source = stripped || raw.trim()
  try {
    return renderMarkdown(source)
  } catch {
    return null
  }
})

const jsonPayload = computed(() => {
  if (!props.response || props.response.success !== true) return null
  const data = (props.response as any).data
  // 当 display=json 或 items_path 解析失败时，统一走 JSON 树
  if (props.display === 'json') return data
  if (!itemsArray.value) return data
  return null
})

const flatColumns = computed<string[] | null>(() => {
  const arr = itemsArray.value
  if (!arr || arr.length === 0) return null
  // 只取首个对象的 keys（保持表格轻量）；后续行多出的字段会被忽略。
  const first = arr.find((x) => x && typeof x === 'object')
  if (!first) return null
  return Object.keys(first)
})

const rowCount = computed(() => itemsArray.value?.length ?? 0)

/** 单元格值轻量格式化：对象/数组压缩成单行 JSON，原值转字符串。 */
function formatCell(value: unknown): string {
  if (value == null) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

// ── 操作：复制 JSON / 下载 JSON ───────────────────────────────────────────────
const copyState = ref<'idle' | 'success' | 'failed'>('idle')

function getJsonString(): string {
  const payload = itemsArray.value ?? jsonPayload.value
  return JSON.stringify(payload ?? {}, null, 2)
}

async function copyJson() {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(getJsonString())
      copyState.value = 'success'
      window.setTimeout(() => (copyState.value = 'idle'), 2000)
      return
    }
    throw new Error('clipboard API unavailable')
  } catch {
    // 兜底用 textarea + execCommand，兼容旧浏览器/非安全上下文
    try {
      const ta = document.createElement('textarea')
      ta.value = getJsonString()
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      copyState.value = 'success'
      window.setTimeout(() => (copyState.value = 'idle'), 2000)
      return
    } catch {
      copyState.value = 'failed'
      window.setTimeout(() => (copyState.value = 'idle'), 2000)
      return
    }
  }
}

function downloadJson() {
  const blob = new Blob([getJsonString()], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `plugin-result-${Date.now()}.json`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

// ── 失败态字段列表（仅错误时显示）────────────────────────────────────────────
const fieldErrorEntries = computed(() => {
  if (!props.fieldErrors) return []
  return Object.entries(props.fieldErrors)
})
</script>

<template>
  <div class="plugin-result">
    <!-- 错误态：success=false 或上游/校验失败 -->
    <div
      v-if="!response || response.success === false"
      class="rounded-lg border border-destructive/40 bg-destructive/5 p-4 space-y-3"
    >
      <div class="flex items-start gap-2">
        <AlertCircle class="w-5 h-5 text-destructive shrink-0 mt-0.5" />
        <div class="flex-1 min-w-0">
          <p class="text-sm font-medium text-destructive">
            {{ t('pluginResult.upstreamError') }}
          </p>
          <p
            v-if="response && (response as any).error"
            class="text-xs text-muted-foreground mt-1 break-words"
          >
            {{ (response as any).error }}
          </p>
        </div>
      </div>

      <ul v-if="fieldErrorEntries.length" class="space-y-1 text-xs">
        <li class="font-medium text-destructive">
          {{ t('pluginResult.fieldsHint') }}
        </li>
        <li
          v-for="[field, msg] in fieldErrorEntries"
          :key="field"
          class="flex gap-2 text-muted-foreground"
        >
          <span class="font-mono text-destructive/80 shrink-0">{{ field }}</span>
          <span class="text-foreground/80">{{ msg }}</span>
        </li>
      </ul>

      <button
        v-if="showRetry"
        type="button"
        class="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent"
        @click="emit('retry')"
      >
        <AlertCircle class="w-3.5 h-3.5" />
        {{ t('pluginForm.retry') }}
      </button>
    </div>

    <!-- 成功态 -->
    <div v-else class="space-y-3">
      <!-- 头部操作 -->
      <div class="flex items-center justify-between gap-2">
        <div class="flex items-center gap-2 text-sm text-muted-foreground">
          <CheckCircle2 class="w-4 h-4 text-green-500" />
          <span>{{ t('pluginResult.title') }}</span>
          <span
            v-if="flatColumns"
            class="text-xs px-1.5 py-0.5 rounded bg-secondary text-secondary-foreground"
          >
            {{
              t('pluginResult.table.columnsCount', {
                count: flatColumns.length,
                rows: rowCount,
              })
            }}
          </span>
        </div>
        <div class="flex items-center gap-2">
          <button
            type="button"
            class="inline-flex items-center gap-1 rounded-md border border-border bg-background px-2 py-1 text-xs hover:bg-accent"
            :title="t('pluginResult.downloadJson')"
            @click="downloadJson"
          >
            <Download class="w-3.5 h-3.5" />
          </button>
          <button
            type="button"
            class="inline-flex items-center gap-1 rounded-md border border-border bg-background px-2 py-1 text-xs hover:bg-accent"
            :title="
              copyState === 'failed'
                ? t('pluginResult.copyFailed')
                : t('pluginResult.copyJson')
            "
            @click="copyJson"
          >
            <Copy class="w-3.5 h-3.5" />
            <span v-if="copyState === 'success'" class="text-green-600">
              {{ t('pluginResult.copied') }}
            </span>
            <span v-else>{{ t('pluginResult.copyJson') }}</span>
          </button>
        </div>
      </div>

      <!-- 表格视图：display=table 且 items_path 解析为数组 -->
      <div
        v-if="display === 'table' && itemsArray && flatColumns"
        class="rounded-lg border border-border overflow-hidden"
      >
        <div class="overflow-x-auto">
          <table class="w-full text-xs">
            <thead class="bg-muted/50">
              <tr>
                <th
                  v-for="col in flatColumns"
                  :key="col"
                  class="px-3 py-2 text-left font-medium text-foreground/80 whitespace-nowrap"
                >
                  {{ col }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(row, idx) in itemsArray"
                :key="idx"
                class="border-t border-border/60 hover:bg-muted/30"
              >
                <td
                  v-for="col in flatColumns"
                  :key="col"
                  class="px-3 py-1.5 text-foreground/90 align-top"
                >
                  <span class="whitespace-pre-wrap break-words">
                    {{ formatCell(row?.[col]) }}
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 数组空态 -->
      <div
        v-else-if="display === 'table' && itemsArray && itemsArray.length === 0"
        class="rounded-lg border border-border p-6 text-center text-sm text-muted-foreground"
      >
        {{ t('pluginResult.table.empty') }}
      </div>

      <!-- Markdown 文本视图：display=text（RAG 类问答的 answer） -->
      <div
        v-else-if="display === 'text' && renderedText"
        class="rounded-lg border border-border bg-background p-4"
        data-testid="plugin-result-text"
      >
        <!-- eslint-disable-next-line vue/no-v-html — renderMarkdown 内置 DOMPurify 消毒 -->
        <div class="prose prose-sm max-w-none text-foreground" v-html="renderedText" />
      </div>
      <div
        v-else-if="display === 'text'"
        class="rounded-lg border border-border p-6 text-center text-sm text-muted-foreground"
      >
        {{ t('pluginResult.noData') }}
      </div>

      <!-- JSON 树 -->
      <div
        v-else
        class="rounded-lg border border-border bg-muted/30 p-3"
      >
        <p
          v-if="!jsonPayload"
          class="text-sm text-muted-foreground text-center py-4"
        >
          {{ t('pluginResult.noData') }}
        </p>
        <pre v-else class="text-xs font-mono whitespace-pre-wrap break-words text-foreground/90 overflow-x-auto">{{ getJsonString() }}</pre>
      </div>
    </div>
  </div>
</template>
