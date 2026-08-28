<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAdminApi } from '@/services/adminApi'
import { useToast } from '@/composables/useToast'
import { useFeature } from '@/composables/useFeature'
import { resolveApiErrorMessage } from '@/i18n/error'
import { Building2, Plus, Pencil, Trash2, Loader2, Search, X, Check } from 'lucide-vue-next'

const router = useRouter()
const toast = useToast()
const { isEnabled: platformAdminEnabled } = useFeature('platform_admin')
const { fetchOrgs, createOrg, updateOrg, deleteOrg } = useAdminApi()

const loading = ref(true)
const orgs = ref<Awaited<ReturnType<typeof fetchOrgs>>>([])
const search = ref('')
const showCreate = ref(false)
const createLoading = ref(false)
const deleteConfirm = ref<string | null>(null)
const deleteLoading = ref(false)

// 编辑状态：editingOrg 保存当前被编辑的组织 id，isEditing 区分创建 vs 编辑
const isEditing = ref(false)
const editingOrgId = ref<string | null>(null)

const form = ref({
  name: '',
  slug: '',
  plan: 'free',
  max_instances: 1,
  max_cpu_total: '4',
  max_mem_total: '8Gi',
  max_storage_total: '500Gi',
  max_collaboration_depth: 3,
  cluster_id: null as string | null,
})

const filteredOrgs = computed(() => {
  if (!search.value) return orgs.value
  const q = search.value.toLowerCase()
  return orgs.value.filter(
    o => o.name.toLowerCase().includes(q) || o.slug.toLowerCase().includes(q),
  )
})

function resetForm() {
  form.value = { name: '', slug: '', plan: 'free', max_instances: 1, max_cpu_total: '4', max_mem_total: '8Gi', max_storage_total: '500Gi', max_collaboration_depth: 3, cluster_id: null }
}

function cancelCreate() {
  showCreate.value = false
  isEditing.value = false
  editingOrgId.value = null
  resetForm()
}

// 打开编辑弹窗：预填当前行数据
function openEdit(org: Awaited<ReturnType<typeof fetchOrgs>>[number]) {
  isEditing.value = true
  editingOrgId.value = org.id
  form.value = {
    name: org.name,
    slug: org.slug,
    plan: org.plan,
    max_instances: org.max_instances,
    max_cpu_total: org.max_cpu_total,
    max_mem_total: org.max_mem_total,
    max_storage_total: org.max_storage_total,
    max_collaboration_depth: org.max_collaboration_depth,
    cluster_id: org.cluster_id ?? null,
  }
  showCreate.value = true
}

async function submitCreate() {
  if (!form.value.name.trim() || !form.value.slug.trim()) {
    toast.warning('名称和 Slug 不能为空')
    return
  }
  createLoading.value = true
  try {
    if (isEditing.value && editingOrgId.value) {
      // 编辑模式：调 updateOrg 并更新列表中对应项
      const updated = await updateOrg(editingOrgId.value, form.value)
      const idx = orgs.value.findIndex(o => o.id === editingOrgId.value)
      if (idx !== -1) orgs.value[idx] = updated
      toast.success('组织已更新')
    } else {
      const newOrg = await createOrg(form.value)
      orgs.value.push(newOrg)
      toast.success('组织已创建')
    }
    showCreate.value = false
    isEditing.value = false
    editingOrgId.value = null
    resetForm()
  } catch (e: unknown) {
    toast.error(resolveApiErrorMessage(e, isEditing.value ? '更新组织失败' : '创建组织失败'))
  } finally {
    createLoading.value = false
  }
}

async function submitDelete() {
  if (!deleteConfirm.value) return
  deleteLoading.value = true
  try {
    await deleteOrg(deleteConfirm.value)
    orgs.value = orgs.value.filter(o => o.id !== deleteConfirm.value)
    toast.success('组织已删除')
    deleteConfirm.value = null
  } catch (e: unknown) {
    toast.error(resolveApiErrorMessage(e, '删除组织失败'))
  } finally {
    deleteLoading.value = false
  }
}

