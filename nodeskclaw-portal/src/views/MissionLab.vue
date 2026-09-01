<script setup lang="ts">
/**
 * 任务实验室（Mission P1 最小验证页面，设计 §10 / T9）。
 * 双轨定位：测试功能独立入口，现有协作空间零改动（唯一例外是空间页的导航按钮）。
 * 验收判据：提交需求 → 拆解 → 确认 → 派发 → 实时播报 → 产物 → 验收/归档。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  ArrowLeft, CheckCircle2, CircleAlert, Clock, FlaskConical, Loader2,
  Package, RefreshCw, Send, Star, XCircle,
} from 'lucide-vue-next'
import api from '@/services/api'
import {
  missionApi,
  missionEventsStreamUrl,
  type MissionArtifactItem,
  type MissionDetail,
  type MissionEventItem,
  type MissionSummary,
} from '@/services/missions'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

// ── 工作区与任务列表 ─────────────────────────────────────────────
const workspaces = ref<Array<{ id: string; name: string }>>([])
const wsId = ref<string>('')
const missions = ref<MissionSummary[]>([])
const listLoading = ref(false)

async function loadWorkspaces() {
  const res = await api.get('/workspaces')
  workspaces.value = res.data?.data ?? []
  const fromQuery = route.query.ws as string
  wsId.value = fromQuery && workspaces.value.some(w => w.id === fromQuery)
    ? fromQuery
    : (workspaces.value[0]?.id ?? '')
}

async function loadMissions() {
  if (!wsId.value) return
  listLoading.value = true
  try {
    missions.value = await missionApi.list(wsId.value)
  } finally {
    listLoading.value = false
  }
}

watch(wsId, () => { loadMissions() })

// ── 创建任务 ─────────────────────────────────────────────────────
const requirement = ref('')
const creating = ref(false)

async function submitMission() {
  if (!requirement.value.trim() || creating.value) return
  creating.value = true
  try {
    const { id } = await missionApi.create(wsId.value, requirement.value.trim())
    requirement.value = ''
    await loadMissions()
    await selectMission(id)
  } finally {
    creating.value = false
  }
}

// ── 任务详情 ─────────────────────────────────────────────────────
const missionId = ref('')
const detail = ref<MissionDetail | null>(null)
const detailLoading = ref(false)
const events = ref<MissionEventItem[]>([])
const artifacts = ref<MissionArtifactItem[]>([])
const openL2 = ref<MissionEventItem | null>(null)
const answerText = ref('')
const rejectMode = ref(false)
const rejectReason = ref('')
const rejectedNodes = ref<string[]>([])

async function selectMission(id: string) {
  missionId.value = id
  await refreshDetail()
}

async function refreshDetail() {
  if (!missionId.value) return
  detailLoading.value = true
  try {
    detail.value = await missionApi.detail(missionId.value)
    events.value = await missionApi.events(missionId.value)
    artifacts.value = await missionApi.artifacts(missionId.value)
    connectStream()
  } finally {
    detailLoading.value = false
  }
}

// ── SSE 时间线（EventSource + query token，断线由页面刷新兜底）─────
let eventSource: EventSource | null = null
let lastSeq = 0

function connectStream() {
  disconnectStream()
  const status = detail.value?.status
  if (!missionId.value || !status || !['executing', 'blocked_question', 'awaiting_confirm'].includes(status)) return
  lastSeq = events.value.length ? Math.max(...events.value.map(e => e.seq)) : 0
  eventSource = new EventSource(missionEventsStreamUrl(missionId.value, lastSeq))
  eventSource.addEventListener('mission:event', (e: MessageEvent) => {
    try {
      const item = JSON.parse(e.data) as MissionEventItem
      if (item.seq <= lastSeq) return
      lastSeq = item.seq
      events.value.push(item)
      // 状态类事件触发详情刷新（节点徽标/验收入口跟进）
      if (['node_done', 'node_failed', 'dag_confirmed', 'mission_accepted', 'token_fuse_tripped', 'l2_question'].includes(item.event_type)) {
        void refreshDetailSoft()
      }
    } catch { /* 忽略坏帧 */ }
  })
}

