<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ArrowLeft, Bot, Send, Loader2, AlertCircle, Plus, MessageSquare, Trash2, Paperclip, X, FileText, Folder, ChevronRight, Download } from 'lucide-vue-next'
import api from '@/services/api'
import { useWorkspaceStore } from '@/stores/workspace'
import { useAuthStore } from '@/stores/auth'
import { useToast } from '@/composables/useToast'
import { getStatusDisplay } from '@/utils/instanceStatus'
import { getRuntimeCaps } from '@/utils/runtimeCapabilities'
import { resolveApiErrorMessage } from '@/i18n/error'
import type { WorkspaceListItem, Conversation, FileAttachment } from '@/stores/workspace'
import FileAttachmentList from '@/components/chat/FileAttachmentList.vue'
import BaseTooltip from '@/components/shared/BaseTooltip.vue'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const store = useWorkspaceStore()
const authStore = useAuthStore()
const toast = useToast()

const instanceId = computed(() => route.params.id as string)

// ── 实例信息 ─────────────────────────────────
interface InstanceDetail {
  id: string
  name: string
  display_status?: string
  compute_provider?: string
  runtime?: string
}

const instance = ref<InstanceDetail | null>(null)
const instanceLoading = ref(true)
const instanceError = ref('')

// ── 工作空间 ─────────────────────────────────
const workspace = ref<WorkspaceListItem | null>(null)
const workspaceLoading = ref(false)

// ── 会话列表 ─────────────────────────────────
const sessions = ref<Conversation[]>([])
const activeSessionId = ref<string | null>(null)
const sessionsLoading = ref(false)
const creatingSession = ref(false)

// ── 消息列表 ─────────────────────────────────
interface ChatMsg {
  id: string
  sender_type: 'user' | 'agent' | 'system'
  sender_id: string
  sender_name: string
  content: string
  created_at: string
  streaming?: boolean
  attachments?: FileAttachment[]
}

const messages = ref<ChatMsg[]>([])
const messagesLoading = ref(false)
const chatEndRef = ref<HTMLElement | null>(null)

