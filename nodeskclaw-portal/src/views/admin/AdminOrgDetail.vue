<!-- 超管组织详情：聚焦成员管理（添加成员 / 角色调整 / 移除）。
     概览与功能开关 tab 已按产品要求移除（2026-08-27），组织基础信息内联在页头展示 -->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ArrowLeft, Plus, Search, X, Loader2, UserPlus, Trash2, AlertTriangle,
} from 'lucide-vue-next'
import { useAdminApi, type AdminOrg, type AdminOrgMember, type OrgMemberRole } from '@/services/adminApi'
import { useToast } from '@/composables/useToast'
import { resolveApiErrorMessage } from '@/i18n/error'

interface Props { id: string }
const props = defineProps<Props>()
const router = useRouter()
const api = useAdminApi()
const toast = useToast()

const org = ref<AdminOrg | null>(null)
const members = ref<AdminOrgMember[]>([])
const loading = ref(true)

const ROLE_OPTIONS: { value: OrgMemberRole; label: string }[] = [
  { value: 'admin', label: '管理员' },
  { value: 'operator', label: '操作员' },
  { value: 'member', label: '成员' },
]

async function reloadMembers() {
  members.value = await api.fetchOrgMembers(props.id)
}

onMounted(async () => {
  try {
    org.value = await api.fetchOrg(props.id)
    await reloadMembers()
  } catch (e: unknown) {
    toast.error(resolveApiErrorMessage(e, '加载组织信息失败'))
  } finally {
    loading.value = false
  }
})

/** 统一时间展示：YYYY/MM/DD HH:mm:ss（本地时区） */
function formatDateTime(iso: string | undefined): string {
  if (!iso) return '-'
  const d = new Date(iso)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

// 修改成员角色后刷新列表
async function onRoleChange(m: AdminOrgMember, ev: Event) {
  const role = (ev.target as HTMLSelectElement).value as OrgMemberRole
  try {
    await api.updateOrgMember(props.id, m.user_id, role)
    toast.success(`已将 ${m.user_name || m.user_email} 设为${ROLE_OPTIONS.find(r => r.value === role)?.label}`)
    await reloadMembers()
  } catch (e: unknown) {
    toast.error(resolveApiErrorMessage(e, '角色更新失败'))
  }
}

// 移除成员后刷新列表
async function onRemove(m: AdminOrgMember) {
  const who = m.user_name || m.user_email || m.user_id
  if (!confirm(`确定将 ${who} 移出组织「${org.value?.name}」？`)) return
  try {
    await api.removeOrgMember(props.id, m.user_id)
    toast.success('成员已移除')
    await reloadMembers()
  } catch (e: unknown) {
    toast.error(resolveApiErrorMessage(e, '移除成员失败'))
  }
}

// ── 添加成员弹窗：搜索全局用户 → 选择 → 定角色 ──────────────────────
const showAdd = ref(false)
const userQuery = ref('')
const searching = ref(false)
const searchResults = ref<Awaited<ReturnType<typeof api.fetchUsers>>['data']>([])
const pickedUserId = ref<string | null>(null)
const pickedUserName = ref('')
const addRole = ref<OrgMemberRole>('member')
const adding = ref(false)
// 选中用户当前已属的其他组织（一人一组织规则：添加前提醒，确认后移出原组织再加入）
const pickedUserOrgs = ref<Awaited<ReturnType<typeof api.fetchUserOrgs>>>([])
const checkingOrgs = ref(false)

const existingMemberIds = computed(() => new Set(members.value.map(m => m.user_id)))

function openAdd() {
  showAdd.value = true
  userQuery.value = ''
  searchResults.value = []
  pickedUserId.value = null
  pickedUserName.value = ''
  pickedUserOrgs.value = []
  addRole.value = 'member'
}

let searchTimer: ReturnType<typeof setTimeout> | null = null
function onSearchInput() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(async () => {
    if (!userQuery.value.trim()) {
      searchResults.value = []
      return
    }
    searching.value = true
    try {
      const res = await api.fetchUsers({ q: userQuery.value.trim(), page: 1, pageSize: 8 })
      // 已是成员的用户不出现在候选里
      searchResults.value = res.data.filter(u => !existingMemberIds.value.has(u.id))
    } finally {
      searching.value = false
    }
  }, 300)
}

function pickUser(u: { id: string; name: string }) {
  pickedUserId.value = u.id
  pickedUserName.value = u.name
  // 选中后查该用户已属组织，属其他组织时弹窗内提醒（一人一组织）
  pickedUserOrgs.value = []
  checkingOrgs.value = true
  api.fetchUserOrgs(u.id)
    .then(orgs => { pickedUserOrgs.value = orgs })
    .catch(() => { pickedUserOrgs.value = [] })
    .finally(() => { checkingOrgs.value = false })
}

