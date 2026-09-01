import api from './api'

/** 任务空间（Mission P1）API 封装，契约见 docs/mission-space-p1-design.md §9 */

export interface MissionSummary {
  id: string
  title: string
  status: string
  mission_type: 'standard' | 'lightweight'
  created_at: string | null
  tokens: number
}

export interface MissionNodeItem {
  id: string
  seq: number
  title: string
  description: string | null
  acceptance_criteria: string | null
  capability_tags: string[]
  depends_on: number[]
  status: string
  assigned_instance_id: string | null
  match_reason: {
    candidates?: Array<{ instance_id: string; instance_name: string; score: number }>
    chosen?: string | null
    capability_gap?: string[]
  } | null
  attempt_count: number
  session_key: string
}

export interface CoverageItem {
  node_id: string
  seq: number
  title: string
  capability_tags: string[]
  suggested_instance_id: string | null
  suggested_instance_name: string | null
  capability_gap: string[]
}

export interface MissionDetail {
  id: string
  workspace_id: string
  title: string
  requirement_text: string
  brief: { goal?: string; constraints?: string[]; acceptance_criteria?: string[]; key_decisions?: string[] }
  mission_type: string
  status: string
  created_by: string
  tokens: { cost: number; prompt: number; completion: number }
  nodes: MissionNodeItem[]
  coverage?: CoverageItem[]
}

export interface MissionEventItem {
  id: string
  seq: number
  event_type: string
  node_id: string | null
  actor_type: string
  actor_name: string | null
  visibility: string
  content: string | null
  payload: Record<string, unknown> | null
  created_at: string | null
}

export interface MissionArtifactItem {
  id: string
  node_id: string | null
  name: string
  kind: string
  size_bytes: number | null
  version: number
  retention: 'quarantine' | 'promoted'
  expires_at: string | null
  created_at: string | null
}

export const missionApi = {
  async list(workspaceId: string, status?: string): Promise<MissionSummary[]> {
    const res = await api.get(`/workspaces/${workspaceId}/missions`, { params: status ? { status } : {} })
    return res.data?.data ?? []
  },
  async create(workspaceId: string, requirementText: string): Promise<{ id: string }> {
    const res = await api.post(`/workspaces/${workspaceId}/missions`, {
      requirement_text: requirementText,
    })
    return res.data?.data
  },
  async detail(missionId: string): Promise<MissionDetail> {
    const res = await api.get(`/missions/${missionId}`)
    return res.data?.data
  },
  async confirm(
    missionId: string,
    body?: {
      node_edits?: Array<{ node_id: string; assigned_instance_id?: string; title?: string }>
    },
  ): Promise<void> {
    await api.post(`/missions/${missionId}/confirm`, body ?? {})
  },
  async replan(missionId: string): Promise<void> {
    await api.post(`/missions/${missionId}/replan`)
  },
  async cancel(missionId: string): Promise<void> {
    await api.post(`/missions/${missionId}/cancel`)
  },
  async retryNode(missionId: string, nodeId: string): Promise<void> {
    await api.post(`/missions/${missionId}/nodes/${nodeId}/retry`)
  },
  async reassignNode(missionId: string, nodeId: string, instanceId: string): Promise<void> {
    await api.post(`/missions/${missionId}/nodes/${nodeId}/reassign`, { instance_id: instanceId })
  },
  async answer(missionId: string, eventId: string, answer: string): Promise<void> {
    await api.post(`/missions/${missionId}/questions/${eventId}/answer`, { answer })
  },
  async events(missionId: string, afterSeq = 0): Promise<MissionEventItem[]> {
    const res = await api.get(`/missions/${missionId}/events`, { params: { after_seq: afterSeq } })
    return res.data?.data ?? []
  },
  async artifacts(missionId: string): Promise<MissionArtifactItem[]> {
    const res = await api.get(`/missions/${missionId}/artifacts`)
    return res.data?.data ?? []
  },
  async promote(missionId: string, artifactId: string): Promise<void> {
    await api.post(`/missions/${missionId}/artifacts/${artifactId}/promote`)
  },
  async accept(missionId: string): Promise<void> {
    await api.post(`/missions/${missionId}/accept`)
  },
  async reject(missionId: string, reason: string, rejectedNodes: string[]): Promise<void> {
    await api.post(`/missions/${missionId}/reject`, {
      reason,
      rejected_nodes: rejectedNodes,
    })
  },
}

/** 任务空间 SSE 地址（EventSource 无法带 Authorization 头，走 query token） */
export function missionEventsStreamUrl(missionId: string, afterSeq = 0): string {
  const token = localStorage.getItem('portal_token') || ''
  return `/api/v1/missions/${missionId}/events/stream?token=${token}&after_seq=${afterSeq}`
}
