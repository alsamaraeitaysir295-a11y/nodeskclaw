// nodeskclaw-portal/src/stores/theme.spec.ts
// 验证主题 store：
// 1. 无本地记录时默认深色，并在 <html> 上挂 .dark 类
// 2. toggle() 在明/暗间切换，同步 localStorage 与 <html> 类
// 3. 重建 store 时能恢复已保存的主题；非法存储值回退深色
import { describe, it, expect, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useThemeStore } from './theme'

function htmlClasses(): string {
  return document.documentElement.className
}

describe('theme store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    document.documentElement.classList.remove('dark')
  })

  it('defaults to dark and applies the .dark class on <html>', () => {
    const store = useThemeStore()

    expect(store.theme).toBe('dark')
    expect(htmlClasses()).toContain('dark')
  })

  it('toggle switches to light, persists, and removes the .dark class', () => {
    const store = useThemeStore()

    store.toggle()

    expect(store.theme).toBe('light')
    expect(localStorage.getItem('portal_theme')).toBe('light')
    expect(htmlClasses()).not.toContain('dark')
  })

  it('toggle twice returns to dark and re-applies the .dark class', () => {
    const store = useThemeStore()

    store.toggle()
    store.toggle()

    expect(store.theme).toBe('dark')
    expect(localStorage.getItem('portal_theme')).toBe('dark')
    expect(htmlClasses()).toContain('dark')
  })

  it('restores the saved light theme on store creation', () => {
    localStorage.setItem('portal_theme', 'light')

    const store = useThemeStore()

    expect(store.theme).toBe('light')
    expect(htmlClasses()).not.toContain('dark')
  })

  it('falls back to dark when the stored value is invalid', () => {
    localStorage.setItem('portal_theme', 'blue')

    const store = useThemeStore()

    expect(store.theme).toBe('dark')
    expect(htmlClasses()).toContain('dark')
  })

  it('setTheme applies the theme immediately', () => {
    const store = useThemeStore()

    store.setTheme('light')

    expect(store.theme).toBe('light')
    expect(htmlClasses()).not.toContain('dark')

    store.setTheme('dark')

    expect(store.theme).toBe('dark')
    expect(htmlClasses()).toContain('dark')
  })
})
