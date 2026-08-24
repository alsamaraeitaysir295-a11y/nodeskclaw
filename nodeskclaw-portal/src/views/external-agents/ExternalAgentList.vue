<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { Bot, Plus, RefreshCw, CheckCircle2, XCircle, Circle, MessageSquare, Wrench, Pencil, Trash2, Wand2 } from 'lucide-vue-next'
import { useExternalAgentStore } from '@/stores/externalAgents'
import { externalAgentApi, type ExternalAgent } from '@/services/externalAgents'
import { useAuthStore } from '@/stores/auth'
import { hasOrgRoleLevel } from '@/utils/orgRole'

const { t, locale } = useI18n()
const router = useRouter()
const store = useExternalAgentStore()
const authStore = useAuthStore()

const syncing = ref<string | null>(null)
const deleting = ref<string | null>(null)

// org operator+ 可创建/编辑/同步；delete 保持 org admin 专属
const canManage = computed(
  () => hasOrgRoleLevel(authStore.user?.portal_org_role, 'operator') || authStore.user?.is_super_admin,
)
const canDelete = computed(
  () => authStore.user?.portal_org_role === 'admin' || authStore.user?.is_super_admin,
)

onMounted(() => store.fetchAgents())

async function sync(agent: ExternalAgent) {
  syncing.value = agent.id
  try {
    await externalAgentApi.sync(agent.id)
    await store.fetchAgents()
  } finally {
    syncing.value = null
  }
}

async function remove(agent: ExternalAgent) {
  if (!confirm(t('externalAgentList.confirmDelete', { name: agent.name }))) return
  deleting.value = agent.id
  try {
    await externalAgentApi.remove(agent.id)
    await store.fetchAgents()
  } finally {
    deleting.value = null
  }
}

function statusClass(agent: ExternalAgent) {
  if (agent.is_reachable) return 'bg-green-100 text-green-700'
  if (agent.last_checked_at) return 'bg-red-100 text-red-700'
  return 'bg-gray-100 text-gray-500'
}

function statusLabel(agent: ExternalAgent) {
  if (agent.is_reachable) return t('externalAgentList.statusConnected')
  if (agent.last_checked_at) return t('externalAgentList.statusFailed')
  return t('externalAgentList.statusUnchecked')
}