let refreshTimer: ReturnType<typeof setTimeout> | null = null
function refreshDetailSoft() {
  if (refreshTimer) return
  refreshTimer = setTimeout(async () => {
    refreshTimer = null
    try {
      detail.value = await missionApi.detail(missionId.value)
      artifacts.value = await missionApi.artifacts(missionId.value)
    } catch { /* 忽略 */ }
  }, 500)
}

function disconnectStream() {
  eventSource?.close()
  eventSource = null
}

onBeforeUnmount(disconnectStream)

// ── 动作 ────────────────────────────────────────────────────────
const acting = ref(false)

async function confirmDag() {
  if (!detail.value || acting.value) return
  acting.value = true
  try {
    await missionApi.confirm(detail.value.id)
    await refreshDetail()
  } finally { acting.value = false }
}

async function cancelMission() {
  if (!detail.value || acting.value) return
  acting.value = true
  try {
    await missionApi.cancel(detail.value.id)
    await refreshDetail()
    await loadMissions()
  } finally { acting.value = false }
}

async function retryNode(nodeId: string) {
  if (!detail.value) return
  await missionApi.retryNode(detail.value.id, nodeId)
  await refreshDetail()
}

async function sendAnswer() {
  if (!detail.value || !openL2.value || !answerText.value.trim()) return
  await missionApi.answer(detail.value.id, openL2.value.id, answerText.value.trim())
  openL2.value = null
  answerText.value = ''
  await refreshDetail()
}

async function promote(artifactId: string) {
  if (!detail.value) return
  await missionApi.promote(detail.value.id, artifactId)
  artifacts.value = await missionApi.artifacts(detail.value.id)
}

async function acceptMission() {
  if (!detail.value) return
  acting.value = true
  try {
    await missionApi.accept(detail.value.id)
    await refreshDetail()
    await loadMissions()
  } finally { acting.value = false }
}

async function submitReject() {
  if (!detail.value) return
  acting.value = true
  try {
    await missionApi.reject(detail.value.id, rejectReason.value, rejectedNodes.value)
    rejectMode.value = false
    rejectReason.value = ''
    rejectedNodes.value = []
    await refreshDetail()
  } finally { acting.value = false }
}

// ── 展示辅助 ─────────────────────────────────────────────────────
const NODE_STATUS_TONE: Record<string, string> = {
  pending: 'text-muted-foreground', matched: 'text-blue-400',
  dispatched: 'text-amber-400', acked: 'text-amber-400',
  running: 'text-emerald-400', done: 'text-emerald-500',
  failed: 'text-red-400', blocked_question: 'text-orange-400',
  blocked_dependency: 'text-orange-400', skipped: 'text-muted-foreground',
}

const EVENT_ICON: Record<string, typeof Clock> = {
  l2_question: CircleAlert, l1_question: CircleAlert,
  node_done: CheckCircle2, node_failed: XCircle,
  artifact_produced: Package, token_fuse_tripped: CircleAlert,
}

function nodeStatusTone(s: string) { return NODE_STATUS_TONE[s] ?? 'text-muted-foreground' }
function eventIcon(type: string) { return EVENT_ICON[type] ?? Clock }
function eventName(type: string) {
  const key = `missions.eventTypes.${type}`
  const translated = t(key)
  return translated === key ? type : translated
}
const timelineEvents = computed(() => events.value.filter(e => e.visibility !== 'context'))
const pendingL2 = computed(() =>
  events.value.filter(e => e.event_type === 'l2_question' && !events.value.some(
    a => a.event_type === 'question_answered' && (a.payload as { answers_event_seq?: number } | null)?.answers_event_seq === e.seq,
  )),
)

