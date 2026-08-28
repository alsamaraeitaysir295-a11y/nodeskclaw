<!-- nodeskclaw-portal/src/views/admin/AdminAuditLog.vue -->
<template>
  <div class="p-6 space-y-5">
    <div>
      <h2 class="text-lg font-semibold">审计日志</h2>
      <p class="text-sm text-muted-foreground mt-0.5">平台操作审计流水，点击行展开详情</p>
    </div>

    <!-- 筛选区：带标签的输入组（替代裸输入框；datetime-local 原生 placeholder 因系统 locale
         会显示成 yyyy/mm/日 --:-- 的混排，用外层标签说明用途，弱化输入框内部占位） -->
    <div class="flex flex-wrap items-end gap-3">
      <div class="w-44">
        <label class="block text-xs font-medium text-muted-foreground mb-1">操作人</label>
        <div class="relative">
          <Search class="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
          <input
            v-model="actor"
            placeholder="姓名 / 邮箱 / ID"
            class="h-9 w-full pl-8 pr-3 rounded-lg border border-border bg-background text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
            @keyup.enter="reload(1)"
          />
        </div>
      </div>

      <div class="w-40">
        <label class="block text-xs font-medium text-muted-foreground mb-1">动作</label>
        <CustomSelect
          v-model="action"
          :options="actionSelectOptions"
          placeholder="所有动作"
          trigger-class="w-full"
        />
      </div>

      <div>
        <label class="block text-xs font-medium text-muted-foreground mb-1">开始时间</label>
        <input
          v-model="fromTs"
          type="datetime-local"
          class="h-9 px-2.5 rounded-lg border border-border bg-background text-sm text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
        />
      </div>

      <div>
        <label class="block text-xs font-medium text-muted-foreground mb-1">结束时间</label>
        <input
          v-model="toTs"
          type="datetime-local"
          class="h-9 px-2.5 rounded-lg border border-border bg-background text-sm text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
        />
      </div>

      <button
        class="h-9 px-4 rounded-lg border border-border text-sm hover:border-primary/50 hover:text-primary transition-colors"
        @click="reload(1)"
      >
        查询
      </button>
    </div>

    <!-- 日志表格：时间 / 操作人 / 动作 / 目标 / 状态，点击行展开 details JSON -->
    <div class="rounded-xl border border-border overflow-hidden">
      <table class="w-full text-sm">
        <thead>
          <tr class="border-b border-border bg-muted/40">
            <th class="text-left font-medium text-muted-foreground px-4 py-3">时间</th>
            <th class="text-left font-medium text-muted-foreground px-4 py-3">操作人</th>
            <th class="text-left font-medium text-muted-foreground px-4 py-3">动作</th>
            <th class="text-left font-medium text-muted-foreground px-4 py-3">目标</th>
            <th class="text-left font-medium text-muted-foreground px-4 py-3">状态</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-border">
          <template v-for="r in rows" :key="r.id">
            <!-- 主行：整行为点击区域（大块），点击切换 details 展开/收起 -->
            <tr class="hover:bg-muted/20 cursor-pointer transition-colors" @click="toggle(r.id)">
              <td class="px-4 py-3 text-muted-foreground text-xs tabular-nums whitespace-nowrap">
                {{ formatDateTime(r.created_at) }}
              </td>
              <td class="px-4 py-3">
                <span class="font-medium">{{ r.actor_name || truncate(r.actor_id) }}</span>
              </td>
              <td class="px-4 py-3">
                <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-primary/10 text-primary">
                  {{ localizeAction(r.action) }}
                </span>
              </td>
              <td class="px-4 py-3 text-muted-foreground">
                {{ r.target_type ? localizeTargetType(r.target_type) : '-' }}:{{ truncate(r.target_id) }}
              </td>
              <td class="px-4 py-3 text-muted-foreground">
                <span v-if="r.details?.status != null" class="text-xs tabular-nums">{{ r.details.status }}</span>
                <span v-else class="text-muted-foreground/60">-</span>
              </td>
            </tr>
            <!-- 展开行：以 pre 展示 details JSON -->
            <tr v-if="expanded.has(r.id)" class="bg-muted/10">
              <td colspan="5" class="px-4 py-3">
                <pre class="bg-muted p-3 rounded-lg text-xs overflow-auto">{{ JSON.stringify(r.details, null, 2) }}</pre>
              </td>
            </tr>
          </template>
          <tr v-if="rows.length === 0">
            <td colspan="5" class="px-4 py-12 text-center text-sm text-muted-foreground">暂无审计记录</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 分页控件 -->
    <div class="flex gap-2 items-center text-sm">
      <button
        class="px-3 py-1.5 rounded-lg border border-border text-sm disabled:opacity-40 hover:border-primary/50 hover:text-primary transition-colors"
        :disabled="page === 1"
        @click="reload(page - 1)"
      >
        上一页
      </button>
      <span class="text-muted-foreground">第 {{ page }} 页 / 共 {{ Math.ceil(total / pageSize) || 1 }} 页</span>
      <button
        class="px-3 py-1.5 rounded-lg border border-border text-sm disabled:opacity-40 hover:border-primary/50 hover:text-primary transition-colors"
        :disabled="page * pageSize >= total"
        @click="reload(page + 1)"
      >
        下一页
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Search } from 'lucide-vue-next'
import { useAdminApi, type AdminAuditRow } from '@/services/adminApi'
import CustomSelect, { type SelectOption } from '@/components/shared/CustomSelect.vue'

