// nodeskclaw-portal/src/stores/theme.spec.ts
// 验证主题 store：
// 1. 无本地记录时默认浅色，<html> 不挂 .dark 类
// 2. toggle() 在明/暗间切换，同步 localStorage 与 <html> 类
// 3. 重建 store 时能恢复已保存的主题；非法存储值回退浅色
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

  it('defaults to light and does not apply the .dark class on <html>', () => {
    const store = useThemeStore()

    expect(store.theme).toBe('light')
    expect(htmlClasses()).not.toContain('dark')
  })

  it('toggle switches to dark, persists, and applies the .dark class', () => {
    const store = useThemeStore()

    store.toggle()

    expect(store.theme).toBe('dark')
    expect(localStorage.getItem('portal_theme')).toBe('dark')
    expect(htmlClasses()).toContain('dark')
  })

  it('toggle twice returns to light and removes the .dark class', () => {
    const store = useThemeStore()

    store.toggle()
    store.toggle()

    expect(store.theme).toBe('light')
    expect(localStorage.getItem('portal_theme')).toBe('light')
    expect(htmlClasses()).not.toContain('dark')
  })

  it('restores the saved dark theme on store creation', () => {
    localStorage.setItem('portal_theme', 'dark')

    const store = useThemeStore()

    expect(store.theme).toBe('dark')
    expect(htmlClasses()).toContain('dark')
  })

  it('falls back to light when the stored value is invalid', () => {
    localStorage.setItem('portal_theme', 'blue')

    const store = useThemeStore()

    expect(store.theme).toBe('light')
    expect(htmlClasses()).not.toContain('dark')
  })

  it('setTheme applies the theme immediately', () => {
    const store = useThemeStore()

    store.setTheme('dark')

    expect(store.theme).toBe('dark')
    expect(htmlClasses()).toContain('dark')

    store.setTheme('light')

    expect(store.theme).toBe('light')
    expect(htmlClasses()).not.toContain('dark')
  })
})