async function submitAdd() {
  if (!pickedUserId.value) {
    toast.warning('请先搜索并选择要添加的用户')
    return
  }
  // 一人一组织：已在其他组织时先确认，确认后移出原组织再加入当前组织
  const otherOrgs = pickedUserOrgs.value.filter(o => o.org_id !== props.id)
  if (otherOrgs.length > 0) {
    const names = otherOrgs.map(o => o.org_name).join('、')
    if (!confirm(`「${pickedUserName.value}」已属于组织「${names}」。\n\n一个成员只能属于一个组织，确认添加将自动将其移出原组织并加入「${org.value?.name}」。是否继续？`)) {
      return
    }
  }
  adding.value = true
  try {
    for (const o of otherOrgs) {
      await api.removeOrgMember(o.org_id, pickedUserId.value)
    }
    await api.addOrgMember(props.id, pickedUserId.value, addRole.value)
    toast.success(
      otherOrgs.length > 0
        ? `已将 ${pickedUserName.value} 从「${otherOrgs.map(o => o.org_name).join('、')}」移动到本组织`
        : `已添加 ${pickedUserName.value} 到组织`,
    )
    showAdd.value = false
    await reloadMembers()
  } catch (e: unknown) {
    toast.error(resolveApiErrorMessage(e, '添加成员失败'))
    await reloadMembers()
  } finally {
    adding.value = false
  }
}
</script>