const api = useAdminApi()
const { t, te } = useI18n()

/** 统一时间展示：YYYY/MM/DD HH:mm:ss（本地时区，表格与筛选口径一致） */
function formatDateTime(iso: string | undefined): string {
  if (!iso) return '-'
  const d = new Date(iso)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

// 动作/目标类型中文化：与组织设置操作审计页面（AuditLogTable.vue）共用同一套 auditActions/auditTargetTypes 字典
function localizeAction(action: string): string {
  const key = 'auditActions.' + action.replace(/\./g, '_')
  return te(key) ? t(key) : action
}
function localizeTargetType(tt: string): string {
  const key = 'auditTargetTypes.' + tt
  return te(key) ? t(key) : tt
}
function truncate(s: string | null | undefined, max = 12): string {
  if (!s) return '-'
  return s.length > max ? s.slice(0, max) + '...' : s
}

// 筛选条件
const actor = ref('')
const action = ref<string | null>(null)
const fromTs = ref('')
const toTs = ref('')

// 下拉选项：从后端拉取可用动作枚举，转成中文化的 CustomSelect 选项
const actionOptions = ref<string[]>([])
const actionSelectOptions = computed<SelectOption[]>(() =>
  actionOptions.value.map(a => ({ value: a, label: localizeAction(a) })),
)

// 表格数据和分页状态
const rows = ref<AdminAuditRow[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)

// 展开状态：使用 Set 存储已展开行的 id，保证不可变更新
const expanded = ref<Set<string>>(new Set())

/** 加载指定页数据；筛选条件变化时传 p=1 重置到第一页 */
async function reload(p = page.value) {
  page.value = p
  const res = await api.fetchAuditLogs({
    actor: actor.value || undefined,
    action: action.value || undefined,
    // datetime-local 输入的是不带时区的本地时间字符串，必须转成 ISO UTC 才能跟后端时间比较对上
    from: fromTs.value ? new Date(fromTs.value).toISOString() : undefined,
    to: toTs.value ? new Date(toTs.value).toISOString() : undefined,
    page: p,
    pageSize: pageSize.value,
  })
  rows.value = res.data
  total.value = res.pagination.total
}

/** 切换指定行的展开/收起状态（不可变 Set 更新） */
function toggle(id: string) {
  const next = new Set(expanded.value)
  next.has(id) ? next.delete(id) : next.add(id)
  expanded.value = next
}

onMounted(async () => {
  // 初始化：并行拉取动作选项和第一页日志
  actionOptions.value = await api.fetchAuditActions()
  await reload(1)
})
</script>
