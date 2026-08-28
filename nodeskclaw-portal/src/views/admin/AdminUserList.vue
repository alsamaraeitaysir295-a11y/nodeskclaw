<!-- 全局用户列表页：搜索 + 分页表格 + 新建账号 + 行点击进详情 + 重置密码/启停/超管切换 -->
<template>
  <div class="p-6 space-y-5">
    <!-- 标题 + 新建账号 -->
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-lg font-semibold">用户管理</h2>
        <p class="text-sm text-muted-foreground mt-0.5">管理平台全部账号</p>
      </div>
      <button
        class="flex items-center gap-2 h-9 px-4 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors"
        @click="showCreate = true"
      >
        <Plus class="w-4 h-4" />
        新建账号
      </button>
    </div>

    <!-- 搜索框 -->
    <div class="relative max-w-md">
      <Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
      <input
        v-model="q"
        type="text"
        placeholder="按 email / 姓名搜索，回车查询..."
               class="h-9 w-full pl-9 pr-3 rounded-lg border border-border bg-background text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
        @keyup.enter="reload(1)"
      />
    </div>

    <!-- 用户表格 -->
    <div class="rounded-xl border border-border overflow-hidden">
      <table class="w-full text-sm">
        <thead>
          <tr class="border-b border-border bg-muted/40">
            <th class="text-left font-medium text-muted-foreground px-4 py-3 w-[35%]">用户</th>
            <th class="text-left font-medium text-muted-foreground px-4 py-3 w-[10%]">状态</th>
            <th class="text-left font-medium text-muted-foreground px-4 py-3 w-[10%]">组织数</th>
            <th class="text-left font-medium text-muted-foreground px-4 py-3 w-[20%]">创建时间</th>
            <th class="text-left font-medium text-muted-foreground px-4 py-3 w-[25%]">操作</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-border">
          <tr
            v-for="u in users"
            :key="u.id"
            class="hover:bg-muted/20 cursor-pointer transition-colors"
            @click="$router.push(`/admin/users/${u.id}`)"
          >
            <td class="px-4 py-3">
              <div class="font-medium flex items-center gap-2">
                <span class="w-7 h-7 rounded-full bg-primary/15 text-primary flex items-center justify-center text-xs font-semibold shrink-0">
                  {{ (u.name || '?').slice(0, 1) }}
                </span>
                {{ u.name || '-' }}
                <span
                  v-if="u.is_super_admin"
                  class="text-xs px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-600 dark:text-amber-400 font-medium"
                >
                  超管
                </span>
              </div>
              <!-- 无邮箱用户（姓名注册）回退显示 ID 截短，避免空白行看起来像脏数据 -->
              <div class="text-xs text-muted-foreground truncate mt-0.5">{{ u.email || `${u.id.slice(0, 8)}…` }}</div>
            </td>
            <td class="px-4 py-3">
              <span
                class="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium"
                :class="u.is_active
                  ? 'bg-green-500/15 text-green-600 dark:text-green-400'
                  : 'bg-muted text-muted-foreground'"
              >
                {{ u.is_active ? '启用' : '禁用' }}
              </span>
            </td>
            <td class="px-4 py-3 text-muted-foreground tabular-nums">{{ u.org_count }}</td>
            <td class="px-4 py-3 text-muted-foreground text-xs tabular-nums">{{ formatDateTime(u.created_at) }}</td>
            <td class="px-4 py-3" @click.stop>
              <div class="flex items-center gap-1.5">
                <button
                  class="text-xs px-2 py-1 rounded-md border border-border hover:border-primary/50 hover:text-primary transition-colors"
                  @click="onReset(u)"
                >
                  重置密码
                </button>
                <button
                  class="text-xs px-2 py-1 rounded-md border transition-colors"
                  :class="u.is_super_admin
                    ? 'border-amber-500/40 text-amber-600 dark:text-amber-400 hover:bg-amber-500/10'
                    : 'border-border hover:border-amber-500/50 hover:text-amber-600 dark:hover:text-amber-400'"
                  :title="u.is_super_admin ? '取消超管权限' : '设为超管'"
                  @click="onToggleSuperAdmin(u)"
                >
                  {{ u.is_super_admin ? '取消超管' : '设为超管' }}
                </button>
                <button
                  class="text-xs px-2 py-1 rounded-md border transition-colors"
                  :class="u.is_active
                    ? 'border-destructive/40 text-destructive hover:bg-destructive/10'
                    : 'border-border hover:border-primary/50 hover:text-primary'"
                  @click="onToggleActive(u)"
                >
                  {{ u.is_active ? '禁用' : '启用' }}
                </button>
                <button
                  class="text-xs px-2 py-1 rounded-md border border-destructive/40 text-destructive hover:bg-destructive/10 transition-colors"
                  @click="onDelete(u)"
                >
                  删除
                </button>
              </div>
            </td>
          </tr>
          <tr v-if="users.length === 0">
            <td colspan="5" class="px-4 py-12 text-center text-sm text-muted-foreground">暂无用户</td>
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

    <!-- 新建账号弹窗 -->
    <div
      v-if="showCreate"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      @click.self="showCreate = false"
    >
      <div class="w-full max-w-md rounded-xl border border-border bg-card shadow-xl">
        <div class="flex items-center justify-between px-5 py-4 border-b border-border">
          <h3 class="font-semibold">新建账号</h3>
          <button class="p-1 rounded hover:bg-muted/60" @click="showCreate = false">
            <X class="w-4 h-4" />
          </button>
        </div>
        <div class="px-5 py-4 space-y-4">
          <div>
            <label class="block text-sm font-medium mb-1.5">姓名 *</label>
            <input
              v-model="createForm.name"
              type="text"
              class="h-9 w-full px-3 rounded-lg border border-border bg-background text-sm focus:outline-none focus:ring-1 focus:ring-primary"
              placeholder="用户姓名"
            />
          </div>
          <div>
            <label class="block text-sm font-medium mb-1.5">邮箱（可选）</label>
            <input
              v-model="createForm.email"
              type="text"
              class="h-9 w-full px-3 rounded-lg border border-border bg-background text-sm focus:outline-none focus:ring-1 focus:ring-primary"
              placeholder="user@example.com"
            />
            <p class="text-xs text-muted-foreground mt-1">留空则为姓名注册账号；邮箱在平台内不可重复</p>
          </div>
          <label class="flex items-center gap-2 text-sm cursor-pointer">
            <input v-model="createForm.is_super_admin" type="checkbox" class="accent-primary" />
            设为超级管理员
          </label>
          <p class="text-xs text-muted-foreground">创建成功后生成一次性临时密码，首次登录强制修改。</p>
        </div>
        <div class="flex items-center justify-end gap-2 px-5 py-4 border-t border-border">
          <button
            class="h-9 px-4 rounded-lg border border-border text-sm hover:bg-muted/50 transition-colors"
            @click="showCreate = false"
          >
            取消
          </button>
          <button
            class="h-9 px-4 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50"
            :disabled="creating"
            @click="submitCreate"
          >
            <Loader2 v-if="creating" class="w-4 h-4 animate-spin" />
            <template v-else>创建</template>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Plus, Search, X, Loader2 } from 'lucide-vue-next'
