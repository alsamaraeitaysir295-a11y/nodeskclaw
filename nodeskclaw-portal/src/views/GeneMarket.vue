<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  Search,
  Loader2,
  Star,
  Package,
  Code,
  Database,
  Cpu,
  Server,
  Shield,
  Zap,
  Wrench,
  Palette,
  MessageSquare,
  Network,
  Sparkles,
  Layers,
  Dna,
  Download,
  TrendingUp,
  AlertCircle,
  Activity,
  Check,
  X,
  Upload,
  FolderOpen,
  AlertTriangle,
  Code2,
  Settings,
  Trash2,
  FolderDown,
  type Component,
} from 'lucide-vue-next'
import { useGeneStore } from '@/stores/gene'
import type { GeneItem, GenomeItem, TemplateInfo } from '@/stores/gene'
import { useToast } from '@/composables/useToast'
import { useAuthStore } from '@/stores/auth'
import { resolveApiErrorMessage } from '@/i18n/error'
import CustomSelect from '@/components/shared/CustomSelect.vue'
import { skillApi } from '@/services/skills'
import type { MarketStats, MarketStatsRankItem } from '@/services/skills'
import { suggestNextPatch } from '@/utils/semver'
import { iconColorClass } from '@/utils/skillIconColor'

const router = useRouter()
const store = useGeneStore()
const authStore = useAuthStore()  // 用于获取当前登录用户，判断删除权限
const toast = useToast()
const { t } = useI18n()

const viewMode = ref<'genes' | 'templates' | 'local' | 'stats'>('genes')
const keyword = ref('')
const selectedCategory = ref<string | null>(null)
// 三栏归属过滤：默认「全部」（公共市场 + 组织 + 个人）；'all' / 'personal' / 'org_private' / 'public'
const selectedVisibility = ref<string>('all')
const sortBy = ref('popularity')
const page = ref(1)
const pageSize = ref(12)

// ── 本地上传 Tab 状态 ──────────────────────────────
const showLocalUpload = ref(false)
const localUploading = ref(false)
const localError = ref<string | null>(null)
const localSuccess = ref<string | null>(null)
const localDragOver = ref(false)
const localFileInputRef = ref<HTMLInputElement>()
const selectedLocalFiles = ref<string[]>([])
const localFolderInputRef = ref<HTMLInputElement>()

// 上传限制：需与后端 genes.py 的 _MAX_UPLOAD_* 常量保持一致，防内存/存储 DoS
const MAX_UPLOAD_FILE_SIZE = 10 * 1024 * 1024 // 单文件 10MB
const MAX_UPLOAD_TOTAL_SIZE = 50 * 1024 * 1024 // 总大小 50MB
const MAX_UPLOAD_FILE_COUNT = 500 // 单次最多 500 个文件