function formatTime(ts: string | null) {
  if (!ts) return ''
  // 跟随 i18n locale 切换；中文走 zh-CN 数字格式，英文走默认短格式
  return new Date(ts).toLocaleString(locale.value, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

// ── Phase 1 §7.2：click 分支 ──────────────────────────────────────────────────
// chat 型 → 既有 ExternalAgentChat；tool 型 → 新 ExternalAgentToolForm。
// 既有的 chat 行为（路由 / 按钮）不改动：仅在 openAgent 里按 type 选择目的地。
function isToolAgent(agent: ExternalAgent): boolean {
  return agent.type === 'tool'
}

function openAgent(agent: ExternalAgent) {
  if (isToolAgent(agent)) {
    router.push(`/agents/${agent.id}/form`)
  } else {
    router.push(`/agents/${agent.id}/chat`)
  }
}

// ── 生命周期状态(Phase 1 §10)：draft / active / disabled ────────────────────
// 非管理员只看到 active 的插件；管理员看到全部(含草稿/停用，便于管理)
const visibleAgents = computed(() => {
  if (canManage.value) return store.agents
  return store.agents.filter((a) => a.status === 'active' || !a.status)
})

function lifecycleBadge(agent: ExternalAgent): { label: string; cls: string } | null {
  if (agent.status === 'disabled') {
    return { label: t('externalAgentList.statusDisabled'), cls: 'bg-gray-200 text-gray-600' }
  }
  if (agent.status === 'draft') {
    return { label: t('externalAgentList.statusDraft'), cls: 'bg-amber-100 text-amber-700' }
  }
  return null // active 不显示额外徽章(连接状态已有)
}

function isUsable(agent: ExternalAgent): boolean {
  return agent.status === 'active' || !agent.status
}
</script>

<template>
  <div class="max-w-6xl mx-auto px-6 py-8">
    <!-- 页头 -->
    <div class="flex items-center justify-between mb-6">
      <div class="flex items-center gap-3">
        <Bot class="w-6 h-6 text-primary" />
        <h1 class="text-xl font-semibold text-foreground">{{ t('externalAgentList.title') }}</h1>
        <span class="text-xs text-muted-foreground">{{ t('externalAgentList.subtitle') }}</span>
      </div>
      <button
        v-if="canManage"
        class="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90"
        @click="router.push('/org-settings/external-agents/new')"
      >
        <Plus class="w-4 h-4" />
        {{ t('externalAgentList.addAgent') }}
      </button>
    </div>

    <!-- 加载中 -->
    <div v-if="store.loading" class="text-sm text-muted-foreground text-center py-20">
      {{ t('externalAgentList.loading') }}
    </div>

    <!-- 空状态 -->
    <div
      v-else-if="visibleAgents.length === 0"
      class="flex flex-col items-center justify-center py-24 text-center"
    >
      <Bot class="w-12 h-12 text-muted-foreground/40 mb-4" />
      <p class="text-sm font-medium text-foreground mb-1">{{ t('externalAgentList.emptyTitle') }}</p>
      <p class="text-xs text-muted-foreground mb-4">
        {{ t('externalAgentList.emptyDescription') }}
      </p>
      <button
        v-if="canManage"
        class="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90"
        @click="router.push('/org-settings/external-agents/new')"
      >
        <Plus class="w-4 h-4" />
        {{ t('externalAgentList.addAgent') }}
      </button>
    </div>

    <!-- 能力卡片网格 -->
    <div v-else class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
      <div
        v-for="agent in visibleAgents"
        :key="agent.id"
        class="relative rounded-xl border border-border bg-card overflow-hidden flex flex-col"
        :class="{ 'opacity-60': !isUsable(agent) }"
      >
        <!-- 主题色装饰条 -->
        <div
          class="h-1 w-full shrink-0"
          :style="agent.theme_color ? `background:${agent.theme_color}` : 'background: var(--primary)'"
        />

        <!-- 卡片主体 -->
        <div class="flex-1 p-4 flex flex-col gap-3">
          <!-- 名称行 -->
          <div class="flex items-start gap-2">
            <span v-if="agent.icon_emoji" class="text-2xl leading-none shrink-0 mt-0.5">
              {{ agent.icon_emoji }}
            </span>
            <Bot v-else class="w-6 h-6 text-muted-foreground shrink-0 mt-0.5" />
            <div class="flex-1 min-w-0">
              <p class="font-semibold text-foreground text-sm leading-tight truncate">{{ agent.name }}</p>
              <!-- 协议 badge -->
              <span class="mt-0.5 inline-block text-[10px] font-medium px-1.5 py-0.5 rounded bg-violet-100 text-violet-700">
                {{ agent.protocol === 'openai_compatible' ? t('externalAgentList.protocolOpenai') : t('externalAgentList.protocolCustom') }}
              </span>
              <!-- Phase 1 §3 插件分类徽章（B类工具） -->
              <span
                v-if="isToolAgent(agent)"
                class="mt-0.5 inline-flex items-center gap-0.5 text-[10px] font-medium px-1.5 py-0.5 rounded bg-amber-100 text-amber-700"
              >
                <Wrench :size="10" />
                {{ t('externalAgentList.typeTool') }}
              </span>
            </div>
          </div>

          <!-- 简介 -->
          <p v-if="agent.description" class="text-xs text-muted-foreground line-clamp-2">
            {{ agent.description }}
          </p>

          <!-- 能力标签 -->
          <div v-if="agent.capabilities.length" class="flex flex-wrap gap-1">
            <span
              v-for="cap in agent.capabilities"
              :key="cap"
              class="text-[10px] px-2 py-0.5 rounded-full bg-secondary text-secondary-foreground"
            >
              {{ cap }}
            </span>
          </div>

          <!-- Phase 2 §8.4：tool 型显示功能数（chat 型不展示） -->
          <div
            v-if="isToolAgent(agent) && (agent.function_count ?? 0) > 0"
            class="flex flex-wrap gap-1"
          >
            <span
              class="inline-flex items-center text-[10px] font-medium px-1.5 py-0.5 rounded bg-sky-100 text-sky-700"
              :data-test="`function-count-${agent.id}`"
            >
              {{ t('externalAgentList.functionsCount', { count: agent.function_count ?? 0 }) }}
            </span>
          </div>

          <!-- 连接状态 + 生命周期状态 -->
          <div class="flex items-center gap-1.5 flex-wrap">
            <!-- 生命周期徽章(draft/disabled)，active 不显示 -->
            <span
              v-if="lifecycleBadge(agent)"
              class="inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium"
              :class="lifecycleBadge(agent)!.cls"
              :data-test="`lifecycle-${agent.id}`"
            >
              {{ lifecycleBadge(agent)!.label }}
            </span>
            <span
              class="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium"
              :class="statusClass(agent)"
            >
              <CheckCircle2 v-if="agent.is_reachable" class="w-3 h-3" />
              <XCircle v-else-if="agent.last_checked_at" class="w-3 h-3" />
              <Circle v-else class="w-3 h-3" />
              {{ statusLabel(agent) }}
            </span>
            <span v-if="agent.last_checked_at" class="text-[10px] text-muted-foreground">
              {{ formatTime(agent.last_checked_at) }}
            </span>
          </div>
        </div>

        <!-- 操作栏 -->
        <div class="px-4 py-3 border-t border-border bg-muted/20 flex items-center gap-2">
          <button
            class="flex-1 inline-flex items-center justify-center gap-1.5 rounded-lg py-1.5 text-xs font-medium"
            :class="isUsable(agent)
              ? 'bg-primary text-primary-foreground hover:opacity-90'
              : 'bg-muted text-muted-foreground cursor-not-allowed'"
            :disabled="!isUsable(agent)"
            :title="!isUsable(agent) ? t('externalAgentList.disabledHint') : undefined"
            @click="isUsable(agent) && openAgent(agent)"
          >
            <MessageSquare v-if="!isToolAgent(agent)" class="w-3.5 h-3.5" />
            <Wrench v-else class="w-3.5 h-3.5" />
            {{ isToolAgent(agent) ? t('externalAgentList.openForm') : t('externalAgentList.openChat') }}
          </button>
          <button
            v-if="canManage"
            class="inline-flex items-center justify-center gap-1 rounded-lg border border-border text-muted-foreground px-3 py-1.5 text-xs hover:text-primary hover:border-primary/50 hover:bg-primary/5"
            @click="router.push(`/org-settings/external-agents/${agent.id}/edit`)"
          >
            <Pencil class="w-3 h-3" />
            {{ t('externalAgentList.edit') }}
          </button>
          <button
            v-if="canManage && agent.status === 'draft'"
            class="inline-flex items-center justify-center gap-1 rounded-lg border border-amber-300 text-amber-700 px-3 py-1.5 text-xs hover:bg-amber-50 focus:outline-none focus:ring-2 focus:ring-amber-200"
            :title="t('externalAgentList.configureWizard')"
            @click="router.push(`/org-settings/external-agents/${agent.id}/edit`)"
          >
            <Wand2 class="w-3 h-3" />
            {{ t('externalAgentList.configureWizard') }}
          </button>
          <button
            v-if="canManage"
            class="p-1.5 rounded-lg text-muted-foreground hover:text-primary hover:bg-primary/10 disabled:opacity-40"
            :disabled="syncing === agent.id"
            :title="t('externalAgentList.syncTitle')"
            @click="sync(agent)"
          >
            <RefreshCw class="w-3.5 h-3.5" :class="{ 'animate-spin': syncing === agent.id }" />
          </button>
          <button
            v-if="canDelete"
            class="p-1.5 rounded-lg text-muted-foreground hover:text-destructive hover:bg-destructive/10 disabled:opacity-40"
            :disabled="deleting === agent.id"
            @click="remove(agent)"
          >
            <Trash2 class="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