function formatDate(iso: string | undefined): string {
  if (!iso) return '-'
  return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

onMounted(async () => {
  try {
    orgs.value = await fetchOrgs()
  } catch (e: unknown) {
    toast.error(resolveApiErrorMessage(e, '加载组织列表失败'))
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="p-6 space-y-5">
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-lg font-semibold">组织管理</h2>
        <p class="text-sm text-muted-foreground mt-0.5">管理所有租户组织</p>
      </div>
      <button
        class="flex items-center gap-2 h-9 px-4 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors"
        @click="showCreate = true; resetForm()"
      >
        <Plus class="w-4 h-4" />
        新建组织
      </button>
    </div>

    <div class="relative">
      <Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
      <input
        v-model="search"
        type="text"
        placeholder="搜索组织名称或 Slug..."
        class="h-9 w-full pl-9 pr-3 rounded-lg border border-border bg-background text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
      />
      <button v-if="search" class="absolute right-3 top-1/2 -translate-y-1/2" @click="search = ''">
        <X class="w-4 h-4 text-muted-foreground" />
      </button>
    </div>

    <div v-if="loading" class="flex items-center justify-center py-16">
      <Loader2 class="w-5 h-5 animate-spin text-muted-foreground" />
    </div>

    <div v-else class="rounded-xl border border-border overflow-hidden">
      <table class="w-full text-sm">
        <thead>
          <tr class="border-b border-border bg-muted/40">
            <th class="text-left font-medium text-muted-foreground px-4 py-3">名称</th>
            <th class="text-left font-medium text-muted-foreground px-4 py-3 hidden md:table-cell">Slug</th>
            <th class="text-right font-medium text-muted-foreground px-4 py-3">实例数</th>
            <th class="text-right font-medium text-muted-foreground px-4 py-3 hidden sm:table-cell">创建时间</th>
            <th class="text-right font-medium text-muted-foreground px-4 py-3">操作</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-border">
          <tr v-for="org in filteredOrgs" :key="org.id" class="hover:bg-muted/20 transition-colors cursor-pointer" @click="router.push(`/admin/orgs/${org.id}`)">
            <td class="px-4 py-3">
              <div class="flex items-center gap-2">
                <Building2 class="w-4 h-4 text-muted-foreground shrink-0" />
                <span class="font-medium">{{ org.name }}</span>
                <span v-if="!org.is_active" class="inline-flex items-center px-1.5 py-0.5 rounded text-xs bg-red-500/15 text-red-400">
                  已停用
                </span>
              </div>
            </td>
            <td class="px-4 py-3 hidden md:table-cell">
              <span class="font-mono text-xs text-muted-foreground">{{ org.slug }}</span>
            </td>
            <td class="px-4 py-3 text-right">
              <span class="font-mono">{{ org.instance_count }}</span>
              <span class="text-muted-foreground"> / {{ org.max_instances }}</span>
            </td>
            <td class="px-4 py-3 text-right text-muted-foreground hidden sm:table-cell">
              {{ formatDate(org.created_at) }}
            </td>
            <!-- @click.stop 防止行点击冒泡触发路由跳转 -->
            <td class="px-4 py-3 text-right" @click.stop>
              <div class="flex items-center justify-end gap-1">
                <button class="p-1.5 rounded hover:bg-muted/60 text-muted-foreground hover:text-foreground transition-colors" title="编辑" @click="openEdit(org)">
                  <Pencil class="w-3.5 h-3.5" />
                </button>
                <button class="p-1.5 rounded hover:bg-red-500/10 text-muted-foreground hover:text-red-400 transition-colors" title="删除" @click="deleteConfirm = org.id">
                  <Trash2 class="w-3.5 h-3.5" />
                </button>
              </div>
            </td>
          </tr>
          <tr v-if="filteredOrgs.length === 0">
            <td colspan="6" class="px-4 py-12 text-center text-muted-foreground">
              {{ search ? '未找到匹配的组织' : '暂无组织数据' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Create Dialog -->
    <div v-if="showCreate" class="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div class="w-full max-w-md rounded-xl border border-border bg-card shadow-xl">
        <div class="flex items-center justify-between px-5 py-4 border-b border-border">
          <h3 class="font-semibold">{{ isEditing ? '编辑组织' : '新建组织' }}</h3>
          <button class="p-1 rounded hover:bg-muted/60" @click="cancelCreate">
            <X class="w-4 h-4" />
          </button>
        </div>
        <div class="px-5 py-4 space-y-4">
          <div>
            <label class="block text-sm font-medium mb-1.5">组织名称 *</label>
            <input v-model="form.name" type="text" class="h-9 w-full px-3 rounded-lg border border-border bg-background text-sm focus:outline-none focus:ring-1 focus:ring-primary" placeholder="如：ABC 制造" />
          </div>
          <div>
            <label class="block text-sm font-medium mb-1.5">Slug *</label>
            <input v-model="form.slug" type="text" class="h-9 w-full px-3 rounded-lg border border-border bg-background text-sm font-mono focus:outline-none focus:ring-1 focus:ring-primary" placeholder="abc-manufacturing" />
          </div>
          <div class="grid grid-cols-2 gap-4">
            <div>
              <label class="block text-sm font-medium mb-1.5">最大实例数</label>
              <input v-model.number="form.max_instances" type="number" min="1" class="h-9 w-full px-3 rounded-lg border border-border bg-background text-sm focus:outline-none focus:ring-1 focus:ring-primary" />
              <p class="text-xs text-muted-foreground mt-1">部署 AI 员工时按此上限拦截</p>
            </div>
          </div>
        </div>
        <div class="flex items-center justify-end gap-2 px-5 py-4 border-t border-border">
          <button class="h-9 px-4 rounded-lg border border-border text-sm hover:bg-muted/50 transition-colors" @click="cancelCreate">
            取消
          </button>
          <button class="h-9 px-4 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50" :disabled="createLoading" @click="submitCreate">
            <Loader2 v-if="createLoading" class="w-4 h-4 animate-spin" />
            <template v-else><Check class="w-4 h-4 inline mr-1" />{{ isEditing ? '保存' : '创建' }}</template>
          </button>
        </div>
      </div>
    </div>

    <!-- Delete Confirm -->
    <div v-if="deleteConfirm" class="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div class="w-full max-w-sm rounded-xl border border-border bg-card shadow-xl">
        <div class="px-5 py-4 border-b border-border">
          <h3 class="font-semibold">删除组织</h3>
        </div>
        <div class="px-5 py-4">
          <p class="text-sm text-muted-foreground">确定要删除此组织吗？删除后无法恢复。</p>
        </div>
        <div class="flex items-center justify-end gap-2 px-5 py-4 border-t border-border">
          <button class="h-9 px-4 rounded-lg border border-border text-sm hover:bg-muted/50 transition-colors" @click="deleteConfirm = null">
            取消
          </button>
          <button class="h-9 px-4 rounded-lg bg-red-500 text-white text-sm font-medium hover:bg-red-600 transition-colors disabled:opacity-50" :disabled="deleteLoading" @click="submitDelete">
            <Loader2 v-if="deleteLoading" class="w-4 h-4 animate-spin" />
            <template v-else>删除</template>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>