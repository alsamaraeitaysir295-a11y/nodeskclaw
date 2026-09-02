<script setup lang="ts">
/**
 * 任务空间 DAG 可视化（P2）：自绘 SVG 分层拓扑图。
 * 按 depends_on 做拓扑分层（Kahn），节点=圆角矩形（状态着色），边=贝塞尔曲线。
 * 项目惯例：不引入 vue-flow/dagre，自绘 SVG 同 Workspace2D。
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

export interface DagNode {
  id: string
  seq: number
  title: string
  status: string
  capability_tags: string[]
  depends_on: number[]
}

const props = defineProps<{ nodes: DagNode[]; selectedNodeId?: string | null }>()
const emit = defineEmits<{ (e: 'node-click', nodeId: string): void }>()
const { t } = useI18n()

const NODE_W = 180
const NODE_H = 56
const GAP_X = 80
const GAP_Y = 24
const PADDING = 30

const STATUS_COLORS: Record<string, { border: string; fill: string; text: string }> = {
  pending: { border: '#94a3b8', fill: '#f1f5f9', text: '#64748b' },
  matched: { border: '#60a5fa', fill: '#eff6ff', text: '#2563eb' },
  dispatched: { border: '#fbbf24', fill: '#fffbeb', text: '#b45309' },
  acked: { border: '#fbbf24', fill: '#fffbeb', text: '#b45309' },
  running: { border: '#34d399', fill: '#ecfdf5', text: '#059669' },
  done: { border: '#10b981', fill: '#d1fae5', text: '#047857' },
  failed: { border: '#f87171', fill: '#fef2f2', text: '#dc2626' },
  blocked_question: { border: '#fb923c', fill: '#fff7ed', text: '#c2410c' },
  blocked_dependency: { border: '#fb923c', fill: '#fff7ed', text: '#c2410c' },
  skipped: { border: '#cbd5e1', fill: '#f8fafc', text: '#94a3b8' },
}

/** Kahn 拓扑分层 */
const layout = computed(() => {
  const nodes = props.nodes
  if (nodes.length === 0) return { positions: [], edges: [], width: 0, height: 0 }

  const bySeq = new Map(nodes.map(n => [n.seq, n]))
  // 计算每个节点的层级（最长路径层号）
  const levelOf = new Map<number, number>()
  const computeLevel = (seq: number, visited: Set<number>): number => {
    if (levelOf.has(seq)) return levelOf.get(seq)!
    if (visited.has(seq)) return 0
    visited.add(seq)
    const node = bySeq.get(seq)
    if (!node || !node.depends_on?.length) {
      levelOf.set(seq, 0)
      return 0
    }
    const max = Math.max(...node.depends_on.map(d => computeLevel(d, visited)))
    const level = max + 1
    levelOf.set(seq, level)
    return level
  }
  for (const n of nodes) computeLevel(n.seq, new Set())

  // 按层分组
  const layers = new Map<number, DagNode[]>()
  for (const n of nodes) {
    const lvl = levelOf.get(n.seq) ?? 0
    if (!layers.has(lvl)) layers.set(lvl, [])
    layers.get(lvl)!.push(n)
  }

  // 计算位置
  const positions: Array<{ node: DagNode; x: number; y: number }> = []
  const maxLevel = Math.max(...layers.keys())
  for (let lvl = 0; lvl <= maxLevel; lvl++) {
    const layerNodes = layers.get(lvl) || []
    layerNodes.sort((a, b) => a.seq - b.seq)
    for (let i = 0; i < layerNodes.length; i++) {
      positions.push({
        node: layerNodes[i],
        x: PADDING + lvl * (NODE_W + GAP_X),
        y: PADDING + i * (NODE_H + GAP_Y),
      })
    }
  }

  // 计算边
  const posBySeq = new Map(positions.map(p => [p.node.seq, p]))
  const edges: Array<{ from: { x: number; y: number }; to: { x: number; y: number } }> = []
  for (const p of positions) {
    for (const dep of p.node.depends_on || []) {
      const from = posBySeq.get(dep)
      if (from) {
        edges.push({
          from: { x: from.x + NODE_W, y: from.y + NODE_H / 2 },
          to: { x: p.x, y: p.y + NODE_H / 2 },
        })
      }
    }
  }

  const width = PADDING * 2 + (maxLevel + 1) * NODE_W + maxLevel * GAP_X
  const maxY = Math.max(...positions.map(p => p.y)) + NODE_H + PADDING
  return { positions, edges, width, height: maxY }
})