// ── 文件上传 ─────────────────────────────────
const fileInputRef = ref<HTMLInputElement | null>(null)
const pendingFiles = ref<File[]>([])
const fileUploading = ref(false)
const MAX_FILE_SIZE = 20 * 1024 * 1024

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`
}

function triggerFileInput() {
  if (!store.fileUploadEnabled) return
  fileInputRef.value?.click()
}
function handleFileSelect(e: Event) {
  const input = e.target as HTMLInputElement
  if (!input.files) return
  addFiles(Array.from(input.files))
  input.value = ''
}
function addFiles(files: File[]) {
  for (const f of files) {
    if (f.size > MAX_FILE_SIZE) { toast.error(t('chat.fileTooLarge', { size: 20 })); continue }
    pendingFiles.value.push(f)
  }
}
function removePendingFile(idx: number) { pendingFiles.value.splice(idx, 1) }
function handleDragOver(e: DragEvent) { if (!store.fileUploadEnabled) return; e.preventDefault() }
function handleDrop(e: DragEvent) {
  if (!store.fileUploadEnabled) return
  e.preventDefault()
  if (e.dataTransfer?.files) addFiles(Array.from(e.dataTransfer.files))
}

// ── AI 员工生成的文件面板（复用 InstanceFiles.vue 的浏览能力，精简版：仅列表+下载） ───
interface InstanceFileItem {
  name: string
  is_dir: boolean
  size: number
  mime_type: string | null
  modified_at: string | null
}
interface InstanceFileListing {
  instance_id: string
  instance_name: string
  path: string
  breadcrumb: string[]
  items: InstanceFileItem[]
}

const filesPanelOpen = ref(false)
const filesCurrentPath = ref('')
const filesLoading = ref(false)
const filesListing = ref<InstanceFileListing | null>(null)
const filesError = ref('')
const filesTruncatedCount = ref(0)
const MAX_RECENT_FILES = 30

const filesDataRoot = computed(() => getRuntimeCaps(instance.value?.runtime || 'openclaw').dataRoot)

function filesBreadcrumbLabel(part: string): string {
  return part === filesDataRoot.value ? t('instanceFiles.workspaceRoot') : part
}

async function fetchInstanceFiles() {
  filesLoading.value = true
  filesError.value = ''
  try {
    const { data } = await api.get(
      `/instances/${instanceId.value}/files/workspace`,
      { params: { path: filesCurrentPath.value || undefined } },
    )
    const listing = data.data as InstanceFileListing
    // 隐藏点号开头的目录/文件（.git、.openclaw 这类运行时内部状态，不是"生成的文件"）；
    // 隐藏 AI 员工自带的人设/配置文件（每个实例创建时都会预置，不是对话中生成的）；
    // 剩下的目录始终保留（方便导航）；文件按修改时间过滤（仅显示近 24 小时内改动过的），
    // 免得人设/配置一类几乎不变的老文件把真正"新生成的文件"淹没掉
    const BUILTIN_FILE_NAMES = new Set([
      'AGENTS.md', 'BOOTSTRAP.md', 'HEARTBEAT.md', 'IDENTITY.md',
      'SOUL.md', 'TOOLS.md', 'USER.md', 'openclaw-workspace-state.json',
    ])
    const RECENT_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
    const now = Date.now()
    const modTime = (item: InstanceFileItem) => (item.modified_at ? new Date(item.modified_at).getTime() : 0)
    const visible = listing.items
      .filter((item) => !item.name.startsWith('.'))
      .filter((item) => item.is_dir || !BUILTIN_FILE_NAMES.has(item.name))
      .filter((item) => item.is_dir || !item.modified_at || now - modTime(item) <= RECENT_WINDOW_MS)
    const dirs = visible.filter((item) => item.is_dir)
    const files = visible.filter((item) => !item.is_dir).sort((a, b) => modTime(b) - modTime(a))
    // 聊天次数上去之后同一时间窗内的文件也可能很多，再加个数量上限，
    // 面板不至于无限变长——最新的始终排在最前面，不受截断影响
    filesTruncatedCount.value = Math.max(0, files.length - MAX_RECENT_FILES)
    listing.items = [...files.slice(0, MAX_RECENT_FILES), ...dirs].sort((a, b) => modTime(b) - modTime(a))
    filesListing.value = listing
  } catch (e: unknown) {
    filesError.value = resolveApiErrorMessage(e)
  } finally {
    filesLoading.value = false
  }
}

function navigateFilesTo(path: string) {
  filesCurrentPath.value = path
  fetchInstanceFiles()
}

function handleFilesBreadcrumb(index: number) {
  if (!filesListing.value) return
  const parts = filesListing.value.breadcrumb.slice(0, index + 1)
  navigateFilesTo(parts.join('/'))
}

const CODE_EXTENSIONS = new Set([
  'ts', 'js', 'tsx', 'jsx', 'vue', 'py', 'sh', 'bash', 'css', 'scss', 'html', 'sql',
])
const TEXT_EXTENSIONS = new Set([
  'md', 'txt', 'json', 'yaml', 'yml', 'toml', 'xml', 'csv', 'log', 'env',
  'gitignore', 'dockerfile', 'conf', 'cfg', 'ini',
])
function fileExt(name: string): string {
  return name.split('.').pop()?.toLowerCase() ?? ''
}
function isPreviewable(item: InstanceFileItem): boolean {
  if (item.is_dir) return false
  const ext = fileExt(item.name)
  return CODE_EXTENSIONS.has(ext) || TEXT_EXTENSIONS.has(ext) || ext === 'pdf'
}

// 预览面板：只读查看内容，不提供编辑（编辑是 InstanceFiles.vue 那个 admin 专属页面的能力）
const previewVisible = ref(false)
const previewLoading = ref(false)
const previewContent = ref('')
const previewFileName = ref('')
const previewError = ref('')
const previewTruncated = ref(false)
const previewBinary = ref(false)
const previewPdfUrl = ref('')

function revokePreviewPdfUrl() {
  if (previewPdfUrl.value) {
    URL.revokeObjectURL(previewPdfUrl.value)
    previewPdfUrl.value = ''
  }
}

async function openFilePreview(item: InstanceFileItem) {
  previewVisible.value = true
  previewLoading.value = true
  previewContent.value = ''
  previewError.value = ''
  previewFileName.value = item.name
  previewTruncated.value = false
  previewBinary.value = false
  revokePreviewPdfUrl()
  const filePath = `${filesCurrentPath.value}/${item.name}`

  // PDF 走二进制下载再用 iframe 渲染，不走文本内容接口（后端只读文本，PDF 会被判成 binary）
  if (fileExt(item.name) === 'pdf') {
    try {
      const url = `/api/v1/instances/${instanceId.value}/files/workspace/download?path=${encodeURIComponent(filePath)}`
      const token = localStorage.getItem('portal_token') || ''
      const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } })
      if (!res.ok) throw new Error(String(res.status))
      const blob = await res.blob()
      previewPdfUrl.value = URL.createObjectURL(blob)
    } catch {
      previewError.value = t('instanceFiles.downloadFailed')
    } finally {
      previewLoading.value = false
    }
    return
  }

  try {
    const { data } = await api.get(
      `/instances/${instanceId.value}/files/workspace/content`,
      { params: { path: filePath } },
    )
    const result = data.data
    if (result.truncated) {
      previewTruncated.value = true
    } else if (result.binary) {
      previewBinary.value = true
    } else {
      previewContent.value = result.content ?? ''
    }
  } catch (e: unknown) {
    previewError.value = resolveApiErrorMessage(e)
  } finally {
    previewLoading.value = false
  }
}

function closeFilePreview() {
  previewVisible.value = false
  revokePreviewPdfUrl()
}

function handleFileItemClick(item: InstanceFileItem) {
  if (item.is_dir) navigateFilesTo(`${filesCurrentPath.value}/${item.name}`)
  else if (isPreviewable(item)) openFilePreview(item)
}

async function downloadInstanceFile(item: InstanceFileItem) {
  const filePath = `${filesCurrentPath.value}/${item.name}`
  const url = `/api/v1/instances/${instanceId.value}/files/workspace/download?path=${encodeURIComponent(filePath)}`
  const token = localStorage.getItem('portal_token')
  if (!token) return
  try {
    const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } })
    if (!res.ok) {
      toast.error(t('instanceFiles.downloadFailed'))
      return
    }
    const blob = await res.blob()
    const blobUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = blobUrl
    a.download = item.name
    a.click()
    URL.revokeObjectURL(blobUrl)
  } catch {
    toast.error(t('instanceFiles.downloadFailed'))
  }
}

function toggleFilesPanel() {
  filesPanelOpen.value = !filesPanelOpen.value
  // 每次打开都重新拉取，不复用上次缓存——否则生成新文件后再打开面板看到的还是旧列表，
  // 得手动刷新整个页面才能看到，体验上像是没生效
  if (filesPanelOpen.value) fetchInstanceFiles()
}

// ── 思考指示器 ───────────────────────────────
// 当 agent:typing 到达但尚未收到第一个 chunk 时显示
const isTyping = ref(false)

// ── 技能列表 ─────────────────────────────────
interface SkillItem {
  skill_name: string
  name: string
  description?: string
  type: string
}

const skills = ref<SkillItem[]>([])

// ── 斜杠命令下拉框 ────────────────────────────
const textareaRef = ref<HTMLTextAreaElement | null>(null)
const showSkillDropdown = ref(false)
const skillFilter = ref('')
const slashPosition = ref(-1)
const dropdownIndex = ref(0)

const filteredSkills = computed(() => {
  const q = skillFilter.value.toLowerCase()
  if (!q) return skills.value
  return skills.value.filter(
    (s) =>
      s.skill_name.toLowerCase().startsWith(q) ||
      s.name.toLowerCase().startsWith(q),
  )
})

// ── 输入框 ───────────────────────────────────
const inputText = ref('')
const sending = ref(false)

// ── 状态展示 ─────────────────────────────────
const statusDisplay = computed(() =>
  getStatusDisplay(instance.value?.display_status ?? ''),
)

// 剥离 LLM 内部格式标签（如 <final>…</final>），只展示正文
function cleanContent(text: string): string {
  return text
    .replace(/<final[^>]*>/gi, '')
    .replace(/<\/final[^>]*>/gi, '')
    .trim()
}

// 滚动到底部
async function scrollToBottom() {
  await nextTick()
  chatEndRef.value?.scrollIntoView({ behavior: 'smooth' })
}

// 加载实例信息
async function loadInstance() {
  instanceLoading.value = true
  instanceError.value = ''
  try {
    const res = await api.get(`/instances/${instanceId.value}`)
    instance.value = res.data.data
  } catch {
    instanceError.value = t('instanceChat.loadFailed')
  } finally {
    instanceLoading.value = false
  }
}

// 查找该实例所属的工作空间
async function findWorkspace() {
  workspaceLoading.value = true
  try {
    await store.fetchWorkspaces()
    const found = store.workspaces.find((ws: WorkspaceListItem) =>
      ws.agents?.some((a) => a.instance_id === instanceId.value),
    )
    workspace.value = found ?? null
  } finally {
    workspaceLoading.value = false
  }
}

// 加载该 AI 员工的历史会话列表
async function loadSessions() {
  if (!workspace.value) return
  sessionsLoading.value = true
  try {
    const list = await store.fetchInstanceConversations(workspace.value.id, instanceId.value)
    // 按最近消息时间降序，置顶有消息的会话
    sessions.value = list.sort((a, b) => {
      if (!a.last_message_at && !b.last_message_at) return 0
      if (!a.last_message_at) return 1
      if (!b.last_message_at) return -1
      return new Date(b.last_message_at).getTime() - new Date(a.last_message_at).getTime()
    })
  } finally {
    sessionsLoading.value = false
  }
}

// 创建新会话
async function createNewSession() {
  if (!workspace.value || creatingSession.value) return
  creatingSession.value = true
  try {
    const userId = authStore.user?.id || ''
    const index = sessions.value.length + 1
    const name = t('instanceChat.sessionTitle', { index })
    const conv = await store.createConversation(workspace.value.id, name, [
      instanceId.value,
      userId,
    ])
    sessions.value.unshift(conv)
    await switchSession(conv.id)
  } finally {
    creatingSession.value = false
  }
}

// 删除会话
async function deleteSession(sessionId: string) {
  if (!workspace.value) return
  try {
    await api.delete(`/workspaces/${workspace.value.id}/conversations/${sessionId}`)
  } catch {
    // 删除失败也从本地列表移除，保持 UI 一致
  }
  const idx = sessions.value.findIndex((s) => s.id === sessionId)
  if (idx !== -1) sessions.value.splice(idx, 1)

  // 若删除的是当前激活会话，切换到最新的或新建一个
  if (activeSessionId.value === sessionId) {
    activeSessionId.value = null
    messages.value = []
    if (sessions.value.length > 0) {
      await switchSession(sessions.value[0].id)
    } else {
      await createNewSession()
    }
  }
}

// 切换会话
async function switchSession(sessionId: string) {
  if (activeSessionId.value === sessionId) return
  activeSessionId.value = sessionId
  messages.value = []
  isTyping.value = false
  await loadSessionMessages()
}

// 加载当前会话的消息
async function loadSessionMessages() {
  if (!workspace.value || !activeSessionId.value) return
  messagesLoading.value = true
  try {
    const msgs = await store.fetchConversationMessages(workspace.value.id, activeSessionId.value)
    messages.value = msgs as ChatMsg[]
    await scrollToBottom()
  } finally {
    messagesLoading.value = false
  }
}

// 加载已安装的技能列表
async function loadSkills() {
  try {
    const res = await api.get(`/instances/${instanceId.value}/skills`)
    skills.value = (res.data.data || []) as SkillItem[]
  } catch {
    // 技能加载失败不影响主流程
  }
}

// 相对时间格式化
function relativeTime(iso: string | null): string {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const min = Math.floor(diff / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h} 小时前`
  const d = Math.floor(h / 24)
  if (d < 30) return `${d} 天前`
  return new Date(iso).toLocaleDateString()
}

