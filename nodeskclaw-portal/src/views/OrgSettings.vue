<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { useOrgStore } from '@/stores/org'
import { useAuthStore } from '@/stores/auth'
import { useFeature } from '@/composables/useFeature'
import { hasOrgRoleLevel, type OrgRoleName } from '@/utils/orgRole'
import { Settings, Users, Dna, FolderOpen, Mail, Server, Building2, Container, ScrollText, Globe, Cpu, Layers, KeyRound, BarChart3 } from 'lucide-vue-next'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const orgStore = useOrgStore()
const authStore = useAuthStore()

interface NavItem {
  name: string
  label: () => string
  icon: typeof Settings
  matchPrefix?: string
  feature?: string  // 关联的 feature_id；未启用时该 Tab 不展示
  minRole: OrgRoleName  // 最低组织角色门槛，驱动侧边栏是否展示该子页面
}

// 组织设置侧边栏导航项；EE/CE 现已共享同一份菜单（集群/Registry/SMTP 等不再 CE 独占）
// minRole：member 可见的仅组织信息/人类成员两页，operator 及以上还能看到集群/Registry 等运营类页面，
// 操作审计（`/{org_id}/audit-logs`）与 LLM 用量分析（`/orgs/{org_id}/token-analytics`）后端接口都是
// require_org_admin，operator 打开会全程 403，故这两页门槛设为 admin，与后端实际权限对齐
const allNavItems: NavItem[] = [
  { name: 'OrgInfo', label: () => t('orgSettings.orgInfo'), icon: Building2, minRole: 'member' },
  { name: 'OrgSettingsClusters', label: () => t('orgSettings.clusters'), icon: Server, minRole: 'operator' },
  { name: 'OrgSettingsRegistry', label: () => t('orgSettings.registryTitle'), icon: Container, minRole: 'operator' },
  { name: 'OrgSettingsEngineVersions', label: () => t('orgSettings.engineVersionsTab'), icon: Layers, minRole: 'operator' },
  { name: 'OrgSettingsSpecs', label: () => t('orgSettings.specsTab'), icon: Cpu, minRole: 'operator' },
  { name: 'OrgMembers', label: () => t('orgSettings.humanMembers'), icon: Users, minRole: 'member' },
  { name: 'OrgSettingsLlmKeys', label: () => t('orgSettings.llmKeysTab'), icon: KeyRound, minRole: 'operator' },
  { name: 'OrgSettingsLlmAnalytics', label: () => t('orgSettings.llmAnalyticsTab'), icon: BarChart3, feature: 'llm_analytics', minRole: 'admin' },
  { name: 'OrgSettingsGenes', label: () => t('orgSettings.requiredGenesTab'), icon: Dna, minRole: 'operator' },
  { name: 'OrgSettingsSmtp', label: () => t('orgSettings.smtpTitle'), icon: Mail, minRole: 'operator' },
  { name: 'OrgSettingsNetwork', label: () => t('orgSettings.networkTab'), icon: Globe, minRole: 'operator' },
  { name: 'OrgEnterpriseFiles', label: () => t('enterpriseFiles.title'), icon: FolderOpen, matchPrefix: '/org-settings/files', minRole: 'operator' },
  { name: 'OrgSettingsAudit', label: () => t('auditLogs.title'), icon: ScrollText, minRole: 'admin' },
]

// 先按"路由是否真实存在"过滤（避免渲染 EE 端未注册的路由，如 OrgEnterpriseFiles），
// 再按关联 feature 是否启用过滤（避免未开通该 feature 的组织点进去被路由守卫重定向），
// 最后按组织角色等级过滤（超管不受角色限制，直接放行）
const navItems = computed(() =>
  allNavItems.filter(item =>
    router.hasRoute(item.name) &&
    (!item.feature || useFeature(item.feature).isEnabled.value) &&
    (authStore.user?.is_super_admin || hasOrgRoleLevel(authStore.user?.portal_org_role, item.minRole))
  )
)

function isActive(item: NavItem): boolean {
  if (item.matchPrefix) return route.path.startsWith(item.matchPrefix)
  return route.name === item.name
}

onMounted(async () => {
  if (!orgStore.currentOrg) await orgStore.fetchMyOrg()
})
</script>

<template>
  <div class="flex flex-col h-[calc(100vh-3.5rem)] max-w-4xl mx-auto px-6">
    <div class="shrink-0 flex items-center gap-3 pt-8 pb-4">
      <Settings class="w-6 h-6 text-primary" />
      <h1 class="text-xl font-bold">{{ t('orgSettings.title') }}</h1>
    </div>

    <div class="flex gap-6 flex-1 min-h-0 pb-8">
      <nav class="w-40 shrink-0 space-y-1">
        <router-link
          v-for="item in navItems"
          :key="item.name"
          :to="{ name: item.name }"
          class="flex items-center gap-2 px-3 py-2 rounded-lg text-sm transition-colors"
          :class="isActive(item)
            ? 'bg-primary/10 text-primary font-medium'
            : 'text-muted-foreground hover:text-foreground hover:bg-muted/50'"
        >
          <component :is="item.icon" class="w-4 h-4" />
          {{ item.label() }}
        </router-link>
      </nav>

      <div class="flex-1 min-w-0 overflow-y-auto pr-3">
        <div class="pb-4">
          <router-view />
        </div>
      </div>
    </div>
  </div>
</template>
