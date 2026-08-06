<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useOrgStore } from '@/stores/org'
import { BarChart3, Loader2 } from 'lucide-vue-next'
import api from '@/services/api'
import { useToast } from '@/composables/useToast'
import { resolveApiErrorMessage } from '@/i18n/error'

const { t } = useI18n()
const orgStore = useOrgStore()
const toast = useToast()

const orgId = computed(() => orgStore.currentOrgId)

interface ProviderUsage {
  provider: string
  model: string
  instance_id: string
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  request_count: number
}

interface TokenAnalytics {
  total_prompt_tokens: number
  total_completion_tokens: number
  total_tokens: number
  by_provider: ProviderUsage[]
}

const loading = ref(true)
const analytics = ref<TokenAnalytics | null>(null)

async function fetchAnalytics() {
  if (!orgId.value) return
  loading.value = true
  try {
    const res = await api.get(`/orgs/${orgId.value}/token-analytics`)
    analytics.value = res.data.data ?? null
  } catch (e: any) {
    toast.error(resolveApiErrorMessage(e))
  } finally {
    loading.value = false
  }
}

// 明细行占总用量的百分比，用于 CSS 进度条；总量为 0 时避免除零
function sharePercent(row: ProviderUsage): number {
  const grand = analytics.value?.total_tokens ?? 0
  if (!grand) return 0
  return Math.min(100, (row.total_tokens / grand) * 100)
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`
  return n.toString()
}

onMounted(async () => {
  if (!orgStore.currentOrg) await orgStore.fetchMyOrg()
  await fetchAnalytics()
})
</script>

<template>
  <div class="space-y-6">
    <div>
      <h2 class="text-lg font-semibold flex items-center gap-2">
        <BarChart3 class="w-5 h-5" />
        {{ t('orgSettings.llmAnalyticsTitle') }}
      </h2>
      <p class="text-sm text-muted-foreground mt-1">
        {{ t('orgSettings.llmAnalyticsDescription') }}
      </p>
    </div>

    <div v-if="loading" class="flex items-center justify-center py-12">
      <Loader2 class="w-5 h-5 animate-spin text-muted-foreground" />
    </div>

    <template v-else-if="analytics && analytics.by_provider.length > 0">
      <!-- 汇总卡片：输入/输出/总 tokens -->
      <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div class="rounded-lg border border-border bg-card p-4">
          <div class="text-xs text-muted-foreground">{{ t('orgSettings.llmAnalyticsTotalPrompt') }}</div>
          <div class="text-xl font-semibold mt-1">{{ formatTokens(analytics.total_prompt_tokens) }}</div>
        </div>
        <div class="rounded-lg border border-border bg-card p-4">
          <div class="text-xs text-muted-foreground">{{ t('orgSettings.llmAnalyticsTotalCompletion') }}</div>
          <div class="text-xl font-semibold mt-1">{{ formatTokens(analytics.total_completion_tokens) }}</div>
        </div>
        <div class="rounded-lg border border-border bg-card p-4">
          <div class="text-xs text-muted-foreground">{{ t('orgSettings.llmAnalyticsTotalTokens') }}</div>
          <div class="text-xl font-semibold mt-1">{{ formatTokens(analytics.total_tokens) }}</div>
        </div>
      </div>

      <!-- 按 provider/model/instance 分组的用量明细 -->
      <div class="rounded-lg border border-border overflow-hidden">
        <table class="w-full text-sm border-collapse">
          <thead class="bg-muted/50">
            <tr>
              <th class="text-left px-4 py-2.5 text-xs font-medium text-muted-foreground uppercase tracking-wide">{{ t('orgSettings.llmAnalyticsColProvider') }}</th>
              <th class="text-left px-4 py-2.5 text-xs font-medium text-muted-foreground uppercase tracking-wide">{{ t('orgSettings.llmAnalyticsColModel') }}</th>
              <th class="text-left px-4 py-2.5 text-xs font-medium text-muted-foreground uppercase tracking-wide">{{ t('orgSettings.llmAnalyticsColInstance') }}</th>
              <th class="text-right px-4 py-2.5 text-xs font-medium text-muted-foreground uppercase tracking-wide">{{ t('orgSettings.llmAnalyticsColPromptTokens') }}</th>
              <th class="text-right px-4 py-2.5 text-xs font-medium text-muted-foreground uppercase tracking-wide">{{ t('orgSettings.llmAnalyticsColCompletionTokens') }}</th>
              <th class="text-right px-4 py-2.5 text-xs font-medium text-muted-foreground uppercase tracking-wide">{{ t('orgSettings.llmAnalyticsColTotalTokens') }}</th>
              <th class="text-right px-4 py-2.5 text-xs font-medium text-muted-foreground uppercase tracking-wide">{{ t('orgSettings.llmAnalyticsColRequests') }}</th>
              <th class="text-left px-4 py-2.5 text-xs font-medium text-muted-foreground uppercase tracking-wide w-[15%]">{{ t('orgSettings.llmAnalyticsColShare') }}</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-border">
            <tr v-for="row in analytics.by_provider" :key="`${row.provider}-${row.model}-${row.instance_id}`" class="hover:bg-muted/30 transition-colors">
              <td class="px-4 py-3">{{ row.provider }}</td>
              <td class="px-4 py-3 font-mono text-xs">{{ row.model }}</td>
              <td class="px-4 py-3 font-mono text-xs">{{ row.instance_id }}</td>
              <td class="px-4 py-3 text-right">{{ formatTokens(row.prompt_tokens) }}</td>
              <td class="px-4 py-3 text-right">{{ formatTokens(row.completion_tokens) }}</td>
              <td class="px-4 py-3 text-right">{{ formatTokens(row.total_tokens) }}</td>
              <td class="px-4 py-3 text-right">{{ row.request_count }}</td>
              <td class="px-4 py-3">
                <div class="h-1.5 bg-muted rounded-full overflow-hidden">
                  <div
                    class="h-full rounded-full bg-primary transition-all"
                    :style="{ width: sharePercent(row) + '%' }"
                  />
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <div v-else class="rounded-lg border border-border py-12 text-center text-sm text-muted-foreground">
      {{ t('orgSettings.llmAnalyticsEmpty') }}
    </div>
  </div>
</template>