// 上传前的本地校验：超限时返回中文提示，未超限返回 null
function validateUploadFiles(files: FileList): string | null {
  if (files.length > MAX_UPLOAD_FILE_COUNT) {
    return `文件数量超过限制（最多 ${MAX_UPLOAD_FILE_COUNT} 个，当前 ${files.length} 个）`
  }
  let totalSize = 0
  for (const file of Array.from(files)) {
    if (file.size > MAX_UPLOAD_FILE_SIZE) {
      const path = (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
      return `文件 ${path} 超过单文件大小限制（${MAX_UPLOAD_FILE_SIZE / (1024 * 1024)}MB）`
    }
    totalSize += file.size
  }
  if (totalSize > MAX_UPLOAD_TOTAL_SIZE) {
    return `上传内容总大小超过限制（${MAX_UPLOAD_TOTAL_SIZE / (1024 * 1024)}MB）`
  }
  return null
}

/** 上传核心：分类校验 + 提交 + 409 同名冲突的确认/版本号重试流程。
 * 文件夹上传与 ZIP 包上传共用（displayName 用于冲突确认弹窗里的名字展示） */
async function doUpload(fileList: FileList | File[], displayName: string) {
  // 分类必选：市场分类筛选/展示依赖该字段
  if (!uploadCategory.value) {
    localError.value = t('geneMarket.categoryRequired')
    return
  }
  localUploading.value = true
  localError.value = null
  localSuccess.value = null
  const input = fileList as FileList
  try {
    // 直接上传只能进入个人 library（后端已无条件拒绝 org/public target），无需再按目标分流文案
    const created = await skillApi.uploadFolder(input, false, 'personal', undefined, uploadCategory.value)
    localSuccess.value = '已上传到个人技能 library'
    // 带技能名的成功提示（后端返回的基因名为准，取不到回退目录/包名）
    toast.success(t('geneMarket.uploadedToPersonal', { name: created?.name || displayName }))
    showLocalUpload.value = false
    selectedLocalFiles.value = []
    await loadData()
  } catch (e: any) {
    // 409 冲突：同名基因已存在，提示用户确认覆盖并输入本次覆盖的版本号
    if (e?.response?.status === 409) {
      const msg = e?.response?.data?.message || ''
      if (msg.includes('已存在') || msg.includes('already exists')) {
        // 已知局限：后端 409 响应暂未携带冲突基因的当前版本号，此处 fallback 到 1.0.0（详见任务计划文档）
        const existingVersion: string = e?.response?.data?.data?.version || '1.0.0'
        const suggested = suggestNextPatch(existingVersion)
        const ok = confirm(`${displayName} 基因已存在（当前版本 ${existingVersion}），是否覆盖原基因？`)
        if (ok) {
          const inputVersion = prompt('请输入本次覆盖的版本号（不改内容可保持原版本号不变）', suggested)
          if (inputVersion === null) {
            localError.value = '已取消上传'
            return
          }
          // 用户清空输入框后直接点确定时 inputVersion 是空字符串而非 null，
          // 不能算取消上传；此时按建议版本号处理，避免空字符串被当作「不传版本」
          // 悄悄回退到后端默认值 1.0.0，导致「版本倒退」报错
          const finalVersion = inputVersion.trim() || suggested
          // 重新上传，携带覆盖参数与版本号
          try {
            const overwritten = await skillApi.uploadFolder(input, true, 'personal', finalVersion, uploadCategory.value ?? undefined)
            localSuccess.value = `基因已覆盖`
            toast.success(t('geneMarket.uploadedToPersonal', { name: overwritten?.name || displayName }))
            showLocalUpload.value = false
            selectedLocalFiles.value = []
            await loadData()
          } catch (e2: any) {
            localError.value = e2?.response?.data?.message || '覆盖失败'
          }
        } else {
          localError.value = '已取消上传'
        }
      } else {
        localError.value = msg || '上传失败'
      }
    } else {
      localError.value = e instanceof Error ? e.message : '上传失败'
    }
  } finally {
    localUploading.value = false
  }
}

/** ZIP 包上传：选中 .zip 后直接走同一上传链路（后端检测单 zip 条目自动解包） */
async function handleLocalFile(file: File) {
  if (file.size > MAX_UPLOAD_TOTAL_SIZE) {
    localError.value = `ZIP 包超过大小限制（${MAX_UPLOAD_TOTAL_SIZE / (1024 * 1024)}MB）`
    return
  }
  await doUpload([file], file.name.replace(/\.zip$/i, ''))
}

async function handleLocalFolder() {
  const input = localFolderInputRef.value
  if (!input?.files || input.files.length === 0) {
    localError.value = '请先选择文件夹'
    return
  }
  // 上传前本地校验大小/数量，避免把超大请求发到后端才被拒绝
  const validationError = validateUploadFiles(input.files)
  if (validationError) {
    localError.value = validationError
    return
  }
  const folderName = input.files[0]?.webkitRelativePath?.split('/')[0] || '该文件夹'
  await doUpload(input.files, folderName)
}

function onLocalFolderInput(e: Event) {
  const input = e.target as HTMLInputElement
  if (!input.files || input.files.length === 0) return
  selectedLocalFiles.value = Array.from(input.files).map(
    f => (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name
  )
}

// 分类列表：改读后端（管理员可编辑），加载失败回退默认八类
const DEFAULT_CATEGORIES = ['开发', '数据', '运维', '网络', '创意', '沟通', '安全', '效率']
const categories = ref<string[]>([...DEFAULT_CATEGORIES])

async function loadCategories() {
  try {
    const list = await skillApi.getCategories()
    if (list.length) categories.value = list.map(c => c.name)
  } catch {
    // 分类加载失败不阻塞页面，沿用默认列表
  }
}

// ── 分类管理（管理员） ──────────────────────────────
const isAdmin = computed(() => !!authStore.user?.is_super_admin)
const showCategoryDialog = ref(false)
const categoryDraft = ref<string[]>([])
const newCategoryInput = ref('')
const savingCategories = ref(false)

function openCategoryDialog() {
  categoryDraft.value = [...categories.value]
  newCategoryInput.value = ''
  showCategoryDialog.value = true
}

function addDraftCategory() {
  const name = newCategoryInput.value.trim()
  if (name && !categoryDraft.value.includes(name) && name.length <= 32) {
    categoryDraft.value.push(name)
  }
  newCategoryInput.value = ''
}

async function saveCategories() {
  savingCategories.value = true
  try {
    const list = await skillApi.updateCategories(categoryDraft.value)
    categories.value = list.map(c => c.name)
    showCategoryDialog.value = false
    toast.success(t('geneMarket.categoriesSaved'))
  } catch (e) {
    toast.error(resolveApiErrorMessage(e, t('geneMarket.categoriesSaveFailed')))
  } finally {
    savingCategories.value = false
  }
}

// ── 本地上传分类（必选） ──────────────────────────────
const uploadCategory = ref<string | null>(null)

// 视图 Tab 选项：技能 / AI员工 / 统计（本地上传不在左侧 tab 组，改为工具栏右侧操作按钮）
const viewModeTabs: { value: 'genes' | 'templates' | 'stats'; key: string }[] = [
  { value: 'genes', key: 'geneMarket.tabGenes' },
  { value: 'templates', key: 'geneMarket.tabTemplates' },
  { value: 'stats', key: 'geneMarket.tabStats' },
]

// ── 统计视图状态 ──────────────────────────────
const statsDimension = ref<'total' | 'month' | 'week'>('total')
const statsLoading = ref(false)
const marketStats = ref<MarketStats | null>(null)

const statsDimensionTabs: { value: 'total' | 'month' | 'week'; key: string }[] = [
  { value: 'total', key: 'geneMarket.statsDimTotal' },
  { value: 'month', key: 'geneMarket.statsDimMonth' },
  { value: 'week', key: 'geneMarket.statsDimWeek' },
]

async function loadStats() {
  statsLoading.value = true
  try {
    marketStats.value = await skillApi.getMarketStats(statsDimension.value)
  } catch (e) {
    // 统计页加载失败仅 toast 提示，保留上一次数据
    toast.error(resolveApiErrorMessage(e, t('geneMarket.statsLoadFailed')))
  } finally {
    statsLoading.value = false
  }
}

// 榜单条目的占比条宽度（相对榜首），避免每行都 100%
function statsBarWidth(item: MarketStatsRankItem, ranking: MarketStatsRankItem[]): string {
  const max = ranking[0]?.count || 0
  if (max <= 0) return '0%'
  return `${Math.max(4, Math.round((item.count / max) * 100))}%`
}

// 排序口径（与后端 sort_map 对齐）：热门=下载量(install_count)、评分=avg_rating、最新=上传时间
const sortOptions = ['popularity', 'rating', 'newest']

function getSortLabel(value: string) {
  const map: Record<string, string> = {
    popularity: 'geneMarket.sortPopularity',
    rating: 'geneMarket.sortRating',
    newest: 'geneMarket.sortNewest',
  }
  const key = map[value]
  if (!key) return value
  const translated = t(key)
  return translated === key ? value : translated
}

const categorySelectOptions = computed(() => [
  { value: null, label: t('geneMarket.allCategories') },
  // 分类为管理员可编辑的自由文本，直接展示原文（不走 geneMeta 本地化映射）
  ...categories.value.map(c => ({ value: c, label: c })),
])

const uploadCategoryOptions = computed(() =>
  categories.value.map(c => ({ value: c, label: c })),
)

// 归属过滤下拉选项：all = 公共+组织+个人 全展示（默认）；value 与后端 visibility 取值一致
const visibilitySelectOptions = computed(() => [
  { value: 'all', label: t('geneMarket.scopeAll') },
  { value: 'public', label: t('geneMarket.scopePublic') },
  { value: 'org_private', label: t('geneMarket.scopeOrg') },
  { value: 'personal', label: t('geneMarket.scopePersonal') },
])

const sortSelectOptions = computed(() =>
  sortOptions.map(s => ({ value: s, label: getSortLabel(s) }))
)

const iconMap: Record<string, typeof Package> = {
  code: Code,
  database: Database,
  cpu: Cpu,
  server: Server,
  shield: Shield,
  zap: Zap,
  wrench: Wrench,
  palette: Palette,
  message: MessageSquare,
  network: Network,
  sparkles: Sparkles,
  layers: Layers,
  package: Package,
}

function resolveIcon(iconName?: string) {
  if (!iconName) return Package
  const key = iconName.toLowerCase().replace(/[- ]/g, '')
  return iconMap[key] ?? iconMap[iconName] ?? Package
}

const featuredItems = computed(() => {
  if (viewMode.value === 'genes') return store.featuredGenes
  return []
})

const hasFeatured = computed(() => featuredItems.value.length > 0 && selectedVisibility.value === 'public')

const totalCount = computed(() => {
  if (viewMode.value === 'genes') return store.totalGenes
  if (viewMode.value === 'templates') return store.totalTemplates
  return 0
})

const totalPages = computed(() => Math.ceil(totalCount.value / pageSize.value) || 1)
const canPrev = computed(() => page.value > 1)
const canNext = computed(() => page.value < totalPages.value)

/**
 * 判断当前用户是否有权限删除某个 gene。
 * 前端仅做显示层过滤（上传者本人 / 超管），
 * org admin 权限需查 membership，由后端兜底拦截。
 * 仅本地上传（source_registry === 'local'）的 gene 才显示删除按钮。
 */
function canDeleteGene(gene: GeneItem): boolean {
  if (gene.source_registry !== 'local') return false
  const me = authStore.user
  if (!me) return false
  return me.is_super_admin || gene.created_by === me.id
}

/**
 * 删除 gene 的确认 + 请求逻辑。
 * 后端 409（如个人技能已被 Agent 加载）会带 message_key，由 resolveApiErrorMessage 翻译为本地化 toast。
 */
async function onDeleteGene(gene: GeneItem) {
  if (!confirm(t('geneMarket.deleteConfirm', { name: gene.name }))) return
  try {
    await skillApi.deleteGene(gene.id)
    toast.success(t('geneMarket.deleteSuccess'))
    await loadData()
  } catch (e: unknown) {
    // 走统一错误解析：优先 message_key 翻译；缺失时回退为后端原文 message，再退到通用文案
    toast.error(resolveApiErrorMessage(e, t('geneMarket.deleteFailed')))
  }
}

/**
 * fork 一份 gene 到个人 / 组织 / 公共 library。
 * 权限校验在后端兜底，前端按 canForkFrom 决定按钮显示。
 * - personal：归属当前用户，无需审核
 * - org：归属当前组织，pending_owner 等组织 admin 审核
 * - public：visibility=public 但 pending_owner，需组织 admin 审核
 */
const forkingSlug = ref<string | null>(null)
// 复合 key：同一 gene 的 personal/org/public 三个 fork 按钮各自独立请求，
// 若只按 slug 记录会导致点击其中一个按钮时其余按钮也一起显示 loading
function forkKey(slug: string, target: 'personal' | 'org' | 'public'): string {
  return `${slug}:${target}`
}
// 记录正在下载中的技能 slug，用于按钮 loading 状态控制
const downloadingSlug = ref<string | null>(null)
async function onForkGene(
  gene: GeneItem,
  target: 'personal' | 'org' | 'public',
  overwrite = false,
) {
  forkingSlug.value = forkKey(gene.slug, target)
  try {
    // 必须用 gene.id（UUID）传给后端：三向 fork 后同 slug 可在多 scope 并存，按 slug 查会冲突
    const forked = await store.forkGene(gene.id, target, overwrite)

    // org/public 目标命中覆盖时后端返回 { kind: 'overwrite_submission', ... } 而非 Gene，
    // 说明请求已进入审核队列，不代表立即生效——必须在成功文案分支之前拦截判断
    // any 转换是有意为之的最小类型逃逸：GeneItem 类型未建模 overwrite_submission 这一分支返回形状
    if ((forked as any)?.kind === 'overwrite_submission') {
      toast.success(t('geneMarket.forkOverwriteSubmitted'))
      forkingSlug.value = null
      return
    }

    // 按 target + 是否免审切换文案：admin/超管自上传时后端直接 approved，不应再提示「等待审核」
    const isApproved = forked?.review_status === 'approved'
    let successKey: string
    if (overwrite) {
      // 走到这里说明是 personal 目标的覆盖（org/public 覆盖已被上面的 kind 分支拦截并 return）
      successKey = 'geneMarket.forkOverwriteSuccess'
    } else if (target === 'personal') {
      successKey = 'geneMarket.forkToPersonalSuccess'
    } else if (target === 'org') {
      successKey = isApproved ? 'geneMarket.forkToOrgImmediate' : 'geneMarket.forkToOrgSuccess'
    } else {
      successKey = isApproved ? 'geneMarket.forkToPublicImmediate' : 'geneMarket.forkToPublicSuccess'
    }
    toast.success(t(successKey))
    // 当前正在浏览目标 scope 时刷新列表
    const visMatches =
      (target === 'personal' && selectedVisibility.value === 'personal') ||
      (target === 'org' && selectedVisibility.value === 'org_private') ||
      (target === 'public' && selectedVisibility.value === 'public')
    if (visMatches) await loadData()
  } catch (e: unknown) {
    // 已是最新版本：不算真正的失败，提示成功语义的提示语并直接返回
    const messageKey = (e as { response?: { data?: { message_key?: string } } })?.response?.data
      ?.message_key
    if (messageKey === 'errors.gene.fork_already_up_to_date') {
      toast.success(t('geneMarket.forkAlreadyUpToDate'))
      return
    }
    // 命中同名冲突且尚未处于覆盖重试中时，弹出确认框，用户确认后以 overwrite=true 重新发起请求
    // if (!overwrite) 防止确认覆盖后仍报「已存在」时无限递归重试
    if (!overwrite) {
      const msg = (e as { response?: { data?: { message?: string } } })?.response?.data?.message || ''
      if (msg.includes('已存在')) {
        const ok = confirm(t('geneMarket.forkConflictConfirm', { name: gene.name }))
        if (ok) {
          await onForkGene(gene, target, true)
          return
        }
      }
    }
    // 统一错误解析：优先 message_key 翻译（如 fork_personal_forbidden / fork_org_forbidden）
    toast.error(resolveApiErrorMessage(e, t('geneMarket.forkFailed')))
  } finally {
    forkingSlug.value = null
  }
}

/**
 * 下载技能到本地文件系统。
 * 调用 store.downloadGene，下载期间显示 loading 状态，防止重复触发。
 */
async function onDownloadGene(gene: GeneItem) {
  // 仅阻止同一个 gene 的重复下载，不影响其他 gene 并发下载
  if (downloadingSlug.value === gene.slug) return
  downloadingSlug.value = gene.slug
  try {
    await store.downloadGene(gene.slug)
  } catch (e) {
    // 与其他操作保持一致，用 toast 展示错误信息
    toast.error(resolveApiErrorMessage(e, t('geneMarket.downloadFailed')))
  } finally {
    downloadingSlug.value = null
  }
}

/**
 * 判断当前用户能将某 gene 作为源 fork 到哪些目标。
 * 与后端 fork_gene_to_library 的权限矩阵保持一致：
 *   - 源 personal：仅本人可 fork（→ org / public）
 *   - 源 org：本组成员可 fork（→ personal / public）
 *   - 源 public：任意登录用户可 fork（→ personal / org）
 * 同 scope 隐藏自身按钮；最终以后端 403 兜底，前端仅做显示层裁剪。
 */
function canForkFrom(gene: GeneItem): { personal: boolean; org: boolean; public: boolean } {
  const me = authStore.user
  const empty = { personal: false, org: false, public: false }
  if (!me) return empty

  const fromPersonal = gene.org_id == null && gene.created_by != null
  const fromPublic = gene.visibility === 'public'
  const fromOrg = !fromPersonal && !fromPublic && gene.org_id != null

  const allowed =
    me.is_super_admin ||
    fromPublic ||
    (fromPersonal && gene.created_by === me.id) ||
    (fromOrg && me.current_org_id === gene.org_id)
  if (!allowed) return empty

  // 隐藏同 scope；org / public 目标都需要有 current_org_id（前端拦一道，后端兜底）
  return {
    personal: !fromPersonal,
    org: !fromOrg && !!me.current_org_id,
    public: !fromPublic && !!me.current_org_id,
  }
}

// 模板版 fork 权限判断：直接用 visibility 字段判源 scope，不用 org_id 是否为空做代理
// （genes 那边 org_id 代理判断在 publish_gene_to_market 场景下失真，模板这边一开始就避开这个坑）
function canForkFromTemplate(tpl: TemplateInfo): { personal: boolean; org: boolean; public: boolean } {
  const me = authStore.user
  const empty = { personal: false, org: false, public: false }
  if (!me) return empty

  const fromPersonal = tpl.visibility === 'personal'
  const fromPublic = tpl.visibility === 'public'
  const fromOrg = !fromPersonal && !fromPublic

  const allowed =
    me.is_super_admin ||
    fromPublic ||
    (fromPersonal && tpl.created_by === me.id) ||
    (fromOrg && me.current_org_id === tpl.org_id)
  if (!allowed) return empty

  return {
    personal: !fromPersonal,
    org: !fromOrg && !!me.current_org_id,
    public: !fromPublic && !!me.current_org_id,
  }
}

const forkingTemplateId = ref<string | null>(null)
async function onForkTemplate(tpl: TemplateInfo, target: 'personal' | 'org' | 'public') {
  forkingTemplateId.value = forkKey(tpl.id, target)
  try {
    const forked = await store.forkTemplate(tpl.id, target)
    const isApproved = forked?.review_status === 'approved'
    let successKey: string
    if (target === 'personal') {
      successKey = 'template.forkToPersonalSuccess'
    } else if (target === 'org') {
      successKey = isApproved ? 'template.forkToOrgImmediate' : 'template.forkToOrgSuccess'
    } else {
      successKey = isApproved ? 'template.forkToPublicImmediate' : 'template.forkToPublicSuccess'
    }
    toast.success(t(successKey))
    const visMatches =
      (target === 'personal' && selectedVisibility.value === 'personal') ||
      (target === 'org' && selectedVisibility.value === 'org_private') ||
      (target === 'public' && selectedVisibility.value === 'public')
    if (visMatches) await loadData()
  } catch (e: unknown) {
    toast.error(resolveApiErrorMessage(e, t('template.forkFailed')))
  } finally {
    forkingTemplateId.value = null
  }
}

async function loadData() {
  if (viewMode.value === 'stats') {
    // 统计视图：本地 loading，不占用 store.loading（避免与列表视图互相干扰）
    await loadStats()
    return
  }
  if (viewMode.value === 'genes') {
    await store.fetchGenes({
      keyword: keyword.value || undefined,
      category: selectedCategory.value || undefined,
      visibility: selectedVisibility.value || undefined,
      sort: sortBy.value,
      page: page.value,
      page_size: pageSize.value,
    })
  } else if (viewMode.value === 'templates') {
    await store.fetchTemplates({
      keyword: keyword.value || undefined,
      visibility: selectedVisibility.value || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
  }
}

async function loadFeatured() {
  if (viewMode.value === 'genes') {
    await store.fetchFeaturedGenes()
  }
}

function goToTemplate(id: string) {
  router.push(`/gene-market/template/${id}`)
}

async function onMount() {
  // 分类加载不阻塞首屏（失败回退默认列表）
  loadCategories()
  await store.fetchGeneTags()
  await loadFeatured()
  await loadData()
}

onMounted(onMount)

watch([keyword, selectedVisibility, selectedCategory, sortBy, viewMode], () => {
  page.value = 1
  loadData()
})

// 统计维度切换：仅在统计视图下重新拉取
watch(statsDimension, () => {
  if (viewMode.value === 'stats') loadStats()
})

watch(page, loadData)

function goToGene(slug: string) {
  router.push(`/gene-market/gene/${slug}`)
}

function goToGenome(id: string) {
  router.push(`/gene-market/genome/${id}`)
}
</script>

<template>
  <div class="min-h-screen bg-background text-foreground">
    <div class="max-w-6xl mx-auto px-6 pt-6 pb-8">

      <!-- 页面标题 -->
      <h1 class="text-2xl font-bold mb-6">{{ t('geneMarket.title') }}</h1>

      <!-- 顶部工具栏：视图 Tab + 归属过滤 + 搜索筛选合并为一行，减少纵向占用；
           窄屏时 flex-wrap 自动换行 -->
      <div class="flex flex-wrap items-center gap-x-3 gap-y-2 mb-6">
        <!-- 视图 Tab：技能 / AI员工 / 统计 -->
        <div class="flex gap-1">
          <button
            v-for="mode in viewModeTabs"
            :key="mode.value"
            :class="[
              'px-3 py-1.5 rounded-lg text-sm font-medium transition-colors',
              viewMode === mode.value
                ? 'bg-primary/10 text-primary'
                : 'text-muted-foreground hover:text-foreground hover:bg-muted',
            ]"
            @click="viewMode = mode.value"
          >
            {{ t(mode.key) }}
          </button>
        </div>

        <!-- 搜索 + 归属 + 分类 + 排序：靠右排布（本地上传/统计视图无列表可筛，不展示） -->
        <div v-if="viewMode === 'genes' || viewMode === 'templates'" class="flex flex-1 flex-wrap items-center justify-end gap-2 min-w-[280px]">
          <div class="relative w-52 max-w-full">
            <Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
            <input
              v-model="keyword"
              type="text"
              :placeholder="t('geneMarket.searchPlaceholder')"
              class="w-full pl-9 pr-3 py-1.5 rounded-lg border border-border bg-card text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
            />
          </div>

          <CustomSelect v-model="selectedVisibility" :options="visibilitySelectOptions" />

          <CustomSelect
            v-if="viewMode === 'genes'"
            v-model="selectedCategory"
            :options="categorySelectOptions"
          />

          <CustomSelect v-model="sortBy" :options="sortSelectOptions" />
        </div>

        <!-- 本地上传：右侧操作按钮（筛选区隐藏时 ml-auto 兜底靠右） -->
        <button
          :class="[
            'ml-auto inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors',
            viewMode === 'local'
              ? 'bg-primary/10 text-primary border-primary/30'
              : 'border-border text-muted-foreground hover:border-primary/50 hover:text-primary',
          ]"
          @click="viewMode = 'local'"
        >
          <Upload class="w-4 h-4" />
          {{ t('geneMarket.tabLocal') }}
        </button>
      </div>

        <div v-if="store.loading && viewMode !== 'stats'" class="flex justify-center py-20">
          <Loader2 class="w-8 h-8 animate-spin text-muted-foreground" />
        </div>

        <template v-else>
          <section>
            <!-- 行式列表：多彩 squircle 图标 + 标题行内联标签 + 两行描述 + 右侧数据列，
                 条目间分隔线（参考市场设计稿），字段与旧卡片完全一致 -->
            <div class="rounded-xl border border-border bg-card divide-y divide-border overflow-hidden">
              <template v-if="viewMode === 'genes'">
              <div
                v-for="gene in store.genes"
                :key="gene.id"
                class="flex items-center gap-4 px-5 py-2 hover:bg-muted/30 transition cursor-pointer group"
                @click="goToGene(gene.slug)"
              >
                <!-- 图标：按 slug 稳定散列底色 -->
                <div
                  :class="['w-14 h-14 rounded-2xl flex items-center justify-center shrink-0', iconColorClass(gene.slug)]"
                >
                  <component :is="resolveIcon(gene.icon)" class="w-7 h-7 text-white" />
                </div>

                <!-- 中部：标题行（名称 + 分类胶囊，其余徽标/标签暂不展示）/ 描述 -->
                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-2 flex-wrap">
                    <span class="font-semibold truncate">{{ gene.name }}</span>
                    <span
                      v-if="gene.category"
                      class="shrink-0 text-xs px-2 py-0.5 rounded bg-muted/70 text-muted-foreground"
                    >
                      {{ gene.category }}
                    </span>
                  </div>
                  <p class="text-sm text-muted-foreground line-clamp-1 mt-1">
                    {{ gene.short_description ?? gene.description ?? '' }}
                  </p>
                </div>

                <!-- 右侧数据列：操作按钮 / 评分与下载 / 效能条 / fork 按钮 -->
                <div class="flex flex-col items-end gap-2 shrink-0" @click.stop>
                  <div class="flex items-center gap-1">
                    <!-- zip 下载到本地 -->
                    <button
                      class="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition"
                      :title="t('geneMarket.downloadGene')"
                      :disabled="downloadingSlug === gene.slug"
                      @click.stop="onDownloadGene(gene)"
                    >
                      <Loader2 v-if="downloadingSlug === gene.slug" class="w-4 h-4 animate-spin" />
                      <FolderDown v-else class="w-4 h-4" />
                    </button>
                    <!-- 删除：仅本地上传 gene 且当前用户有权限时显示 -->
                    <button
                      v-if="canDeleteGene(gene)"
                      class="p-1.5 rounded-md hover:bg-destructive/10 text-muted-foreground hover:text-destructive transition"
                      :title="t('geneMarket.deleteGene')"
                      @click.stop="onDeleteGene(gene)"
                    >
                      <Trash2 class="w-4 h-4" />
                    </button>
                  </div>
                  <div class="flex items-center gap-3 text-xs text-muted-foreground tabular-nums">
                    <span class="flex items-center gap-1">
                      <Star class="w-3.5 h-3.5 fill-amber-400 text-amber-400" />
                      {{ (gene.avg_rating ?? 0).toFixed(1) }}
                    </span>
                    <span class="flex items-center gap-1">
                      <Download class="w-3.5 h-3.5" />
                      {{ gene.install_count ?? 0 }}
                    </span>
                  </div>
                  <!-- fork 按钮组：根据源 scope + 当前用户权限决定显示哪些目标按钮 -->
                  <div
                    v-if="canForkFrom(gene).personal || canForkFrom(gene).org || canForkFrom(gene).public"
                    class="flex items-center gap-1.5"
                  >
                    <button
                      v-if="canForkFrom(gene).personal"
                      class="inline-flex items-center gap-1 px-2 py-1 rounded-md border border-border text-xs hover:border-primary/50 hover:text-primary transition-colors disabled:opacity-50"
                      :disabled="forkingSlug === forkKey(gene.slug, 'personal')"
                      @click.stop="onForkGene(gene, 'personal')"
                    >
                      <Loader2 v-if="forkingSlug === forkKey(gene.slug, 'personal')" class="w-3 h-3 animate-spin" />
                      <Download v-else class="w-3 h-3" />
                      {{ t('geneMarket.forkToPersonal') }}
                    </button>
                    <button
                      v-if="canForkFrom(gene).org"
                      class="inline-flex items-center gap-1 px-2 py-1 rounded-md border border-border text-xs hover:border-primary/50 hover:text-primary transition-colors disabled:opacity-50"
                      :disabled="forkingSlug === forkKey(gene.slug, 'org')"
                      @click.stop="onForkGene(gene, 'org')"
                    >
                      <Loader2 v-if="forkingSlug === forkKey(gene.slug, 'org')" class="w-3 h-3 animate-spin" />
                      <Download v-else class="w-3 h-3" />
                      {{ t('geneMarket.forkToOrg') }}
                    </button>
                    <button
                      v-if="canForkFrom(gene).public"
                      class="inline-flex items-center gap-1 px-2 py-1 rounded-md border border-border text-xs hover:border-primary/50 hover:text-primary transition-colors disabled:opacity-50"
                      :disabled="forkingSlug === forkKey(gene.slug, 'public')"
                      @click.stop="onForkGene(gene, 'public')"
                    >
                      <Loader2 v-if="forkingSlug === forkKey(gene.slug, 'public')" class="w-3 h-3 animate-spin" />
                      <Download v-else class="w-3 h-3" />
                      {{ t('geneMarket.forkToPublic') }}
                    </button>
                  </div>
                </div>
              </div>
              </template>
              <template v-else-if="viewMode === 'templates'">
              <div
                v-for="tpl in store.templates"
                :key="tpl.id"
                class="flex items-center gap-4 px-5 py-2 hover:bg-muted/30 transition cursor-pointer"
                @click="goToTemplate(tpl.id)"
              >
                <!-- 图标：按 id 稳定散列底色 -->
                <div
                  :class="['w-14 h-14 rounded-2xl flex items-center justify-center shrink-0', iconColorClass(tpl.id)]"
                >
                  <component :is="resolveIcon(tpl.icon)" class="w-7 h-7 text-white" />
                </div>

                <div class="min-w-0 flex-1">
                  <span class="font-semibold truncate">{{ tpl.name }}</span>
                  <p class="text-sm text-muted-foreground line-clamp-1 mt-1">
                    {{ tpl.short_description ?? tpl.description ?? '' }}
                  </p>
                </div>

                <!-- 右侧数据列：技能数 / 使用数 / fork 按钮 -->
                <div class="flex flex-col items-end gap-2 shrink-0" @click.stop>
                  <div class="flex items-center gap-3 text-xs text-muted-foreground tabular-nums">
                    <span class="flex items-center gap-1">
                      <Dna class="w-3.5 h-3.5" />
                      {{ t('template.geneCount', { count: tpl.gene_slugs?.length ?? 0 }) }}
                    </span>
                    <span class="flex items-center gap-1">
                      <Download class="w-3.5 h-3.5" />
                      {{ t('template.useCount', { count: tpl.use_count ?? 0 }) }}
                    </span>
                  </div>

                  <!-- fork 按钮组：根据源 scope + 当前用户权限决定显示哪些目标按钮 -->
                  <div
                    v-if="canForkFromTemplate(tpl).personal || canForkFromTemplate(tpl).org || canForkFromTemplate(tpl).public"
                    class="flex items-center gap-1.5"
                  >
                    <button
                      v-if="canForkFromTemplate(tpl).personal"
                      class="inline-flex items-center gap-1 px-2 py-1 rounded-md border border-border text-xs hover:border-primary/50 hover:text-primary transition-colors disabled:opacity-50"
                      :disabled="forkingTemplateId === forkKey(tpl.id, 'personal')"
                      @click.stop="onForkTemplate(tpl, 'personal')"
                    >
                      <Loader2 v-if="forkingTemplateId === forkKey(tpl.id, 'personal')" class="w-3 h-3 animate-spin" />
                      <Download v-else class="w-3 h-3" />
                      {{ t('template.forkToPersonal') }}
                    </button>
                    <button
                      v-if="canForkFromTemplate(tpl).org"
                      class="inline-flex items-center gap-1 px-2 py-1 rounded-md border border-border text-xs hover:border-primary/50 hover:text-primary transition-colors disabled:opacity-50"
                      :disabled="forkingTemplateId === forkKey(tpl.id, 'org')"
                      @click.stop="onForkTemplate(tpl, 'org')"
                    >
                      <Loader2 v-if="forkingTemplateId === forkKey(tpl.id, 'org')" class="w-3 h-3 animate-spin" />
                      <Download v-else class="w-3 h-3" />
                      {{ t('template.forkToOrg') }}
                    </button>
                    <button
                      v-if="canForkFromTemplate(tpl).public"
                      class="inline-flex items-center gap-1 px-2 py-1 rounded-md border border-border text-xs hover:border-primary/50 hover:text-primary transition-colors disabled:opacity-50"
                      :disabled="forkingTemplateId === forkKey(tpl.id, 'public')"
                      @click.stop="onForkTemplate(tpl, 'public')"
                    >
                      <Loader2 v-if="forkingTemplateId === forkKey(tpl.id, 'public')" class="w-3 h-3 animate-spin" />
                      <Download v-else class="w-3 h-3" />
                      {{ t('template.forkToPublic') }}
                    </button>
                  </div>
                </div>
              </div>
              </template>
            </div>
          </section>

          <!-- ═══ 统计 Tab ═══ -->
          <div v-if="viewMode === 'stats'" class="space-y-6">
            <!-- 汇总卡（左）+ 维度切换（右）：同一行 -->
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div class="flex gap-3">
                <div class="flex items-center gap-2 px-4 py-2 rounded-lg border border-border bg-card">
                  <Download class="w-4 h-4 text-primary" />
                  <div>
                    <p class="text-xs text-muted-foreground">{{ t('geneMarket.statsCardDownload') }}</p>
                    <p class="text-lg font-semibold tabular-nums">{{ marketStats?.totals.download ?? 0 }}</p>
                  </div>
                </div>
                <div class="flex items-center gap-2 px-4 py-2 rounded-lg border border-border bg-card">
                  <TrendingUp class="w-4 h-4 text-primary" />
                  <div>
                    <p class="text-xs text-muted-foreground">{{ t('geneMarket.statsCardUse') }}</p>
                    <p class="text-lg font-semibold tabular-nums">{{ marketStats?.totals.use ?? 0 }}</p>
                  </div>
                </div>
              </div>
              <div class="flex items-center gap-0.5 bg-muted/50 rounded-lg p-0.5">
                <button
                  v-for="dim in statsDimensionTabs"
                  :key="dim.value"
                  :class="[
                    'px-3 py-1.5 rounded-md text-sm transition-colors',
                    statsDimension === dim.value
                      ? 'bg-background text-foreground shadow-sm font-medium'
                      : 'text-muted-foreground hover:text-foreground',
                  ]"
                  @click="statsDimension = dim.value"
                >
                  {{ t(dim.key) }}
                </button>
              </div>
            </div>

            <div v-if="statsLoading" class="flex justify-center py-16">
              <Loader2 class="w-8 h-8 animate-spin text-muted-foreground" />
            </div>

            <template v-else>
              <!-- 两榜并排 -->
              <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
                <div
                  v-for="rank in [
                    { key: 'download', title: t('geneMarket.statsRankDownload'), items: marketStats?.rankings.download },
                    { key: 'use', title: t('geneMarket.statsRankUse'), items: marketStats?.rankings.use },
                  ]"
                  :key="rank.key"
                  class="rounded-xl border border-border bg-card p-4"
                >
                  <h3 class="flex items-center gap-2 text-sm font-semibold mb-3">
                    <component :is="rank.key === 'download' ? Download : TrendingUp" class="w-4 h-4 text-primary" />
                    {{ rank.title }}
                  </h3>
                  <p v-if="!rank.items?.length" class="text-xs text-muted-foreground py-6 text-center">
                    {{ t('geneMarket.statsNoData') }}
                  </p>
                  <div v-else class="space-y-2.5">
                    <div
                      v-for="(item, idx) in rank.items"
                      :key="item.slug"
                      class="flex items-center gap-3 cursor-pointer group"
                      @click="goToGene(item.slug)"
                    >
                      <span
                        :class="[
                          'w-5 h-5 shrink-0 rounded text-xs flex items-center justify-center font-medium',
                          idx < 3 ? 'bg-primary/15 text-primary' : 'bg-muted text-muted-foreground',
                        ]"
                      >
                        {{ idx + 1 }}
                      </span>
                      <div class="min-w-0 flex-1">
                        <p class="text-sm truncate group-hover:text-primary transition-colors">{{ item.name }}</p>
                        <div class="h-1.5 rounded-full bg-muted overflow-hidden mt-1">
                          <div
                            class="h-full rounded-full bg-primary/60 transition-all"
                            :style="{ width: statsBarWidth(item, rank.items!) }"
                          />
                        </div>
                      </div>
                      <span class="shrink-0 text-sm text-muted-foreground tabular-nums">{{ item.count }}</span>
                    </div>
                  </div>
                </div>
              </div>
            </template>
          </div>

          <!-- ═══ 本地上传 Tab ═══ -->
          <div v-if="viewMode === 'local'" class="space-y-6">
            <div class="rounded-xl border-2 border-dashed border-blue-300 bg-blue-50 p-8">
              <div class="flex flex-col items-center gap-4">
                <FolderOpen class="w-10 h-10 text-blue-400" />
                <p class="text-sm text-gray-700 text-center font-medium">上传本地 SKILL 文件夹</p>
                <p class="text-xs text-gray-500 text-center max-w-md">
                  选择包含 <code class="bg-white px-1 rounded">SKILL.md</code> 的文件夹，系统自动解析并创建本地基因。
                  同时支持上传 ZIP 包。
                </p>
                <p class="text-xs text-gray-400 text-center max-w-md">
                  限制：单文件最大 {{ MAX_UPLOAD_FILE_SIZE / (1024 * 1024) }}MB，
                  总大小最大 {{ MAX_UPLOAD_TOTAL_SIZE / (1024 * 1024) }}MB，
                  最多 {{ MAX_UPLOAD_FILE_COUNT }} 个文件。
                </p>

                <!-- 分类选择（必选）：上传后技能市场的分类筛选/展示依赖该字段 -->
                <div class="flex items-center gap-2">
                  <span class="text-xs text-gray-600 shrink-0">{{ t('geneMarket.categoryLabel') }}</span>
                  <CustomSelect v-model="uploadCategory" :options="uploadCategoryOptions" />
                  <button
                    v-if="isAdmin"
                    class="inline-flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 shrink-0"
                    :title="t('geneMarket.manageCategories')"
                    @click="openCategoryDialog"
                  >
                    <Settings class="w-3.5 h-3.5" />
                    {{ t('geneMarket.manageCategories') }}
                  </button>
                </div>
                <p v-if="!uploadCategory" class="text-xs text-gray-400">
                  {{ t('geneMarket.categoryRequired') }}
                </p>

                <input
                  ref="localFolderInputRef"
                  type="file"
                  class="hidden"
                  webkitdirectory
                  multiple
                  @change="onLocalFolderInput"
                />
                <button
                  class="inline-flex items-center gap-2 rounded-lg border border-blue-300 bg-white px-4 py-2 text-sm text-blue-700 hover:bg-blue-50"
                  @click="localFolderInputRef?.click()"
                >
                  <FolderOpen class="w-4 h-4" />
                  选择文件夹
                </button>
              </div>

              <div v-if="selectedLocalFiles.length > 0" class="mt-4">
                <p class="text-xs font-medium text-gray-600 mb-2">
                  已选 {{ selectedLocalFiles.length }} 个文件：
                </p>
                <ul class="max-h-40 overflow-y-auto rounded-lg bg-white border border-gray-200 divide-y divide-gray-100">
                  <li
                    v-for="path in selectedLocalFiles"
                    :key="path"
                    class="flex items-center gap-2 px-3 py-1.5 text-xs"
                  >
                    <Code2 v-if="path.endsWith('.py')" class="w-3 h-3 text-blue-500 shrink-0" />
                    <FolderOpen v-else-if="path.includes('/')" class="w-3 h-3 text-gray-400 shrink-0" />
                    <span class="text-gray-600">{{ path }}</span>
                  </li>
                </ul>

                <!-- 直接上传只能进入个人库；组织库/公共市场内容需先落地个人库，再通过技能详情页的 Fork 功能同步过去 -->
                <div class="mt-3 rounded-lg bg-white border border-gray-200 p-3">
                  <p class="text-xs text-gray-600">
                    <span class="font-medium text-gray-700">上传到个人技能 library</span>
                    <span class="text-gray-500"> — 仅自己可见，立即可用。需要同步到组织库/公共市场，请上传后通过技能详情页的 Fork 功能操作。</span>
                  </p>
                </div>

                <div class="mt-3 flex justify-end">
                  <button
                    :disabled="localUploading"
                    class="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
                    @click="handleLocalFolder"
                  >
                    <span v-if="localUploading" class="animate-spin inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full" />
                    <Upload v-else class="w-4 h-4" />
                    {{ localUploading ? '上传中...' : '确认上传' }}
                  </button>
                </div>
              </div>

              <div class="mt-4 flex flex-col items-center gap-2">
                <p class="text-xs text-gray-400">或上传 ZIP 包</p>
                <input
                  ref="localFileInputRef"
                  type="file"
                  accept=".zip"
                  class="hidden"
                  @change="(e: Event) => { const f = (e.target as HTMLInputElement).files?.[0]; if (f) handleLocalFile(f) }"
                />
                <button
                  class="inline-flex items-center gap-1.5 text-xs text-gray-500 underline underline-offset-2 hover:text-gray-700"
                  @click="localFileInputRef?.click()"
                >
                  点击选择 .zip 文件
                </button>
              </div>

              <div v-if="localUploading" class="mt-4 flex items-center gap-2 text-sm text-blue-600 justify-center">
                <span class="animate-spin inline-block w-4 h-4 border-2 border-blue-400 border-t-transparent rounded-full" />
                解析上传中...
              </div>
              <div v-if="localError" class="mt-4 flex items-start gap-2 rounded-lg bg-red-50 border border-red-200 px-4 py-3">
                <AlertTriangle class="w-4 h-4 text-red-500 shrink-0" />
                <p class="text-sm text-red-700">{{ localError }}</p>
              </div>
              <div v-if="localSuccess" class="mt-4 flex items-center gap-2 rounded-lg bg-green-50 border border-green-200 px-4 py-3">
                <Check class="w-4 h-4 text-green-500" />
                <p class="text-sm text-green-700">{{ localSuccess }}</p>
              </div>
            </div>
          </div>

          <div
            v-if="totalPages > 1"
            class="flex items-center justify-center gap-2 mt-8"
          >
            <button
              :disabled="!canPrev"
              :class="[
                'px-3 py-1.5 rounded-lg text-sm transition-colors',
                canPrev
                  ? 'text-foreground hover:bg-muted'
                  : 'text-muted-foreground cursor-not-allowed',
              ]"
              @click="page = Math.max(1, page - 1)"
            >
              {{ t('geneMarket.prevPage') }}
            </button>
            <span class="text-sm text-muted-foreground">
              {{ page }} / {{ totalPages }}
            </span>
            <button
              :disabled="!canNext"
              :class="[
                'px-3 py-1.5 rounded-lg text-sm transition-colors',
                canNext
                  ? 'text-foreground hover:bg-muted'
                  : 'text-muted-foreground cursor-not-allowed',
              ]"
              @click="page = Math.min(totalPages, page + 1)"
            >
              {{ t('geneMarket.nextPage') }}
            </button>
          </div>
        </template>

        <!-- ═══ 分类管理弹窗（仅管理员） ═══ -->
        <div
          v-if="showCategoryDialog"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          @click.self="showCategoryDialog = false"
        >
          <div class="w-full max-w-md rounded-xl border border-border bg-card p-5 shadow-lg">
            <h3 class="flex items-center gap-2 text-sm font-semibold mb-1">
              <Settings class="w-4 h-4 text-primary" />
              {{ t('geneMarket.manageCategories') }}
            </h3>
            <p class="text-xs text-muted-foreground mb-4">{{ t('geneMarket.manageCategoriesHint') }}</p>

            <div class="flex flex-wrap gap-2 mb-4 min-h-8">
              <span
                v-for="(name, idx) in categoryDraft"
                :key="name"
                class="inline-flex items-center gap-1 px-2 py-1 rounded-md bg-muted/70 text-sm"
              >
                {{ name }}
                <button
                  class="text-muted-foreground hover:text-destructive"
                  :title="t('geneMarket.removeCategory')"
                  @click="categoryDraft.splice(idx, 1)"
                >
                  <X class="w-3 h-3" />
                </button>
              </span>
              <span v-if="!categoryDraft.length" class="text-xs text-muted-foreground self-center">
                {{ t('geneMarket.noCategories') }}
              </span>
            </div>

            <div class="flex gap-2 mb-4">
              <input
                v-model="newCategoryInput"
                type="text"
                maxlength="32"
                class="flex-1 px-3 py-1.5 rounded-lg border border-border bg-background text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
                :placeholder="t('geneMarket.newCategoryPlaceholder')"
                @keyup.enter="addDraftCategory"
              />
              <button
                class="px-3 py-1.5 rounded-lg border border-border text-sm hover:border-primary/50 hover:text-primary transition-colors"
                @click="addDraftCategory"
              >
                {{ t('geneMarket.addCategory') }}
              </button>
            </div>

            <div class="flex justify-end gap-2">
              <button
                class="px-4 py-1.5 rounded-lg text-sm text-muted-foreground hover:text-foreground"
                @click="showCategoryDialog = false"
              >
                {{ t('common.cancel') }}
              </button>
              <button
                class="px-4 py-1.5 rounded-lg bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50"
                :disabled="savingCategories"
                @click="saveCategories"
              >
                <Loader2 v-if="savingCategories" class="w-4 h-4 animate-spin inline mr-1" />
                {{ t('common.save') }}
              </button>
            </div>
          </div>
        </div>
    </div>
  </div>
</template>