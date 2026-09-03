/**
 * 任务空间（Mission）协议模块 — 设计 docs/mission-space-p1-design.md §7 / T7。
 *
 * 职责：
 * - 翻译下行 mission.task.dispatch：存任务上下文 → 自动回 ack → 把任务包编译成
 *   提示词经本地 Gateway 发起会话（X-OpenClaw-Session-Key 用包内 session_key 做
 *   实例内会话隔离，设计 D7）→ 催办循环驱动至 Agent 显式 mission_complete；
 *   done 只能由显式声明产生，轮数用尽无声明转 L2 人工确认（防"假完成"）
 * - 提供 4 个 Agent 工具（report / submit_artifact / block / complete），经
 *   collaboration 上行通道发 mission.task.* 消息（后端 ingest_service 消费）
 * - 单实例同时只处理一个任务（后端 D9 实例串行保证），故模块级单槽任务上下文即可
 */

import type { AnyAgentTool } from "openclaw/plugin-sdk";

const GATEWAY_PORT_DEFAULT = 3000;

/** 催办循环上限：Agent 未显式 mission_complete 时最多追加驱动的轮数。 */
const MAX_DRIVE_ROUNDS = 5;

/** 下行任务包（设计 §7.1 mission.task.dispatch 的 data 部分）。 */
export interface MissionTaskPackage {
  session_key: string;
  mission_title: string;
  brief: { goal: string; constraints: string[]; acceptance_criteria: string[] };
  subtask: { title: string; description: string; acceptance_criteria: string };
  upstream_conclusions?: Array<{ seq: number; title: string; summary: string }>;
  upstream_artifacts: Array<{ name: string; kind: string; storage_url: string }>;
  /** 编排者打回反馈（历次复核未通过原因 + 最近人工答复），重派时注入 */
  review_feedback?: string[];
  escalation_rules: { l2_rules: string[]; l1_hint: string };
  report_guidance: string;
}

interface ActiveTask {
  taskId: string;
  pkg: MissionTaskPackage;
  /** Agent 已主动 mission_complete 时置位，流结束不再重复回 done。 */
  completed: boolean;
  /** Agent 已 L2 阻塞时置位，停止催办循环（等待人类处理）。 */
  blocked: boolean;
}

let activeTask: ActiveTask | null = null;

/** 上行发送器（collaboration.message 信封），测试可注入。 */
export type MissionUplink = (payload: Record<string, unknown>) => void;

let _uplinkOverride: MissionUplink | null = null;

/** 仅供测试注入上行通道。 */
export function _setUplinkForTest(fn: MissionUplink | null): void {
  _uplinkOverride = fn;
}

function uplink(payload: Record<string, unknown>): void {
  if (_uplinkOverride) {
    _uplinkOverride(payload);
    return;
  }
  // 延迟 require 避免与 tunnel-client 形成模块初始化期循环
  const { getTunnelClient } = require("./tunnel-client.js") as {
    getTunnelClient: () => { sendCollaboration: (p: Record<string, unknown>) => void };
  };
  getTunnelClient().sendCollaboration(payload);
}

export function setActiveTask(taskId: string, pkg: MissionTaskPackage): void {
  activeTask = { taskId, pkg, completed: false, blocked: false };
}

export function getActiveTask(): ActiveTask | null {
  return activeTask;
}

/** 供测试重置。 */
export function _resetActiveTask(): void {
  activeTask = null;
}

