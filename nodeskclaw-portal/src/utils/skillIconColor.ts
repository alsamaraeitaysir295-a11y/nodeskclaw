/**
 * 技能图标底色：按稳定 seed（slug/id）散列到一组主色，
 * 复刻技能市场设计稿的多彩 squircle 图标。
 * 市场页（GeneMarket.vue）与选择弹窗（GeneMarketDialog.vue）共用，勿在组件内复制。
 */

const ICON_PALETTE = [
  'bg-violet-500', 'bg-orange-500', 'bg-blue-500', 'bg-emerald-500',
  'bg-cyan-500', 'bg-pink-500', 'bg-indigo-500', 'bg-teal-500',
]

export function iconColorClass(seed: string): string {
  let h = 0
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0
  return ICON_PALETTE[h % ICON_PALETTE.length]
}
