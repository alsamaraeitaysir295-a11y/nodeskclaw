<!-- nodeskclaw-portal/src/views/admin/AdminAuditLog.vue -->
<template>
  <div class="p-6 space-y-4">
    <h2 class="text-2xl font-semibold">审计日志</h2>

    <!-- 筛选区：actor 输入、action 下拉、时间范围、查询按钮 -->
    <div class="flex gap-2 text-sm">
      <input v-model="actor" placeholder="actor_id" class="border rounded px-2 py-1" />
      <CustomSelect
        v-model="action"
        :options="actionSelectOptions"
        placeholder="所有动作"
        size="xs"
        trigger-class="w-40"
      />
      <input v-model="fromTs" type="datetime-local" class="border rounded px-2 py-1" />
      <input v-model="toTs" type="datetime-local" class="border rounded px-2 py-1" />
      <button @click="reload(1)" class="border px-3 py-1 rounded">查询</button>
    </div>

    <!-- 日志表格：时间 / 操作人 / 动作 / 目标 / 状态，点击行展开 details JSON -->
    <table class="w-full text-sm">
      <thead>
        <tr>
          <th class="text-left py-1">时间</th>
          <th class="text-left py-1">操作人</th>
          <th class="text-left py-1">动作</th>
          <th class="text-left py-1">目标</th>
          <th class="text-left py-1">状态</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="r in rows" :key="r.id">
          <!-- 主行：点击切换 details 展开/收起 -->
          <tr class="hover:bg-muted/50 cursor-pointer" @click="toggle(r.id)">
            <td class="py-1">{{ r.created_at }}</td>
            <td class="py-1">{{ r.actor_name || truncate(r.actor_id) }}</td>
            <td class="py-1">{{ localizeAction(r.action) }}</td>
            <td class="py-1">{{ r.target_type ? localizeTargetType(r.target_type) : '-' }}:{{ truncate(r.target_id) }}</td>
            <td class="py-1">{{ r.details?.status ?? '-' }}</td>
          </tr>
          <!-- 展开行：以 pre 展示 details JSON，使用 Set 判断是否展开 -->
          <tr v-if="expanded.has(r.id)">
            <td colspan="5">
              <pre class="bg-muted p-3 text-xs overflow-auto">{{ JSON.stringify(r.details, null, 2) }}</pre>
            </td>
          </tr>
        </template>
      </tbody>
    </table>

    <!-- 分页控件 -->
    <div class="flex gap-2 items-center text-sm">
      <button :disabled="page === 1" @click="reload(page - 1)" class="border px-2 py-1 rounded disabled:opacity-40">上一页</button>
      <span>第 {{ page }} 页 / {{ Math.ceil(total / pageSize) || 1 }}</span>
      <button :disabled="page * pageSize >= total" @click="reload(page + 1)" class="border px-2 py-1 rounded disabled:opacity-40">下一页</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAdminApi, type AdminAuditRow } from '@/services/adminApi'
import CustomSelect, { type SelectOption } from '@/components/shared/CustomSelect.vue'

const api = useAdminApi()
const { t, te } = useI18n()

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