import { useAdminApi, type AdminUser } from '@/services/adminApi'
import { useToast } from '@/composables/useToast'
import { resolveApiErrorMessage } from '@/i18n/error'

const api = useAdminApi()
const toast = useToast()
const q = ref('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const users = ref<AdminUser[]>([])

// ── 新建账号 ──────────────────────────────
const showCreate = ref(false)
const creating = ref(false)
const createForm = ref({ name: '', email: '', is_super_admin: false })

/** 统一时间展示：YYYY/MM/DD HH:mm:ss（本地时区） */
function formatDateTime(iso: string | undefined): string {
  if (!iso) return '-'
  const d = new Date(iso)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

async function reload(p = page.value) {
  page.value = p
  const res = await api.fetchUsers({ q: q.value || undefined, page: p, pageSize: pageSize.value })
  users.value = res.data
  total.value = res.pagination.total
}
onMounted(() => reload(1))

async function submitCreate() {
  const name = createForm.value.name.trim()
  if (!name) {
    toast.warning('姓名不能为空')
    return
  }
  creating.value = true
  try {
    const { user, temp_password } = await api.createUser({
      name,
      email: createForm.value.email.trim() || undefined,
      is_super_admin: createForm.value.is_super_admin,
    })
    showCreate.value = false
    createForm.value = { name: '', email: '', is_super_admin: false }
    window.alert(`账号已创建：${user.name}\n临时密码：${temp_password}\n请复制并交付用户，关闭后无法再次查看。`)
    await reload(1)
  } catch (e: unknown) {
    toast.error(resolveApiErrorMessage(e, '创建账号失败'))
  } finally {
    creating.value = false
  }
}

async function onReset(u: AdminUser) {
  if (!confirm(`为 ${u.email || u.name} 重置密码？将生成一次性临时密码。`)) return
  const { temp_password } = await api.resetUserPassword(u.id)
  window.alert(`临时密码：${temp_password}\n请复制并交付用户，关闭后无法再次查看。`)
}

async function onToggleActive(u: AdminUser) {
  await api.updateUser(u.id, { is_active: !u.is_active })
  await reload()
}

async function onToggleSuperAdmin(u: AdminUser) {
  const next = !u.is_super_admin
  if (!confirm(next ? `确认将 ${u.email || u.name} 设为超级管理员？` : `确认取消 ${u.email || u.name} 的超管权限？`)) return
  try {
    await api.updateUser(u.id, { is_super_admin: next })
    toast.success(next ? '已设为超管' : '已取消超管')
    await reload()
  } catch (e: unknown) {
    toast.error(resolveApiErrorMessage(e, '操作失败'))
  }
}

/** 删除用户：后端级联软删（组织/管理员成员关系、个人 Key 等），自我保护与最后超管守卫在后端 */
async function onDelete(u: AdminUser) {
  const who = u.email || u.name || u.id
  if (!confirm(`确定删除用户「${who}」？\n\n将同时移除其组织成员关系、管理员身份与个人模型 Key（软删除，数据可恢复）。`)) return
  try {
    await api.deleteUser(u.id)
    toast.success(`用户 ${who} 已删除`)
    await reload()
  } catch (e: unknown) {
    toast.error(resolveApiErrorMessage(e, '删除用户失败'))
  }
}
</script>