<template>
  <div class="p-6 space-y-5">
    <!-- 页头：返回 + 组织基础信息（原概览数据内联）+ 添加成员 -->
    <div class="flex items-start justify-between gap-4">
      <div>
        <button
          class="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors mb-2"
          @click="router.push('/admin/orgs')"
        >
          <ArrowLeft class="w-4 h-4" />
          返回组织列表
        </button>
        <h2 class="text-lg font-semibold flex items-center gap-2">
          {{ org?.name ?? '…' }}
        </h2>
        <p v-if="org" class="text-sm text-muted-foreground mt-0.5 tabular-nums">
          {{ org.slug }} · {{ org.instance_count }}/{{ org.max_instances }} 实例 · 成员 {{ members.length }} 人
        </p>
      </div>
      <button
        class="flex items-center gap-2 h-9 px-4 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors shrink-0"
        @click="openAdd"
      >
        <Plus class="w-4 h-4" />
        添加成员
      </button>
    </div>

    <div v-if="loading" class="flex items-center justify-center py-16">
      <Loader2 class="w-5 h-5 animate-spin text-muted-foreground" />
    </div>

    <!-- 成员列表 -->
    <div v-else class="rounded-xl border border-border overflow-hidden">
      <table class="w-full text-sm">
        <thead>
          <tr class="border-b border-border bg-muted/40">
            <th class="text-left font-medium text-muted-foreground px-4 py-3 w-[45%]">用户</th>
            <th class="text-left font-medium text-muted-foreground px-4 py-3 w-[20%]">角色</th>
            <th class="text-left font-medium text-muted-foreground px-4 py-3 w-[20%]">加入时间</th>
            <th class="text-left font-medium text-muted-foreground px-4 py-3 w-[15%]">操作</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-border">
          <tr
            v-for="m in members"
            :key="m.user_id"
            class="hover:bg-muted/20 cursor-pointer transition-colors"
            title="点击查看用户详情"
            @click="router.push(`/admin/users/${m.user_id}`)"
          >
            <td class="px-4 py-3">
              <div class="flex items-center gap-2.5">
                <span class="w-7 h-7 rounded-full bg-primary/15 text-primary flex items-center justify-center text-xs font-semibold shrink-0">
                  {{ (m.user_name || '?').slice(0, 1) }}
                </span>
                <div class="min-w-0">
                  <div class="font-medium">{{ m.user_name || '-' }}</div>
                  <!-- 无邮箱用户不显示邮箱行（姓名注册账号） -->
                  <div v-if="m.user_email" class="text-xs text-muted-foreground truncate">{{ m.user_email }}</div>
                </div>
              </div>
            </td>
            <td class="px-4 py-3" @click.stop>
              <select
                :value="m.role"
                class="text-sm border border-border rounded-md px-2 py-1 bg-background focus:outline-none focus:ring-1 focus:ring-primary"
                @change="onRoleChange(m, $event)"
              >
                <option v-for="r in ROLE_OPTIONS" :key="r.value" :value="r.value">{{ r.label }}</option>
              </select>
            </td>
            <td class="px-4 py-3 text-muted-foreground text-xs tabular-nums">{{ formatDateTime(m.joined_at) }}</td>
            <td class="px-4 py-3" @click.stop>
              <button
                class="inline-flex items-center gap-1 text-xs px-2 py-1 rounded-md border border-destructive/40 text-destructive hover:bg-destructive/10 transition-colors"
                @click="onRemove(m)"
              >
                <Trash2 class="w-3 h-3" />
                移除
              </button>
            </td>
          </tr>
          <tr v-if="members.length === 0">
            <td colspan="4" class="px-4 py-12 text-center text-sm text-muted-foreground">
              暂无成员，点击右上角「添加成员」加入用户
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 添加成员弹窗：搜索用户 → 选择 → 角色 -->
    <div
      v-if="showAdd"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      @click.self="showAdd = false"
    >
      <div class="w-full max-w-md rounded-xl border border-border bg-card shadow-xl">
        <div class="flex items-center justify-between px-5 py-4 border-b border-border">
          <h3 class="font-semibold flex items-center gap-2">
            <UserPlus class="w-4 h-4 text-primary" />
            添加成员到「{{ org?.name }}」
          </h3>
          <button class="p-1 rounded hover:bg-muted/60" @click="showAdd = false">
            <X class="w-4 h-4" />
          </button>
        </div>

        <div class="px-5 py-4 space-y-4">
          <!-- 搜索全局用户 -->
          <div>
            <label class="block text-sm font-medium mb-1.5">搜索用户（姓名 / 邮箱）</label>
            <div class="relative">
              <Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
              <input
                v-model="userQuery"
                type="text"
                placeholder="输入关键词，自动搜索..."
                class="h-9 w-full pl-9 pr-3 rounded-lg border border-border bg-background text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
                @input="onSearchInput"
              />
            </div>
            <div v-if="searching" class="flex justify-center py-3">
              <Loader2 class="w-4 h-4 animate-spin text-muted-foreground" />
            </div>
            <div v-else-if="searchResults.length" class="mt-2 rounded-lg border border-border divide-y divide-border max-h-52 overflow-y-auto">
              <button
                v-for="u in searchResults"
                :key="u.id"
                class="w-full flex items-center gap-2.5 px-3 py-2 text-left hover:bg-muted/40 transition-colors"
                :class="pickedUserId === u.id ? 'bg-primary/10' : ''"
                @click="pickUser(u)"
              >
                <span class="w-7 h-7 rounded-full bg-primary/15 text-primary flex items-center justify-center text-xs font-semibold shrink-0">
                  {{ (u.name || '?').slice(0, 1) }}
                </span>
                <div class="min-w-0">
                  <div class="text-sm font-medium">{{ u.name || '-' }}</div>
                  <div class="text-xs text-muted-foreground truncate">{{ u.email || u.id.slice(0, 8) + '…' }}</div>
                </div>
                <span v-if="pickedUserId === u.id" class="ml-auto text-primary text-xs font-medium shrink-0">已选</span>
              </button>
            </div>
            <p v-else-if="userQuery.trim()" class="text-xs text-muted-foreground mt-2">
              未找到可添加的用户（已是成员的用户不再显示）
            </p>
          </div>

          <!-- 一人一组织：选中用户已属其他组织时警示，提交时确认移动 -->
          <div
            v-if="pickedUserId && !checkingOrgs && pickedUserOrgs.filter(o => o.org_id !== id).length"
            class="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-600 dark:text-amber-400"
          >
            <AlertTriangle class="w-4 h-4 shrink-0 mt-0.5" />
            <p>
              该用户已属于组织「{{ pickedUserOrgs.filter(o => o.org_id !== id).map(o => o.org_name).join('、') }}」。
              一个成员只能属于一个组织，添加后将自动移出原组织。
            </p>
          </div>

          <!-- 角色选择 -->
          <div>
            <label class="block text-sm font-medium mb-1.5">角色</label>
            <div class="flex gap-2">
              <button
                v-for="r in ROLE_OPTIONS"
                :key="r.value"
                class="flex-1 px-3 py-1.5 rounded-lg border text-sm transition-colors"
                :class="addRole === r.value
                  ? 'border-primary/50 bg-primary/10 text-primary font-medium'
                  : 'border-border text-muted-foreground hover:border-primary/40'"
                @click="addRole = r.value"
              >
                {{ r.label }}
              </button>
            </div>
          </div>
        </div>

        <div class="flex items-center justify-end gap-2 px-5 py-4 border-t border-border">
          <button
            class="h-9 px-4 rounded-lg border border-border text-sm hover:bg-muted/50 transition-colors"
            @click="showAdd = false"
          >
            取消
          </button>
          <button
            class="h-9 px-4 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50"
            :disabled="adding || !pickedUserId"
            @click="submitAdd"
          >
            <Loader2 v-if="adding" class="w-4 h-4 animate-spin" />
            <template v-else>添加</template>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