void loadWorkspaces().then(() => loadMissions())
</script>

<template>
  <div class="flex flex-col h-screen overflow-hidden bg-background text-foreground">
    <!-- 顶栏 -->
    <div class="flex items-center gap-3 px-4 py-2.5 border-b border-border shrink-0">
      <button class="p-1.5 rounded-lg hover:bg-muted transition-colors" @click="router.back()">
        <ArrowLeft class="w-4 h-4" />
      </button>
      <FlaskConical class="w-4 h-4 text-primary" />
      <span class="font-semibold text-sm">{{ t('missions.title') }}</span>
      <span class="px-1.5 py-0.5 rounded text-[10px] font-bold uppercase bg-amber-500/15 text-amber-500 border border-amber-500/30">
        {{ t('missions.betaBadge') }}
      </span>
      <div class="flex-1" />
      <select
        v-model="wsId"
        class="px-2 py-1 rounded-lg border border-border bg-background text-xs"
      >
        <option v-for="w in workspaces" :key="w.id" :value="w.id">{{ w.name }}</option>
      </select>
      <button
        class="flex items-center gap-1 px-2 py-1 rounded-lg border border-border text-xs hover:bg-muted transition-colors"
        @click="refreshDetail"
      >
        <RefreshCw class="w-3.5 h-3.5" />
      </button>
    </div>

    <div class="flex flex-1 overflow-hidden">
      <!-- 左栏：新建 + 列表 -->
      <div class="w-72 shrink-0 border-r border-border flex flex-col overflow-hidden">
        <div class="p-3 border-b border-border">
          <textarea
            v-model="requirement"
            :placeholder="t('missions.requirementPlaceholder')"
            rows="3"
            class="w-full px-2.5 py-2 rounded-lg border border-border bg-background text-xs resize-none focus:outline-none focus:ring-1 focus:ring-primary"
          />
          <button
            :disabled="!requirement.trim() || creating || !wsId"
            class="mt-2 w-full flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg bg-primary text-primary-foreground text-xs font-medium disabled:opacity-50"
            @click="submitMission"
          >
            <Loader2 v-if="creating" class="w-3.5 h-3.5 animate-spin" />
            <Send v-else class="w-3.5 h-3.5" />
            {{ t('missions.submit') }}
          </button>
        </div>
        <div class="flex-1 overflow-y-auto">
          <div v-if="listLoading" class="p-3 text-xs text-muted-foreground">…</div>
          <button
            v-for="m in missions"
            :key="m.id"
            class="w-full text-left px-3 py-2.5 border-b border-border hover:bg-muted/50 transition-colors"
            :class="{ 'bg-muted': m.id === missionId }"
            @click="selectMission(m.id)"
          >
            <div class="text-xs font-medium truncate">{{ m.title }}</div>
            <div class="mt-1 flex items-center gap-2 text-[10px] text-muted-foreground">
              <span>{{ t(`missions.status.${m.status}`, m.status) }}</span>
              <span v-if="m.mission_type === 'lightweight'" class="px-1 rounded bg-primary/10 text-primary">
                {{ t('missions.lightweight') }}
              </span>
            </div>
          </button>
          <div v-if="!listLoading && !missions.length" class="p-3 text-xs text-muted-foreground">
            {{ t('missions.empty') }}
          </div>
        </div>
      </div>

      <!-- 右栏：详情 -->
      <div class="flex-1 overflow-y-auto p-4">
        <div v-if="!detail" class="text-sm text-muted-foreground">{{ t('missions.pickHint') }}</div>
        <template v-else>
          <!-- 头部 -->
          <div class="flex items-start justify-between gap-3">
            <div>
              <h2 class="text-base font-semibold">{{ detail.title }}</h2>
              <div class="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
                <span>{{ t(`missions.status.${detail.status}`, detail.status) }}</span>
                <span v-if="detail.brief?.goal">· {{ detail.brief.goal }}</span>
                <span>· {{ detail.tokens.cost }} tokens</span>
              </div>
            </div>
            <div class="flex items-center gap-2">
              <button
                v-if="detail.status === 'awaiting_confirm'"
                :disabled="acting"
                class="px-3 py-1.5 rounded-lg bg-primary text-primary-foreground text-xs font-medium disabled:opacity-50"
                @click="confirmDag"
              >
                {{ t('missions.confirmDag') }}
              </button>
              <button
                v-if="['executing', 'blocked_question', 'awaiting_confirm'].includes(detail.status)"
                class="px-3 py-1.5 rounded-lg border border-border text-xs hover:bg-muted"
                @click="cancelMission"
              >
                {{ t('missions.cancel') }}
              </button>
              <template v-if="detail.status === 'acceptance'">
                <button
                  :disabled="acting"
                  class="px-3 py-1.5 rounded-lg bg-emerald-600 text-white text-xs font-medium"
                  @click="acceptMission"
                >
                  {{ t('missions.accept') }}
                </button>
                <button
                  class="px-3 py-1.5 rounded-lg border border-red-500/40 text-red-400 text-xs"
                  @click="rejectMode = !rejectMode"
                >
                  {{ t('missions.reject') }}
                </button>
              </template>
            </div>
          </div>

          <!-- 打回面板 -->
          <div v-if="rejectMode && detail.status === 'acceptance'" class="mt-3 p-3 rounded-lg border border-red-500/30 bg-red-500/5">
            <div class="text-xs font-medium mb-2">{{ t('missions.rejectPickNodes') }}</div>
            <div class="flex flex-wrap gap-2 mb-2">
              <button
                v-for="n in detail.nodes.filter(x => x.status === 'done')"
                :key="n.id"
                class="px-2 py-1 rounded border text-xs transition-colors"
                :class="rejectedNodes.includes(n.id) ? 'border-red-400 bg-red-400/10 text-red-400' : 'border-border text-muted-foreground'"
                @click="rejectedNodes.includes(n.id) ? rejectedNodes.splice(rejectedNodes.indexOf(n.id), 1) : rejectedNodes.push(n.id)"
              >
                {{ n.title }}
              </button>
            </div>
            <input
              v-model="rejectReason"
              :placeholder="t('missions.rejectReasonPlaceholder')"
              class="w-full px-2.5 py-1.5 rounded border border-border bg-background text-xs"
            />
            <button
              :disabled="acting"
              class="mt-2 px-3 py-1.5 rounded-lg bg-red-500 text-white text-xs font-medium"
              @click="submitReject"
            >
              {{ t('missions.submitReject') }}
            </button>
          </div>

          <!-- L2 待答 -->
          <div v-for="q in pendingL2" :key="q.id" class="mt-3 p-3 rounded-lg border border-orange-500/40 bg-orange-500/5">
            <div class="flex items-start gap-2">
              <CircleAlert class="w-4 h-4 text-orange-400 shrink-0 mt-0.5" />
              <div class="flex-1">
                <div class="text-xs font-medium">{{ q.content }}</div>
                <div class="mt-2 flex gap-2">
                  <input
                    v-model="answerText"
                    :placeholder="t('missions.answerPlaceholder')"
                    class="flex-1 px-2.5 py-1.5 rounded border border-border bg-background text-xs"
                    @keyup.enter="sendAnswer"
                  />
                  <button class="px-3 py-1.5 rounded-lg bg-primary text-primary-foreground text-xs" @click="sendAnswer">
                    {{ t('missions.sendAnswer') }}
                  </button>
                </div>
              </div>
            </div>
          </div>

          <!-- 节点 -->
          <div class="mt-4">
            <div class="text-xs font-semibold text-muted-foreground mb-2">{{ t('missions.nodesTitle') }}</div>
            <div class="space-y-1.5">
              <div
                v-for="n in detail.nodes"
                :key="n.id"
                class="flex items-center gap-3 px-3 py-2 rounded-lg border border-border"
              >
                <span class="text-[10px] text-muted-foreground w-5">#{{ n.seq }}</span>
                <span class="text-xs font-medium flex-1 truncate">{{ n.title }}</span>
                <span
                  v-for="tag in n.capability_tags"
                  :key="tag"
                  class="px-1.5 py-0.5 rounded bg-muted text-[10px] text-muted-foreground"
                >{{ tag }}</span>
                <span class="text-[10px] font-medium" :class="nodeStatusTone(n.status)">
                  {{ t(`missions.nodeStatus.${n.status}`, n.status) }}
                </span>
                <button
                  v-if="n.status === 'failed'"
                  class="text-[10px] px-1.5 py-0.5 rounded border border-border hover:bg-muted"
                  @click="retryNode(n.id)"
                >{{ t('missions.retry') }}</button>
              </div>
            </div>
            <!-- 覆盖检查（确认页） -->
            <div v-if="detail.coverage" class="mt-2 space-y-1">
              <div v-for="c in detail.coverage" :key="c.node_id" class="flex items-center gap-2 text-[11px]">
                <span class="text-muted-foreground">#{{ c.seq }} {{ c.title }}:</span>
                <span v-if="c.suggested_instance_name" class="text-emerald-400">
                  {{ t('missions.suggested') }}: {{ c.suggested_instance_name }}
                </span>
                <span v-else class="text-orange-400">
                  {{ t('missions.gap') }}: {{ c.capability_gap.join(', ') }}
                </span>
              </div>
            </div>
          </div>

          <!-- 时间线 -->
          <div class="mt-4">
            <div class="text-xs font-semibold text-muted-foreground mb-2">{{ t('missions.timeline') }}</div>
            <div class="space-y-1.5">
              <div
                v-for="e in timelineEvents"
                :key="e.id"
                class="flex items-start gap-2 px-3 py-1.5 rounded-lg bg-muted/40"
              >
                <component :is="eventIcon(e.event_type)" class="w-3.5 h-3.5 mt-0.5 shrink-0 text-muted-foreground" />
                <div class="flex-1 text-xs">
                  <span class="text-muted-foreground">[{{ eventName(e.event_type) }}]</span>
                  {{ e.content }}
                </div>
              </div>
              <div v-if="!timelineEvents.length" class="text-xs text-muted-foreground">{{ t('missions.timelineEmpty') }}</div>
            </div>
          </div>

          <!-- 产物 -->
          <div class="mt-4">
            <div class="text-xs font-semibold text-muted-foreground mb-2">{{ t('missions.artifacts') }}</div>
            <div class="space-y-1.5">
              <div
                v-for="a in artifacts"
                :key="a.id"
                class="flex items-center gap-3 px-3 py-2 rounded-lg border border-border"
              >
                <Package class="w-3.5 h-3.5 text-muted-foreground" />
                <span class="text-xs flex-1 truncate">{{ a.name }}</span>
                <span class="text-[10px] text-muted-foreground">
                  {{ a.size_bytes ? `${Math.ceil(a.size_bytes / 1024)}KB` : '' }}
                </span>
                <span
                  class="text-[10px] px-1.5 py-0.5 rounded"
                  :class="a.retention === 'promoted' ? 'bg-emerald-500/10 text-emerald-400' : 'bg-amber-500/10 text-amber-400'"
                >
                  {{ a.retention === 'promoted' ? t('missions.promoted') : t('missions.quarantine') }}
                </span>
                <button
                  v-if="a.retention === 'quarantine'"
                  class="flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded border border-border hover:bg-muted"
                  @click="promote(a.id)"
                >
                  <Star class="w-3 h-3" />{{ t('missions.keep') }}
                </button>
              </div>
              <div v-if="!artifacts.length" class="text-xs text-muted-foreground">{{ t('missions.noArtifacts') }}</div>
            </div>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>