function nodeColor(status: string) {
  return STATUS_COLORS[status] || STATUS_COLORS.pending
}

function edgePath(edge: { from: { x: number; y: number }; to: { x: number; y: number } }): string {
  const midX = (edge.from.x + edge.to.x) / 2
  return `M ${edge.from.x} ${edge.from.y} C ${midX} ${edge.from.y}, ${midX} ${edge.to.y}, ${edge.to.x} ${edge.to.y}`
}

function truncate(text: string, max: number): string {
  return text.length > max ? text.slice(0, max) + '…' : text
}

const hoveredId = ref<string | null>(null)
</script>

<template>
  <div class="relative overflow-auto rounded-xl border border-border bg-card" style="max-height: 400px">
    <svg
      v-if="layout.positions.length > 0"
      :width="layout.width"
      :height="layout.height"
      class="dag-svg"
    >
      <!-- 依赖边 -->
      <g v-for="(edge, i) in layout.edges" :key="'e' + i">
        <path
          :d="edgePath(edge)"
          fill="none"
          stroke="currentColor"
          class="text-border"
          stroke-width="1.5"
          marker-end="url(#arrow)"
        />
      </g>

      <!-- 箭头 marker -->
      <defs>
        <marker
          id="arrow"
          viewBox="0 0 10 10"
          refX="10"
          refY="5"
          markerWidth="6"
          markerHeight="6"
          orient="auto-start-reverse"
        >
          <path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" class="text-border" />
        </marker>
      </defs>

      <!-- 节点 -->
      <g
        v-for="pos in layout.positions"
        :key="pos.node.id"
        :transform="`translate(${pos.x}, ${pos.y})`"
        class="cursor-pointer"
        @click="emit('node-click', pos.node.id)"
        @mouseenter="hoveredId = pos.node.id"
        @mouseleave="hoveredId = null"
      >
        <rect
          :width="NODE_W"
          :height="NODE_H"
          rx="8"
          :fill="nodeColor(pos.node.status).fill"
          :stroke="hoveredId === pos.node.id || selectedNodeId === pos.node.id ? nodeColor(pos.node.status).border : nodeColor(pos.node.status).border + '80'"
          :stroke-width="hoveredId === pos.node.id || selectedNodeId === pos.node.id ? 2 : 1"
        />
        <text
          x="12"
          y="20"
          :fill="nodeColor(pos.node.status).text"
          font-size="11"
          font-weight="600"
        >
          #{{ pos.node.seq }} {{ truncate(pos.node.title, 14) }}
        </text>
        <text
          x="12"
          y="38"
          :fill="nodeColor(pos.node.status).text"
          font-size="9"
          opacity="0.8"
        >
          {{ truncate(pos.node.capability_tags.join(', '), 20) }}
        </text>
        <text
          x="12"
          y="50"
          :fill="nodeColor(pos.node.status).text"
          font-size="8"
          opacity="0.6"
        >
          {{ t(`missions.nodeStatus.${pos.node.status}`, pos.node.status) }}
        </text>
      </g>
    </svg>

    <div v-else class="flex items-center justify-center h-32 text-sm text-muted-foreground">
      {{ t('missions.noNodes') }}
    </div>
  </div>
</template>

<style scoped>
.dag-svg {
  display: block;
}
</style>
