<template>
  <!-- 超管后台布局：左侧导航 + 右侧内容区 -->
  <div class="flex h-screen bg-background">
    <!-- 左侧侧边栏 -->
    <aside class="w-56 shrink-0 border-r border-border bg-card flex flex-col">
      <!-- 返回门户 -->
      <div class="p-4 border-b border-border">
        <router-link
          to="/"
          class="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground transition-colors"
        >
          <Home class="w-4 h-4" />
          <span>返回门户</span>
        </router-link>
      </div>

      <!-- 导航入口 -->
      <nav class="flex-1 p-3 space-y-1">
        <router-link
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          class="flex items-center gap-2.5 px-3 py-2 rounded-md text-sm text-foreground hover:bg-muted transition-colors"
          active-class="bg-muted text-foreground font-medium"
        >
          <component :is="item.icon" class="w-4 h-4 shrink-0" />
          <span>{{ item.label }}</span>
        </router-link>
      </nav>
    </aside>

    <!-- 右侧内容区 -->
    <main class="flex-1 overflow-auto">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import { Home, Building2, Users, ClipboardList } from 'lucide-vue-next'

// 侧边栏导航项定义
// 注：功能开关（/admin/features）暂不展示（产品要求，2026-08-27），恢复时加回即可
const navItems = [
  { to: '/admin/orgs', icon: Building2, label: '组织' },
  { to: '/admin/users', icon: Users, label: '用户' },
  { to: '/admin/audit', icon: ClipboardList, label: '审计日志' },
]
</script>
