/**
 * 任务空间协议单测（设计 §11 T7 验收：信封解析、4 工具注册、ack 自动回）。
 * 运行：node --test src/mission.test.ts（Node >= 22.6，原生 TS 类型剥离；
 * 仅 import mission.ts——它无运行期外部依赖，上行通道经 _setUplinkForTest 注入）。
 */
import test from "node:test";
import assert from "node:assert/strict";
import {
  buildAckPayload,
  buildTaskPrompt,
  handleMissionDispatch,
  MISSION_TOOL_FACTORIES,
  _resetActiveTask,
  _setUplinkForTest,
  setActiveTask,
  parseDispatchEnvelope,
} from "./mission.ts";

const PKG = {
  session_key: "org1:m1:n1",
  mission_title: "竞品分析",
  brief: { goal: "输出三竞品对比", constraints: ["只用公开资料"], acceptance_criteria: ["含对比表"] },
  subtask: { title: "搜集资料", description: "搜集三家的定价", acceptance_criteria: "资料清单" },
  upstream_artifacts: [{ name: "brief.md", kind: "report", storage_url: "http://x/brief.md" }],
  escalation_rules: { l2_rules: ["付费购买数据"], l1_hint: "口径歧义可带假设继续" },
  report_guidance: "定期 mission_report",
};

test("parseDispatchEnvelope：合法信封解析出 task_id 与任务包", () => {
  const parsed = parseDispatchEnvelope({ task_id: "t1", protocol_version: 1, data: PKG });
  assert.ok(parsed);
  assert.equal(parsed!.taskId, "t1");
  assert.equal(parsed!.pkg.subtask.title, "搜集资料");
});

test("parseDispatchEnvelope：缺 task_id / 缺 data / 缺 session_key 均拒绝", () => {
  assert.equal(parseDispatchEnvelope({ data: PKG }), null);
  assert.equal(parseDispatchEnvelope({ task_id: "t1" }), null);
  assert.equal(
    parseDispatchEnvelope({ task_id: "t1", data: { ...PKG, session_key: "" } }),
    null,
  );
  assert.equal(parseDispatchEnvelope(null), null);
});

test("buildTaskPrompt：含任务标题/目标/验收/上游产物/L2 边界/工具指引", () => {
  const prompt = buildTaskPrompt(PKG as never);
  for (const expect of [
    "竞品分析 — 搜集资料",
    "输出三竞品对比",
    "资料清单",
    "brief.md",
    "付费购买数据",
    "mission_complete",
  ]) {
    assert.ok(prompt.includes(expect), `prompt 缺少: ${expect}`);
  }
});

test("handleMissionDispatch：收到即自动回 ack（首个上行消息）", async () => {
  _resetActiveTask();
  const sent: Array<Record<string, unknown>> = [];
  _setUplinkForTest((p) => sent.push(p));
  // 拦截本地 Gateway 调用（避免真实网络）
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => {
    throw new Error("gateway unreachable in test");
  }) as typeof fetch;
  try {
    await handleMissionDispatch({ task_id: "t-ack", protocol_version: 1, data: PKG });
  } finally {
    globalThis.fetch = originalFetch;
    _setUplinkForTest(null);
  }
  assert.equal(sent[0]?.type, "mission.task.ack");
  assert.equal(sent[0]?.task_id, "t-ack");
  assert.deepEqual(sent[0]?.data, {});
  // Gateway 失败 → L2 阻塞上报（不静默丢任务）
  assert.equal(sent[1]?.type, "mission.task.blocked");
});

test("handleMissionDispatch：Gateway 正常时回 done 携带总结", async () => {
  _resetActiveTask();
  const sent: Array<Record<string, unknown>> = [];
  _setUplinkForTest((p) => sent.push(p));
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => ({
    ok: true,
    json: async () => ({ choices: [{ message: { content: "已完成搜集" } }] }),
  })) as typeof fetch;
  try {
    await handleMissionDispatch({ task_id: "t-done", protocol_version: 1, data: PKG });
  } finally {
    globalThis.fetch = originalFetch;
    _setUplinkForTest(null);
  }
  const types = sent.map((m) => m.type);
  assert.deepEqual(types, ["mission.task.ack", "mission.task.done"]);
  const done = sent[1] as { data?: { summary?: string } };
  assert.equal(done.data?.summary, "已完成搜集");
});

test("4 个 mission 工具已注册且命名符合协议", async () => {
  assert.equal(MISSION_TOOL_FACTORIES.length, 4);
  const tools = MISSION_TOOL_FACTORIES.map((f) => f()) as Array<{ name: string }>;
  assert.deepEqual(
    tools.map((t) => t.name).sort(),
    ["mission_block", "mission_complete", "mission_report", "mission_submit_artifact"],
  );

  // 工具执行：上行经注入通道发出、携带当前 task_id
  const sent: Array<Record<string, unknown>> = [];
  _setUplinkForTest((p) => sent.push(p));
  try {
    setActiveTask("t-tools", PKG as never);
    const report = tools.find((t) => t.name === "mission_report") as unknown as {
      execute: (_id: string, args: unknown) => Promise<string>;
    };
    const out = JSON.parse(await report.execute("x", { message: "进行中" })) as { ok: boolean };
    assert.equal(out.ok, true);
    assert.equal(sent[0]?.type, "mission.task.report");
    assert.equal(sent[0]?.task_id, "t-tools");
    assert.deepEqual(sent[0]?.data, { message: "进行中" });
  } finally {
    _setUplinkForTest(null);
    _resetActiveTask();
  }
});