// SSE 事件回调（仅保留系统事件，消息流已改用直连）
function onSSEEvent(_event: string, _data: Record<string, unknown>) {}

// 发送消息：调用私人对话直连端点，流式读取响应
async function sendMessage() {
  if ((!inputText.value.trim() && pendingFiles.value.length === 0) || sending.value || fileUploading.value || !workspace.value || !activeSessionId.value) return

  const text = inputText.value.trim()
  inputText.value = ''
  sending.value = true
  isTyping.value = true

  // 有待发送文件时先逐个上传，拿到 file_id 供发送接口使用
  let fileIds: string[] | undefined
  let attachments: FileAttachment[] | undefined
  if (pendingFiles.value.length > 0) {
    fileUploading.value = true
    const filesToUpload = [...pendingFiles.value]
    pendingFiles.value = []
    try {
      const uploaded: FileAttachment[] = []
      for (const f of filesToUpload) {
        const result = await store.uploadFile(workspace.value.id, f)
        if (result) uploaded.push(result)
      }
      if (uploaded.length > 0) {
        fileIds = uploaded.map((u) => u.id)
        attachments = uploaded
      }
    } catch {
      toast.error(t('chat.fileUploadFailed'))
    } finally {
      fileUploading.value = false
    }
  }

  // 乐观更新：立即显示用户消息
  messages.value.push({
    id: `local-${Date.now()}`,
    sender_type: 'user',
    sender_id: authStore.user?.id || 'me',
    sender_name: authStore.user?.name || t('instanceChat.you'),
    content: text,
    created_at: new Date().toISOString(),
    attachments,
  })
  await scrollToBottom()

  // 更新会话预览
  const session = sessions.value.find((s) => s.id === activeSessionId.value)
  if (session) {
    session.last_message_at = new Date().toISOString()
    session.last_message_preview = text.slice(0, 60)
  }

  // 创建占位流式消息气泡
  const streamMsg: ChatMsg = {
    id: `stream-${Date.now()}`,
    sender_type: 'agent',
    sender_id: instanceId.value,
    sender_name: instance.value?.name || '',
    content: '',
    created_at: new Date().toISOString(),
    streaming: true,
  }

  try {
    const token = localStorage.getItem('portal_token') || ''
    const response = await fetch(
      `/api/v1/workspaces/${workspace.value.id}/agents/${instanceId.value}/chat`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`,
        },
        body: JSON.stringify({
          message: text,
          conversation_id: activeSessionId.value,
          file_ids: fileIds,
        }),
      },
    )

    if (!response.ok || !response.body) {
      throw new Error(`HTTP ${response.status}`)
    }

    // 第一个字节到达时结束思考指示器，推入流式消息气泡
    isTyping.value = false
    messages.value.push(streamMsg)
    await scrollToBottom()

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const payload = line.slice(6)
        if (payload === '[DONE]') {
          streamMsg.streaming = false
          // 更新会话预览为最终回复内容
          if (session) session.last_message_preview = streamMsg.content.slice(0, 60)
          // 文件面板开着时顺带刷新一下，尽快看到这一轮可能生成的新文件
          if (filesPanelOpen.value) fetchInstanceFiles()
          break
        }
        try {
          const parsed = JSON.parse(payload) as { content?: string; error?: string }
          if (parsed.error) {
            streamMsg.content = `[${parsed.error}]`
            streamMsg.streaming = false
          } else if (parsed.content) {
            streamMsg.content += parsed.content
            scrollToBottom()
          }
        } catch {
          // 忽略非 JSON 行
        }
      }
    }
  } catch {
    isTyping.value = false
    // 若气泡已推入则标记完成，否则移除
    if (messages.value.includes(streamMsg)) {
      streamMsg.streaming = false
    }
  } finally {
    sending.value = false
    streamMsg.streaming = false
  }
}

// 检测 textarea 中的斜杠命令，触发技能下拉框
function handleInput() {
  const ta = textareaRef.value
  if (!ta || skills.value.length === 0) return

  const cursor = ta.selectionStart ?? 0
  const text = ta.value

  // 从光标向前扫描，找到最近的 / 命令起始位置
  let slashIdx = -1
  for (let i = cursor - 1; i >= 0; i--) {
    if (text[i] === '/') {
      // / 需位于行首或空白之后
      if (i === 0 || /[\s\n]/.test(text[i - 1])) {
        slashIdx = i
      }
      break
    } else if (/[\s\n]/.test(text[i])) {
      break
    }
  }

  if (slashIdx >= 0) {
    slashPosition.value = slashIdx
    skillFilter.value = text.slice(slashIdx + 1, cursor)
    dropdownIndex.value = 0
    showSkillDropdown.value = filteredSkills.value.length > 0
  } else {
    showSkillDropdown.value = false
  }
}

// 从下拉框选中一个技能，将 /xxx 替换为 /skill_name 并关闭下拉框
function selectSkillFromDropdown(skill: SkillItem) {
  const ta = textareaRef.value
  if (!ta) return

  const cursor = ta.selectionStart ?? 0
  const text = ta.value
  const before = text.slice(0, slashPosition.value)
  const after = text.slice(cursor)
  const inserted = `/${skill.skill_name} `

  inputText.value = before + inserted + after
  showSkillDropdown.value = false

  nextTick(() => {
    const pos = before.length + inserted.length
    ta.setSelectionRange(pos, pos)
    ta.focus()
  })
}

// 按 Enter 发送（Shift+Enter 换行）；下拉框开启时 Enter/ArrowUp/ArrowDown/Esc 控制下拉框
function onKeydown(e: KeyboardEvent) {
  if (showSkillDropdown.value) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      dropdownIndex.value = Math.min(dropdownIndex.value + 1, filteredSkills.value.length - 1)
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      dropdownIndex.value = Math.max(dropdownIndex.value - 1, 0)
      return
    }
    if (e.key === 'Enter') {
      e.preventDefault()
      const skill = filteredSkills.value[dropdownIndex.value]
      if (skill) selectSkillFromDropdown(skill)
      return
    }
    if (e.key === 'Escape') {
      showSkillDropdown.value = false
      return
    }
  }

  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    sendMessage()
  }
}

onMounted(async () => {
  store.fetchSystemCapabilities()
  await loadInstance()
  await findWorkspace()

  if (workspace.value) {
    await Promise.all([loadSessions(), loadSkills()])

    if (sessions.value.length > 0) {
      activeSessionId.value = sessions.value[0].id
      await loadSessionMessages()
    } else {
      await createNewSession()
    }
  }
})

onUnmounted(() => {
  revokePreviewPdfUrl()
})
</script>

<template>
  <div class="h-[calc(100vh-3.5rem)] flex flex-col bg-background">
    <!-- 加载中 -->
    <div v-if="instanceLoading || workspaceLoading" class="flex-1 flex items-center justify-center">
      <Loader2 class="w-6 h-6 animate-spin text-muted-foreground" />
    </div>

    <!-- 加载失败 -->
    <div v-else-if="instanceError" class="flex-1 flex flex-col items-center justify-center gap-3">
      <AlertCircle class="w-8 h-8 text-red-400" />
      <p class="text-sm text-muted-foreground">{{ instanceError }}</p>
      <button
        class="px-3 py-1.5 rounded-lg border border-border text-sm hover:bg-accent transition-colors"
        @click="loadInstance"
      >
        {{ t('instanceList.retry') }}
      </button>
    </div>

    <!-- 未加入工作空间提示 -->
    <div v-else-if="!workspace" class="flex-1 flex flex-col items-center justify-center gap-3 px-6 text-center">
      <div class="w-14 h-14 rounded-2xl bg-primary/10 flex items-center justify-center">
        <Bot class="w-7 h-7 text-primary" />
      </div>
      <h3 class="font-semibold">{{ t('instanceChat.noWorkspaceTitle') }}</h3>
      <p class="text-sm text-muted-foreground max-w-sm">
        {{ t('instanceChat.noWorkspaceDesc') }}
      </p>
      <button
        class="px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors"
        @click="router.push('/')"
      >
        {{ t('instanceChat.goToWorkspaces') }}
      </button>
    </div>

    <!-- 主对话区域：左侧会话面板 + 右侧消息区 -->
    <div v-else class="flex-1 flex overflow-hidden">

        <!-- ── 左侧会话侧边栏 200px ── -->
        <aside class="w-[200px] shrink-0 border-r border-border flex flex-col bg-card">
          <!-- 新建对话按钮 -->
          <div class="p-3 border-b border-border">
            <button
              class="w-full flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg bg-primary/10 text-primary text-xs font-medium hover:bg-primary/20 transition-colors disabled:opacity-50"
              :disabled="creatingSession"
              @click="createNewSession"
            >
              <Loader2 v-if="creatingSession" class="w-3.5 h-3.5 animate-spin" />
              <Plus v-else class="w-3.5 h-3.5" />
              {{ t('instanceChat.newSession') }}
            </button>
          </div>

          <!-- 会话列表 -->
          <div class="flex-1 overflow-y-auto py-1">
            <div v-if="sessionsLoading" class="flex justify-center py-4">
              <Loader2 class="w-4 h-4 animate-spin text-muted-foreground" />
            </div>
            <template v-else>
              <button
                v-for="session in sessions"
                :key="session.id"
                class="group w-full text-left px-3 py-2.5 flex flex-col gap-0.5 hover:bg-accent transition-colors relative"
                :class="session.id === activeSessionId ? 'bg-accent/60' : ''"
                @click="switchSession(session.id)"
              >
                <!-- 激活指示条 -->
                <span
                  v-if="session.id === activeSessionId"
                  class="absolute left-0 top-2 bottom-2 w-0.5 rounded-full bg-primary"
                />
                <div class="flex items-center justify-between gap-1 pl-1">
                  <span class="text-xs font-medium truncate flex-1">{{ session.name }}</span>
                  <!-- 删除按钮：hover 时显示，带背景色与侧边栏区分 -->
                  <button
                    class="shrink-0 opacity-0 group-hover:opacity-100 p-0.5 rounded text-white hover:text-destructive transition-all"
                    @click.stop="deleteSession(session.id)"
                  >
                    <Trash2 class="w-3 h-3" />
                  </button>
                </div>
                <p v-if="session.last_message_preview" class="text-[11px] text-muted-foreground truncate pl-1">
                  {{ session.last_message_preview }}
                  <span class="ml-1 text-muted-foreground/60">· {{ relativeTime(session.last_message_at) }}</span>
                </p>
                <div v-else class="flex items-center gap-1 pl-1">
                  <MessageSquare class="w-2.5 h-2.5 text-muted-foreground/50" />
                  <span class="text-[11px] text-muted-foreground/50">{{ t('instanceChat.startHint', { name: '' }).trim().replace('和', '').replace('打个招呼吧', '') }}</span>
                </div>
              </button>
            </template>
          </div>
        </aside>

        <!-- ── 右侧：顶部栏 + 消息区 + 输入区 ── -->
        <div class="flex-1 flex flex-col min-w-0">

          <!-- 顶部导航栏 -->
          <header class="flex items-center gap-3 px-4 py-3 border-b border-border bg-card shrink-0">
            <button
              class="p-1.5 rounded-lg hover:bg-accent transition-colors"
              @click="router.back()"
            >
              <ArrowLeft class="w-4 h-4" />
            </button>

            <template v-if="instance">
              <div class="w-8 h-8 rounded-xl bg-primary/10 flex items-center justify-center shrink-0">
                <Bot class="w-4 h-4 text-primary" />
              </div>
              <div class="flex-1 min-w-0">
                <div class="font-semibold text-sm truncate">{{ instance.name }}</div>
                <div class="flex items-center gap-1.5 text-xs">
                  <span
                    class="w-1.5 h-1.5 rounded-full"
                    :class="[statusDisplay.bgColor, statusDisplay.pulse ? 'animate-pulse' : '']"
                  />
                  <span :class="statusDisplay.color">
                    {{ t(`displayStatus.${statusDisplay.key}`) }}
                  </span>
                </div>
              </div>
            </template>

            <BaseTooltip :text="t('instanceChat.filesPanelTitle')">
              <button
                class="p-1.5 rounded-lg hover:bg-accent transition-colors shrink-0"
                :class="filesPanelOpen ? 'text-primary bg-primary/10' : 'text-muted-foreground'"
                @click="toggleFilesPanel"
              >
                <Folder class="w-4 h-4" />
              </button>
            </BaseTooltip>
          </header>

          <!-- 消息列表 -->
          <div class="flex-1 overflow-y-auto px-4 py-4 space-y-4">
            <div v-if="messagesLoading" class="flex justify-center py-4">
              <Loader2 class="w-5 h-5 animate-spin text-muted-foreground" />
            </div>

            <div
              v-else-if="messages.length === 0 && !isTyping"
              class="flex flex-col items-center justify-center py-16 gap-3 text-center"
            >
              <div class="w-12 h-12 rounded-2xl bg-primary/10 flex items-center justify-center">
                <Bot class="w-6 h-6 text-primary" />
              </div>
              <p class="text-sm text-muted-foreground">
                {{ t('instanceChat.startHint', { name: instance?.name }) }}
              </p>
            </div>

            <template v-else>
              <!-- 消息气泡 -->
              <div
                v-for="msg in messages"
                :key="msg.id"
                class="flex gap-3"
                :class="msg.sender_type === 'user' ? 'flex-row-reverse' : 'flex-row'"
              >
                <div
                  class="w-7 h-7 rounded-full flex items-center justify-center shrink-0 text-xs font-semibold mt-0.5"
                  :class="msg.sender_type === 'user' ? 'bg-primary text-primary-foreground' : 'bg-primary/15 text-primary'"
                >
                  <template v-if="msg.sender_type === 'user'">
                    {{ (msg.sender_name || '?').charAt(0).toUpperCase() }}
                  </template>
                  <Bot v-else class="w-3.5 h-3.5" />
                </div>

                <div class="flex flex-col min-w-0 max-w-[72%]" :class="msg.sender_type === 'user' ? 'items-end' : 'items-start'">
                  <div
                    class="rounded-2xl px-3.5 py-2.5 text-sm whitespace-pre-wrap break-words leading-relaxed"
                    :class="
                      msg.sender_type === 'user'
                        ? 'bg-primary text-primary-foreground rounded-tr-sm'
                        : 'bg-card border border-border rounded-tl-sm'
                    "
                  >
                    <div
                      v-if="msg.sender_type === 'agent' && msg.sender_id !== instanceId"
                      class="text-xs text-muted-foreground mb-1 font-medium"
                    >
                      {{ msg.sender_name }}
                    </div>
                    {{ cleanContent(msg.content) }}
                    <!-- 流式输出光标 -->
                    <span v-if="msg.streaming" class="inline-block w-1 h-3.5 ml-0.5 bg-current animate-pulse align-middle" />
                  </div>
                  <FileAttachmentList
                    v-if="msg.attachments?.length"
                    :attachments="msg.attachments"
                    :workspace-id="workspace?.id || ''"
                    class="mt-1"
                  />
                </div>
              </div>

              <!-- 思考指示器：等待第一个 chunk 时显示 -->
              <div v-if="isTyping" class="flex gap-3 flex-row">
                <div class="w-7 h-7 rounded-full bg-primary/15 text-primary flex items-center justify-center shrink-0 mt-0.5">
                  <Bot class="w-3.5 h-3.5" />
                </div>
                <div class="bg-card border border-border rounded-2xl rounded-tl-sm px-3.5 py-2.5 text-sm text-muted-foreground">
                  <span class="flex items-center gap-1">
                    <span class="w-1.5 h-1.5 rounded-full bg-muted-foreground/60 animate-bounce" style="animation-delay: 0ms" />
                    <span class="w-1.5 h-1.5 rounded-full bg-muted-foreground/60 animate-bounce" style="animation-delay: 150ms" />
                    <span class="w-1.5 h-1.5 rounded-full bg-muted-foreground/60 animate-bounce" style="animation-delay: 300ms" />
                  </span>
                </div>
              </div>
            </template>

            <!-- 滚动锚点 -->
            <div ref="chatEndRef" />
          </div>

          <!-- 输入区域 -->
          <div class="shrink-0 border-t border-border bg-card px-4 pt-2 pb-3" @dragover="handleDragOver" @drop="handleDrop">
            <!-- 待发送文件预览 -->
            <div v-if="pendingFiles.length > 0" class="flex flex-wrap gap-2 mb-2">
              <div
                v-for="(file, idx) in pendingFiles"
                :key="idx"
                class="group relative flex items-center gap-1.5 px-2 py-1 rounded-md bg-background border border-border text-xs max-w-[200px]"
              >
                <FileText class="w-3.5 h-3.5 shrink-0 text-muted-foreground" />
                <span class="truncate">{{ file.name }}</span>
                <span class="text-muted-foreground shrink-0">({{ formatFileSize(file.size) }})</span>
                <button
                  class="absolute -top-1.5 -right-1.5 p-0.5 rounded-full bg-destructive text-destructive-foreground opacity-0 group-hover:opacity-100 transition-opacity"
                  @click.stop="removePendingFile(idx)"
                >
                  <X class="w-2.5 h-2.5" />
                </button>
              </div>
            </div>
            <div v-if="fileUploading" class="flex items-center gap-1.5 text-xs text-muted-foreground mb-1.5">
              <Loader2 class="w-3 h-3 animate-spin" />
              <span>{{ t('chat.fileUploading') }}</span>
            </div>

            <!-- 文本输入和发送按钮（相对定位，用于锚定下拉框） -->
            <div class="relative flex items-end gap-2">
              <!-- 技能斜杠命令下拉框：显示在 textarea 上方 -->
              <div
                v-if="showSkillDropdown && filteredSkills.length > 0"
                class="absolute bottom-full left-0 mb-1 w-64 max-h-52 overflow-y-auto rounded-xl border border-border bg-popover shadow-lg z-50"
              >
                <div class="px-2.5 py-1.5 text-[11px] text-muted-foreground border-b border-border">
                  {{ t('instanceChat.skillSelect') }}
                </div>
                <button
                  v-for="(skill, idx) in filteredSkills"
                  :key="skill.skill_name"
                  class="w-full text-left px-3 py-2 flex flex-col gap-0.5 hover:bg-accent transition-colors"
                  :class="idx === dropdownIndex ? 'bg-accent' : ''"
                  @mousedown.prevent="selectSkillFromDropdown(skill)"
                >
                  <span class="text-sm font-medium">/{{ skill.skill_name }}</span>
                  <span v-if="skill.description" class="text-[11px] text-muted-foreground truncate">
                    {{ skill.description }}
                  </span>
                </button>
              </div>

              <input
                ref="fileInputRef"
                type="file"
                multiple
                class="hidden"
                @change="handleFileSelect"
              />
              <BaseTooltip :text="!store.fileUploadEnabled ? t('chat.fileUploadDisabled') : ''">
                <button
                  class="shrink-0 w-9 h-9 rounded-xl flex items-center justify-center transition-colors"
                  :class="store.fileUploadEnabled
                    ? 'text-muted-foreground hover:text-foreground hover:bg-accent'
                    : 'text-muted-foreground/40 cursor-not-allowed'"
                  :disabled="!store.fileUploadEnabled"
                  @click="triggerFileInput"
                >
                  <Paperclip class="w-4 h-4" />
                </button>
              </BaseTooltip>

              <textarea
                ref="textareaRef"
                v-model="inputText"
                rows="2"
                class="flex-1 resize-none rounded-xl border border-border bg-background px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary/40 transition-colors placeholder:text-muted-foreground"
                :placeholder="t('instanceChat.placeholder', { name: instance?.name || 'AI 员工' })"
                @input="handleInput"
                @keydown="onKeydown"
                @blur="showSkillDropdown = false"
              />
              <button
                class="shrink-0 w-9 h-9 rounded-xl bg-primary text-primary-foreground flex items-center justify-center hover:bg-primary/90 transition-colors disabled:opacity-50"
                :disabled="(!inputText.trim() && pendingFiles.length === 0) || sending || fileUploading || !activeSessionId"
                @click="sendMessage"
              >
                <Send v-if="!sending" class="w-4 h-4" />
                <Loader2 v-else class="w-4 h-4 animate-spin" />
              </button>
            </div>
          </div>
        </div>
    </div>

    <!-- AI 员工生成文件面板（精简版：面包屑 + 列表 + 下载，无预览/编辑） -->
    <Teleport to="body">
      <Transition name="slide-right">
        <div
          v-if="filesPanelOpen"
          class="fixed inset-y-0 right-0 w-xl max-w-full bg-background border-l border-border shadow-xl z-50 flex flex-col"
        >
          <div class="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
            <h3 class="font-medium text-sm">{{ t('instanceChat.filesPanelTitle') }}</h3>
            <button class="p-1 rounded hover:bg-accent transition-colors" @click="filesPanelOpen = false">
              <X class="w-4 h-4" />
            </button>
          </div>

          <div class="px-4 py-2 border-b border-border shrink-0">
            <div class="flex items-center gap-1 text-sm flex-wrap">
              <template v-if="filesListing">
                <template v-for="(part, idx) in filesListing.breadcrumb" :key="idx">
                  <ChevronRight v-if="idx > 0" class="w-3 h-3 text-muted-foreground shrink-0" />
                  <button
                    v-if="idx < filesListing.breadcrumb.length - 1"
                    class="text-muted-foreground hover:text-foreground transition-colors"
                    @click="handleFilesBreadcrumb(idx)"
                  >
                    {{ filesBreadcrumbLabel(part) }}
                  </button>
                  <span v-else class="text-foreground font-medium">{{ filesBreadcrumbLabel(part) }}</span>
                </template>
              </template>
            </div>
          </div>

          <div class="flex-1 overflow-y-auto">
            <div v-if="filesLoading" class="flex items-center justify-center py-16">
              <Loader2 class="w-5 h-5 animate-spin text-muted-foreground" />
            </div>
            <div v-else-if="filesError" class="text-center py-16 space-y-3">
              <p class="text-sm text-red-400">{{ filesError }}</p>
              <button
                class="px-3 py-1.5 rounded-lg border border-border text-xs hover:bg-accent transition-colors"
                @click="fetchInstanceFiles"
              >{{ t('instanceList.retry') }}</button>
            </div>
            <div
              v-else-if="filesListing && filesListing.items.length === 0"
              class="text-center py-16 text-sm text-muted-foreground"
            >
              {{ t('instanceFiles.emptyDir') }}
            </div>
            <table v-else-if="filesListing" class="w-full text-sm">
              <thead>
                <tr class="border-b border-border bg-card/60">
                  <th class="text-left px-4 py-2 font-medium text-muted-foreground text-xs">
                    {{ t('instanceFiles.fileName') }}
                  </th>
                  <th class="text-left px-4 py-2 font-medium text-muted-foreground text-xs w-20">
                    {{ t('instanceFiles.fileSize') }}
                  </th>
                  <th class="text-left px-4 py-2 font-medium text-muted-foreground text-xs w-28">
                    {{ t('instanceFiles.fileModified') }}
                  </th>
                  <th class="w-10" />
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="item in filesListing.items"
                  :key="item.name"
                  class="border-b border-border last:border-b-0 hover:bg-accent/50 transition-colors"
                  :class="item.is_dir || isPreviewable(item) ? 'cursor-pointer' : ''"
                  @click="handleFileItemClick(item)"
                >
                  <td class="px-4 py-2.5">
                    <div class="flex items-center gap-2 min-w-0">
                      <Folder v-if="item.is_dir" class="w-4 h-4 shrink-0 text-primary" />
                      <FileText v-else class="w-4 h-4 shrink-0 text-muted-foreground" />
                      <span class="truncate" :class="item.is_dir ? 'font-medium' : ''">{{ item.name }}</span>
                    </div>
                  </td>
                  <td class="px-4 py-2.5 text-muted-foreground tabular-nums whitespace-nowrap text-xs">
                    {{ item.is_dir ? '-' : formatFileSize(item.size) }}
                  </td>
                  <td class="px-4 py-2.5 text-muted-foreground whitespace-nowrap text-xs">
                    {{ relativeTime(item.modified_at) }}
                  </td>
                  <td class="px-2 py-2.5 text-right">
                    <button
                      v-if="!item.is_dir"
                      class="p-1 rounded hover:bg-accent transition-colors text-muted-foreground hover:text-foreground"
                      :title="t('instanceFiles.download')"
                      @click.stop="downloadInstanceFile(item)"
                    >
                      <Download class="w-3.5 h-3.5" />
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
            <div
              v-if="filesListing && filesTruncatedCount > 0"
              class="px-4 py-2 text-xs text-muted-foreground text-center border-t border-border"
            >
              {{ t('instanceChat.filesTruncated', { count: filesTruncatedCount }) }}
            </div>
          </div>
        </div>
      </Transition>
      <Transition name="fade">
        <div
          v-if="filesPanelOpen"
          class="fixed inset-0 bg-black/30 z-40"
          @click="filesPanelOpen = false"
        />
      </Transition>
    </Teleport>

    <!-- 文件内容预览（只读，不提供编辑） -->
    <Teleport to="body">
      <Transition name="slide-right">
        <div
          v-if="previewVisible"
          class="fixed inset-y-0 right-0 w-xl max-w-full bg-background border-l border-border shadow-xl z-[60] flex flex-col"
        >
          <div class="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
            <div class="flex items-center gap-2 min-w-0">
              <button class="p-1 rounded hover:bg-accent transition-colors shrink-0" @click="closeFilePreview">
                <ArrowLeft class="w-4 h-4" />
              </button>
              <h3 class="font-medium text-sm truncate">{{ previewFileName }}</h3>
            </div>
            <button class="p-1 rounded hover:bg-accent transition-colors" @click="closeFilePreview">
              <X class="w-4 h-4" />
            </button>
          </div>
          <div class="flex-1 overflow-auto" :class="previewPdfUrl ? '' : 'p-4'">
            <iframe
              v-if="previewPdfUrl"
              :src="previewPdfUrl"
              class="w-full h-full border-0"
              :title="previewFileName"
            />
            <div v-else-if="previewLoading" class="flex items-center justify-center py-10">
              <Loader2 class="w-5 h-5 animate-spin text-muted-foreground" />
            </div>
            <div v-else-if="previewError" class="text-sm text-red-400">{{ previewError }}</div>
            <div v-else-if="previewTruncated" class="text-sm text-muted-foreground">
              {{ t('instanceFiles.fileTooLarge') }}
            </div>
            <div v-else-if="previewBinary" class="text-sm text-muted-foreground">
              {{ t('instanceFiles.binaryFile') }}
            </div>
            <pre v-else class="text-xs leading-relaxed font-mono whitespace-pre-wrap break-all text-foreground">{{ previewContent }}</pre>
          </div>
        </div>
      </Transition>
      <Transition name="fade">
        <div
          v-if="previewVisible"
          class="fixed inset-0 bg-black/30 z-[55]"
          @click="closeFilePreview"
        />
      </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
.slide-right-enter-active,
.slide-right-leave-active {
  transition: transform 0.25s ease;
}
.slide-right-enter-from,
.slide-right-leave-to {
  transform: translateX(100%);
}
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.25s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