/** 任务包 → 提示词（信封翻译的核心纯函数，测试覆盖点）。 */
export function buildTaskPrompt(pkg: MissionTaskPackage): string {
  const lines: string[] = [
    `[任务] ${pkg.mission_title} — ${pkg.subtask.title}`,
    `目标：${pkg.brief.goal}`,
  ];
  if (pkg.subtask.description) lines.push(`说明：${pkg.subtask.description}`);
  if (pkg.subtask.acceptance_criteria) lines.push(`验收标准：${pkg.subtask.acceptance_criteria}`);
  if (pkg.brief.constraints?.length) {
    lines.push(`约束：${pkg.brief.constraints.join("；")}`);
  }
  if (pkg.brief.acceptance_criteria?.length) {
    lines.push(`整体验收：${pkg.brief.acceptance_criteria.join("；")}`);
  }
  if (pkg.upstream_conclusions?.length) {
    lines.push("上游结论（前序节点已完成的工作总结，直接参考不需重做）：");
    for (const c of pkg.upstream_conclusions) {
      lines.push(`  #${c.seq} ${c.title}：${c.summary}`);
    }
  }
  if (pkg.upstream_artifacts?.length) {
    const arts = pkg.upstream_artifacts
      .map((a) => `${a.name}(${a.kind}) ${a.storage_url}`)
      .join("；");
    lines.push(`上游产物（带签名的临时下载链接，可直接 GET）：${arts}`);
  }
  if (pkg.review_feedback?.length) {
    lines.push("编排者打回反馈（上一轮产出未通过验收，必须针对性改进后重新提交）：");
    for (const f of pkg.review_feedback) {
      lines.push(`  - ${f}`);
    }
  }
  if (pkg.escalation_rules?.l2_rules?.length) {
    lines.push(
      `以下情形必须先调用 mission_block 阻塞等待人类确认，不得自行动作：${pkg.escalation_rules.l2_rules.join("；")}`,
    );
  }
  if (pkg.escalation_rules?.l1_hint) {
    lines.push(`如有歧义但不致命：${pkg.escalation_rules.l1_hint}（用 mission_block level=L1 提问）`);
  }
  lines.push(
    pkg.report_guidance ||
      "执行中请定期调用 mission_report 播报进展；完成后调用 mission_complete 提交总结；产物用 mission_submit_artifact 上交。",
  );
  lines.push(
    "重要：读取文件时注意控制上下文——大文件只读需要的部分（用 offset/limit 参数分段），"
    + "不要一次性读完整个大文件；每次工具调用后先总结要点再决定下一步，避免上下文膨胀。",
  );
  lines.push(
    "完成判定：只有调用 mission_complete 才视为任务完成，仅输出文字总结不构成完成；"
    + "部分完成时不要停，继续执行剩余部分，全部完成后调用 mission_complete。",
  );
  lines.push(
    "工具用法：mission_report(message)；mission_submit_artifact(name, kind, content_b64)；" +
      "mission_block(reason, question_level, message, assumption)；mission_complete(summary)。",
  );
  return lines.join("\n");
}

/** 解析下行信封（设计 §7：{task_id, protocol_version, data}），非法返回 null（测试覆盖点）。 */
export function parseDispatchEnvelope(
  payload: unknown,
): { taskId: string; pkg: MissionTaskPackage } | null {
  if (!payload || typeof payload !== "object") return null;
  const p = payload as Record<string, unknown>;
  const taskId = typeof p.task_id === "string" ? p.task_id : "";
  const data = p.data;
  if (!taskId || !data || typeof data !== "object") return null;
  const d = data as Record<string, unknown>;
  if (typeof d.session_key !== "string" || !d.session_key) return null;
  if (typeof d.mission_title !== "string" || !d.mission_title) return null;
  return { taskId, pkg: d as unknown as MissionTaskPackage };
}

/** ack 上行消息构造（测试覆盖点）。 */
export function buildAckPayload(taskId: string): Record<string, unknown> {
  return { type: "mission.task.ack", task_id: taskId, data: {} };
}

function missionSend(type: string, data: Record<string, unknown>): void {
  if (!activeTask) {
    console.warn("[mission] 无进行中的任务，忽略 %s", type);
    return;
  }
  uplink({ type, task_id: activeTask.taskId, data });
}

/**
 * 处理 mission.task.dispatch（由 tunnel-client 注入，sender 供测试替换）。
 * 流程：存上下文 → 回 ack → 编提示词 → 本地 Gateway 会话 → 完成回 done。
 */
