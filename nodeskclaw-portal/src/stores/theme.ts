import { defineStore } from 'pinia'
import { ref } from 'vue'

export type Theme = 'light' | 'dark'

const THEME_STORAGE_KEY = 'portal_theme'

function readStoredTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY)
    return stored === 'light' || stored === 'dark' ? stored : 'light'
  } catch {
    return 'light'
  }
}

export const useThemeStore = defineStore('theme', () => {
  // 默认深色（当前应用外观），用户切换后持久化到 localStorage
  const theme = ref<Theme>(readStoredTheme())

  function setTheme(t: Theme) {
    theme.value = t
    try {
      localStorage.setItem(THEME_STORAGE_KEY, t)
    } catch {
      // localStorage 不可用时仅影响持久化，不影响本次会话切换
    }
    apply()
  }

  function toggle() {
    setTheme(theme.value === 'dark' ? 'light' : 'dark')
  }

  // 在 <html> 上挂载/移除 .dark 类，驱动 CSS 变量与 dark: 变体
  function apply() {
    document.documentElement.classList.toggle('dark', theme.value === 'dark')
  }

  // store 创建时立即应用保存的主题
  apply()

  return { theme, setTheme, toggle, apply }
})