export async function handleMissionDispatch(
  payload: unknown,
  opts?: { token?: string; model?: string; send?: MissionUplink },
): Promise<void> {
  const send = opts?.send ?? uplink;
  const parsed = parseDispatchEnvelope(payload);
  if (!parsed) {
    console.warn("[mission] dispatch 信封非法，丢弃");
    return;
  }
  const { taskId, pkg } = parsed;
  setActiveTask(taskId, pkg);
  send(buildAckPayload(taskId)); // 收到任务包即自动 ack（设计 §7.2，无需 Agent 动作）
  console.log("[mission] 任务已接收: %s（%s）", pkg.subtask.title, taskId);

  const gatewayPort =
    parseInt(process.env.OPENCLAW_GATEWAY_PORT ?? "", 10) || GATEWAY_PORT_DEFAULT;
  const url = `http://localhost:${gatewayPort}/v1/chat/completions`;

  // 执行中定期播报进度（30s 一次），让前端时间线有"正在干什么"的实时感
  const progressTimer = setInterval(() => {
    if (activeTask?.taskId === taskId) {
      send({ type: "mission.task.progress", task_id: taskId, data: {
        message: `正在执行「${pkg.subtask.title}」…（已等待 ${Math.round((Date.now() - startTime) / 1000)}s）`,
        phase: "executing",
      } });
    }
  }, 30_000);
  const startTime = Date.now();

  try {
    // 催办循环：会话结束≠任务完成（Agent 可能中途停下/上下文截断）。
    // done 只能由 Agent 显式调用 mission_complete 产生；未声明则同会话追加催办
    // 继续驱动，轮数用尽仍无声明 → L2 阻塞交人工（防"假完成"污染下游节点）。
    let nudge = buildTaskPrompt(pkg);
    let lastOutput = "";
    for (let round = 1; round <= MAX_DRIVE_ROUNDS; round++) {
      const resp = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${opts?.token ?? ""}`,
          "X-OpenClaw-Session-Key": pkg.session_key,
        },
        body: JSON.stringify({
          model: opts?.model ?? "openclaw/main",
          messages: [{ role: "user", content: nudge }],
          stream: false,
        }),
      });
      if (!resp.ok) {
        console.error("[mission] Gateway 响应异常: %d", resp.status);
        missionSend("mission.task.blocked", {
          reason: `本地模型调用失败（HTTP ${resp.status}）`,
          question: { level: "L2", message: "本地模型调用失败，请检查实例模型配置" },
        });
        return;
      }
      const body = (await resp.json()) as {
        choices?: Array<{ message?: { content?: string } }>;
      };
      lastOutput = body.choices?.[0]?.message?.content ?? "";
      const cur = activeTask?.taskId === taskId ? activeTask : null;
      if (!cur) return; // 任务被顶替/清理
      if (cur.completed || cur.blocked) return; // 已显式完成 / L2 等人，不再催办
      nudge =
        "任务尚未完成。请继续执行剩余部分；全部完成后必须调用 mission_complete 提交总结，" +
        "如遇无法自行解决的问题调用 mission_block（L2）。注意：仅回复文字不构成完成。";
    }
    console.warn("[mission] Agent %d 轮未显式完成，转 L2 人工确认: %s", MAX_DRIVE_ROUNDS, taskId);
    missionSend("mission.task.blocked", {
      reason: `Agent 连续 ${MAX_DRIVE_ROUNDS} 轮对话未调用 mission_complete`,
      question: {
        level: "L2",
        message: `Agent 未明确报告任务完成，请人工确认。Agent 最后输出：\n${lastOutput.slice(0, 2000)}`,
      },
    });
  } catch (err) {
    console.error("[mission] 任务执行失败:", err);
    missionSend("mission.task.blocked", {
      reason: err instanceof Error ? err.message : String(err),
      question: { level: "L2", message: "任务执行过程发生异常" },
    });
  } finally {
    clearInterval(progressTimer);
  }
}

// ── Agent 工具（设计 §7.3，注册进 createNoDeskClawTools）────────────────────

function jsonResult(obj: Record<string, unknown>): string {
  return JSON.stringify(obj);
}

export function createMissionReportTool(): AnyAgentTool {
  return {
    name: "mission_report",
    description:
      "Report your progress on the current mission task to humans (visible in the " +
      "mission timeline). Call this periodically while working, and when you are about " +
      "to hand over to another agent.",
    parameters: {
      type: "object",
      properties: {
        message: { type: "string", description: "进展播报（人类可读）" },
      },
      required: ["message"],
    },
    execute: async (_id, args) => {
      const msg = (args as Record<string, unknown>).message;
      if (!activeTask) return jsonResult({ error: "no active mission task" });
      missionSend("mission.task.report", { message: String(msg ?? "") });
      return jsonResult({ ok: true });
    },
  } as unknown as AnyAgentTool;
}

export function createMissionSubmitArtifactTool(): AnyAgentTool {
  return {
    name: "mission_submit_artifact",
    description:
      "Submit a deliverable (file/report/code) for the current mission task. Content " +
      "must be base64-encoded; keep it under 20MB. For large files, write them to your " +
      "workspace via nodeskclaw_write_file instead and submit a short summary doc.",
    parameters: {
      type: "object",
      properties: {
        name: { type: "string", description: "产物名（含扩展名）" },
        kind: {
          type: "string",
          description: "file | report | code | config | other",
        },
        content_b64: { type: "string", description: "base64 编码的内容" },
      },
      required: ["name", "content_b64"],
    },
    execute: async (_id, args) => {
      const p = args as Record<string, unknown>;
      if (!activeTask) return jsonResult({ error: "no active mission task" });
      const content = String(p.content_b64 ?? "");
      if (!p.name) return jsonResult({ error: "name is required" });
      if (content.length > 20 * 1024 * 1024) {
        return jsonResult({ error: "artifact exceeds 20MB" });
      }
      missionSend("mission.task.artifact", {
        name: String(p.name),
        kind: String(p.kind ?? "other"),
        content_base64: content,
        size: Math.floor((content.length * 3) / 4),
      });
      return jsonResult({ ok: true });
    },
  } as unknown as AnyAgentTool;
}

export function createMissionBlockTool(): AnyAgentTool {
  return {
    name: "mission_block",
    description:
      "Raise a question or block on the current mission task. level=L1 asks a " +
      "non-blocking question (you may continue with your stated assumption); " +
      "level=L2 blocks the task until a human answers (required for dangerous " +
      "operations, DAG changes, or budget overruns).",
    parameters: {
      type: "object",
      properties: {
        reason: { type: "string", description: "阻塞/提问原因" },
        question_level: { type: "string", description: "L1 | L2" },
        message: { type: "string", description: "要问人类的具体问题" },
        assumption: { type: "string", description: "L1 附带假设（可选）" },
      },
      required: ["reason", "question_level", "message"],
    },
    execute: async (_id, args) => {
      const p = args as Record<string, unknown>;
      if (!activeTask) return jsonResult({ error: "no active mission task" });
      const level = String(p.question_level ?? "L1").toUpperCase() === "L2" ? "L2" : "L1";
      if (level === "L2") activeTask.blocked = true; // L2 等人类处理，停止催办循环
      missionSend("mission.task.blocked", {
        reason: String(p.reason ?? ""),
        question: {
          level,
          message: String(p.message ?? ""),
          assumption: p.assumption ? String(p.assumption) : undefined,
        },
      });
      return jsonResult({ ok: true, level });
    },
  } as unknown as AnyAgentTool;
}

export function createMissionCompleteTool(): AnyAgentTool {
  return {
    name: "mission_complete",
    description:
      "Mark the current mission task as complete with a summary. Call this exactly " +
      "once when your deliverable is done and artifacts are submitted.",
    parameters: {
      type: "object",
      properties: {
        summary: { type: "string", description: "完成总结（做了什么、产出在哪）" },
      },
      required: ["summary"],
    },
    execute: async (_id, args) => {
      const p = args as Record<string, unknown>;
      if (!activeTask) return jsonResult({ error: "no active mission task" });
      activeTask.completed = true;
      missionSend("mission.task.done", { summary: String(p.summary ?? "") });
      return jsonResult({ ok: true });
    },
  } as unknown as AnyAgentTool;
}

export const MISSION_TOOL_FACTORIES = [
  createMissionReportTool,
  createMissionSubmitArtifactTool,
  createMissionBlockTool,
  createMissionCompleteTool,
];
