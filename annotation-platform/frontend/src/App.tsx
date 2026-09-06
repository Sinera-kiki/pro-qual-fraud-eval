import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import {
  Database, Tag, Upload, Download, Trash2, ChevronLeft, ChevronRight,
  X, CheckCircle, AlertTriangle, Info, Loader2, FolderOpen, ArrowLeft,
  RotateCw, RotateCcw, ZoomIn, ZoomOut, Maximize2,
} from 'lucide-react'

// ─── Types ────────────────────────────────────────────────────────────────────

interface Dataset {
  id: string
  name: string
  filename: string
  total_rows: number
  cluster_count: number
  created_at: string
  channel?: 'evaluation' | 'review'
}

type Channel = 'evaluation' | 'review'

interface Whoami {
  user: { name?: string; email?: string; displayName?: string }
}

interface MyTask {
  current_cluster_id: number | null
  total: number
  done: number
  my_done: number
}

type Verdict = string  // v1.5: 不违规 | 实锤造假·实拍图片p图 | …
type ImageReviewMap = Record<string, { verdict: Verdict }>

interface ClusterInfo {
  cluster_id: number
  count: number
  trade_first_name: string
  trade_second_name: string
  remark_first?: string
  remark_second?: string
}

interface ClusterItem {
  user_id: string
  qualification_url: string
  trade_first_name: string
  trade_second_name: string
  cluster_id: number
}

interface AnnotationProgress {
  total_clusters: number
  annotated_clusters: number
  pending_clusters: number
}

// ─── Label taxonomy ───────────────────────────────────────────────────────────

const LABEL_TAXONOMY: Record<string, string[]> = {
  '实锤造假': [
    '二维码完全一致（仅针对营业执照）',
    '实拍图片p图且ai更换背景',
    '实拍图片p图',
    '手机截屏p图',
    '公章样式变化',
    '部分二维码缺失',
  ],
  '疑似造假': ['资质模糊', '资质批量拍摄'],
  '资质挂靠': ['资质挂靠'],
  '不违规': ['不违规'],
}

const FIRST_LEVEL_LABELS = Object.keys(LABEL_TAXONOMY)

// v1.5：审核图片级判定叶字符串列表（不违规 + 所有违规细分）
interface VerdictOption { value: string; firstLevel: string }
const IMAGE_VERDICT_OPTIONS: VerdictOption[] = [
  { value: '不违规', firstLevel: '不违规' },
  ...Object.entries(LABEL_TAXONOMY).flatMap(([first, seconds]) =>
    first === '不违规' ? [] : seconds.map(s => ({ value: `${first}·${s}`, firstLevel: first }))
  ),
]

const LABEL_COLORS: Record<string, string> = {
  '实锤造假': 'bg-red-100 text-red-700 border-red-200',
  '疑似造假': 'bg-amber-100 text-amber-700 border-amber-200',
  '资质挂靠': 'bg-purple-100 text-purple-700 border-purple-200',
  '不违规': 'bg-green-100 text-green-700 border-green-200',
}

// ─── API helpers ──────────────────────────────────────────────────────────────

const api = {
  get: (url: string) => fetch(url).then(r => { if (!r.ok) throw new Error(r.statusText); return r.json() }),
  post: (url: string, body: unknown) => fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(r => { if (!r.ok) throw new Error(r.statusText); return r.json() }),
  delete: (url: string) => fetch(url, { method: 'DELETE' }).then(r => { if (!r.ok) throw new Error(r.statusText); return r.json() }),
  upload: (url: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return fetch(url, { method: 'POST', body: fd }).then(async r => {
      if (!r.ok) {
        const err = await r.json().catch(() => ({ detail: r.statusText }))
        throw new Error(err.detail || r.statusText)
      }
      return r.json()
    })
  },
}

// ─── Toast ────────────────────────────────────────────────────────────────────

interface Toast { id: number; msg: string; type: 'success' | 'error' | 'info' }
let toastId = 0

function ToastContainer({ toasts, onRemove }: { toasts: Toast[]; onRemove: (id: number) => void }) {
  return (
    <div className="fixed bottom-6 right-6 z-50 flex flex-col gap-2">
      {toasts.map(t => (
        <div
          key={t.id}
          className={`flex items-center gap-3 px-4 py-3 rounded-xl shadow-lg text-sm font-medium max-w-sm
            ${t.type === 'success' ? 'bg-green-50 text-green-800 border border-green-200' :
              t.type === 'error' ? 'bg-red-50 text-red-800 border border-red-200' :
                'bg-blue-50 text-blue-800 border border-blue-200'}`}
        >
          {t.type === 'success' && <CheckCircle size={16} className="shrink-0" />}
          {t.type === 'error' && <AlertTriangle size={16} className="shrink-0" />}
          {t.type === 'info' && <Info size={16} className="shrink-0" />}
          <span className="flex-1">{t.msg}</span>
          <button onClick={() => onRemove(t.id)} className="ml-1 opacity-50 hover:opacity-100">
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  )
}

function useToast() {
  const [toasts, setToasts] = useState<Toast[]>([])
  const push = useCallback((msg: string, type: Toast['type'] = 'info') => {
    const id = ++toastId
    setToasts(prev => [...prev, { id, msg, type }])
    setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 4000)
  }, [])
  const remove = useCallback((id: number) => setToasts(prev => prev.filter(t => t.id !== id)), [])
  return { toasts, push, remove }
}

// ─── Lightbox ─────────────────────────────────────────────────────────────────

function Lightbox({ items, index, onClose, onNav }: {
  items: ClusterItem[]
  index: number
  onClose: () => void
  onNav: (delta: number) => void
}) {
  const item = items[index]
  const [rotation, setRotation] = useState(0)
  const [zoom, setZoom] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const dragRef = useRef<{ startX: number; startY: number; baseX: number; baseY: number } | null>(null)

  // 切图时重置
  useEffect(() => { setRotation(0); setZoom(1); setPan({ x: 0, y: 0 }) }, [index])

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
      else if (e.key === 'ArrowLeft') onNav(-1)
      else if (e.key === 'ArrowRight') onNav(1)
      else if (e.key === 'r' || e.key === 'R') setRotation(r => (r + 90) % 360)
      else if (e.key === '+' || e.key === '=') setZoom(z => Math.min(z * 1.25, 6))
      else if (e.key === '-' || e.key === '_') setZoom(z => Math.max(z / 1.25, 0.5))
      else if (e.key === '0') { setZoom(1); setPan({ x: 0, y: 0 }); setRotation(0) }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose, onNav])

  if (!item) return null

  return (
    <div className="fixed inset-0 z-50 bg-black/90 flex items-center justify-center" onClick={onClose}>
      {/* 右上角控制栏 */}
      <div className="absolute top-4 right-4 flex items-center gap-2 z-10" onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-1 bg-white/10 backdrop-blur-md rounded-full px-3 py-1.5">
          {/* 缩放 */}
          <button onClick={() => setZoom(z => Math.max(z / 1.25, 0.5))} title="缩小 (-)"
            className="p-1.5 text-white/80 hover:text-white hover:bg-white/10 rounded-full">
            <ZoomOut size={16} />
          </button>
          <span className="text-white/80 text-xs w-12 text-center tabular-nums select-none">{Math.round(zoom * 100)}%</span>
          <button onClick={() => setZoom(z => Math.min(z * 1.25, 6))} title="放大 (+)"
            className="p-1.5 text-white/80 hover:text-white hover:bg-white/10 rounded-full">
            <ZoomIn size={16} />
          </button>
          <div className="w-px h-4 bg-white/20 mx-1" />
          {/* 旋转 */}
          <button onClick={() => setRotation(r => (r - 90 + 360) % 360)} title="逆时针旋转"
            className="p-1.5 text-white/80 hover:text-white hover:bg-white/10 rounded-full">
            <RotateCcw size={16} />
          </button>
          <button onClick={() => setRotation(r => (r + 90) % 360)} title="顺时针旋转 (R)"
            className="p-1.5 text-white/80 hover:text-white hover:bg-white/10 rounded-full">
            <RotateCw size={16} />
          </button>
          <div className="w-px h-4 bg-white/20 mx-1" />
          {/* 重置 */}
          <button onClick={() => { setZoom(1); setPan({ x: 0, y: 0 }); setRotation(0) }} title="重置 (0)"
            className="p-1.5 text-white/80 hover:text-white hover:bg-white/10 rounded-full">
            <Maximize2 size={16} />
          </button>
        </div>
        <button onClick={onClose} title="关闭 (Esc)"
          className="p-2 text-white/70 hover:text-white bg-white/10 hover:bg-white/20 rounded-full">
          <X size={20} />
        </button>
      </div>

      {/* 左右切图 */}
      <button
        onClick={e => { e.stopPropagation(); onNav(-1) }}
        disabled={index === 0}
        className="absolute left-4 text-white/70 hover:text-white disabled:opacity-20 z-10"
      >
        <ChevronLeft size={40} />
      </button>
      <button
        onClick={e => { e.stopPropagation(); onNav(1) }}
        disabled={index === items.length - 1}
        className="absolute right-4 text-white/70 hover:text-white disabled:opacity-20 z-10"
      >
        <ChevronRight size={40} />
      </button>

      {/* 中间：图片 + 图下信息 */}
      <div
        onClick={e => e.stopPropagation()}
        className="max-w-[90vw] max-h-[85vh] flex flex-col items-center gap-3 select-none"
      >
        <img
          src={item.qualification_url}
          alt=""
          draggable={false}
          onMouseDown={e => { if (zoom > 1) { dragRef.current = { startX: e.clientX, startY: e.clientY, baseX: pan.x, baseY: pan.y } } }}
          onMouseMove={e => { if (dragRef.current) { setPan({ x: dragRef.current.baseX + (e.clientX - dragRef.current.startX), y: dragRef.current.baseY + (e.clientY - dragRef.current.startY) }) } }}
          onMouseUp={() => { dragRef.current = null }}
          onMouseLeave={() => { dragRef.current = null }}
          onWheel={e => { if (e.ctrlKey || e.metaKey) return; e.preventDefault(); const delta = e.deltaY < 0 ? 1.1 : 1/1.1; setZoom(z => Math.min(Math.max(z * delta, 0.5), 6)) }}
          className="max-h-[75vh] max-w-[85vw] object-contain rounded-lg transition-transform duration-75"
          style={{
            transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom}) rotate(${rotation}deg)`,
            cursor: zoom > 1 ? (dragRef.current ? 'grabbing' : 'grab') : 'default',
          }}
        />
        <div className="text-white/70 text-xs">
          {index + 1} / {items.length} &nbsp;·&nbsp; UID: {item.user_id} &nbsp;·&nbsp;
          <span className="opacity-60">快捷键：←→ 切图 / R 旋转 / +– 缩放 / 0 重置 / Esc 关闭</span>
        </div>
      </div>
    </div>
  )
}
// --- ClusterList with floating active item ---

function ClusterItemBtn({ c, ann, isActive, onClick, innerRef, seq }: {
  c: ClusterInfo
  ann?: { remark_first: string; remark_second: string }
  isActive: boolean
  onClick: () => void
  innerRef?: React.Ref<HTMLButtonElement>
  seq: number
}) {
  return (
    <button
      ref={innerRef}
      onClick={onClick}
      className={`text-left w-full px-3 py-2.5 rounded-xl transition-all ${
        isActive ? 'bg-gray-900 text-white' : 'hover:bg-gray-100 text-gray-700'
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-xs font-bold">第 {seq} 个</span>
        <span className={`text-xs ${isActive ? 'text-gray-300' : 'text-gray-400'}`}>{c.count} 张</span>
      </div>
      <div className="text-xs truncate mt-0.5 text-gray-400">
        {c.trade_first_name}{c.trade_second_name ? ` · ${c.trade_second_name}` : ''}
      </div>
      {ann && (
        <div className="mt-1">
          <span className={`text-[10px] px-1.5 py-0.5 rounded-full border ${
            isActive ? 'bg-white/20 text-white border-white/20' : LABEL_COLORS[ann.remark_first] ?? 'bg-gray-100 text-gray-600 border-gray-200'
          }`}>
            {ann.remark_first}
          </span>
        </div>
      )}
    </button>
  )
}

function ClusterList({ clusters, annotations, activeCluster, loadingClusters, onSelect, uidMatchedIds, firstFilter, secondFilter, tradeQuery }: {
  clusters: ClusterInfo[]
  annotations: Record<string, { remark_first: string; remark_second: string }>
  activeCluster: ClusterInfo | null
  loadingClusters: boolean
  onSelect: (c: ClusterInfo) => void
  uidMatchedIds: Set<number> | null          // null = 未启用搜索；Set = 命中的 cluster_id 集合（高亮，不过滤）
  firstFilter: string                         // '' = 全部；'未打标' 特殊；其它=一级标签
  secondFilter: string                        // '' = 全部；其它=二级标签
  tradeQuery: string                           // 行业搜索关键字
}) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const activeItemRef = useRef<HTMLButtonElement>(null)
  const [floatPos, setFloatPos] = useState<'top' | 'bottom' | null>(null)

  useEffect(() => {
    const container = scrollRef.current
    if (!container || !activeCluster) { setFloatPos(null); return }

    function check() {
      const item = activeItemRef.current
      if (!container || !item) return
      const cr = container.getBoundingClientRect()
      const ir = item.getBoundingClientRect()
      if (ir.bottom <= cr.top) {
        setFloatPos('top')
      } else if (ir.top >= cr.bottom) {
        setFloatPos('bottom')
      } else {
        setFloatPos(null)
      }
    }

    check()
    container.addEventListener('scroll', check, { passive: true })
    return () => container.removeEventListener('scroll', check)
  }, [activeCluster, clusters])

  function scrollToActive() {
    activeItemRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  const active = activeCluster ? clusters.find(c => c.cluster_id === activeCluster.cluster_id) ?? null : null
  const activeAnn = active ? annotations[String(active.cluster_id)] : undefined

  return (
    <div className="relative flex-1 min-h-0 flex flex-col">
      {floatPos && active && (
        <div
          className={`absolute left-0 right-1 z-10 ${floatPos === 'top' ? 'top-0' : 'bottom-0'}`}
          style={{ boxShadow: floatPos === 'top' ? '0 4px 12px rgba(0,0,0,0.12)' : '0 -4px 12px rgba(0,0,0,0.12)' }}
        >
          <ClusterItemBtn
            c={active}
            ann={activeAnn}
            isActive
            onClick={scrollToActive}
            seq={(clusters.findIndex(c => c.cluster_id === active.cluster_id)) + 1}
          />
        </div>
      )}
      <div ref={scrollRef} className="flex flex-col gap-1 overflow-y-auto pr-1 flex-1 min-h-0">
        {loadingClusters ? (
          <div className="flex justify-center py-8"><Loader2 size={24} className="animate-spin text-gray-300" /></div>
        ) : (() => {
          const filtered = clusters
            .map((c, i) => ({ c, seq: i + 1 }))  // seq 用原始位置，过滤不重排
            .filter(({ c }) => {
              if (uidMatchedIds && !uidMatchedIds.has(c.cluster_id)) return false
              const ann = annotations[String(c.cluster_id)]
              if (firstFilter === '未打标') { if (ann) return false }
              else if (firstFilter && ann?.remark_first !== firstFilter) return false
              if (secondFilter && ann?.remark_second !== secondFilter) return false
              // 行业搜索：匹配一级或二级行业
              const tq = tradeQuery.trim().toLowerCase()
              if (tq) {
                const t1 = (c.trade_first_name || '').toLowerCase()
                const t2 = (c.trade_second_name || '').toLowerCase()
                if (!t1.includes(tq) && !t2.includes(tq)) return false
              }
              return true
            })
          if (filtered.length === 0) {
            return <div className="text-xs text-gray-400 text-center py-8">没有符合条件的簇</div>
          }
          return filtered.map(({ c, seq }) => {
            const ann = annotations[String(c.cluster_id)]
            const isActive = activeCluster?.cluster_id === c.cluster_id
            return (
              <ClusterItemBtn
                key={c.cluster_id}
                c={c}
                ann={ann}
                isActive={isActive}
                onClick={() => onSelect(c)}
                innerRef={isActive ? activeItemRef : undefined}
                seq={seq}
              />
            )
          })
        })()}
      </div>
    </div>
  )
}

// ─── Review Image Panel: 审核通道 v1.4 ─ 按账户分组卡片 + 每张图二元判定 ───────

function ReviewImagePanel({
  datasetId, cluster, onAllReviewed, onProgress,
}: {
  datasetId: string
  cluster: ClusterInfo
  onAllReviewed: () => void
  onProgress?: (reviewed: number, total: number) => void
}) {
  const [items, setItems] = useState<ClusterItem[]>([])
  const [reviews, setReviews] = useState<ImageReviewMap>({})
  const [loading, setLoading] = useState(false)
  const [savingUrl, setSavingUrl] = useState<string | null>(null)
  const [pendingFirstByUrl, setPendingFirstByUrl] = useState<Record<string, string>>({})
  const [lightboxIdx, setLightboxIdx] = useState<number | null>(null)

  useEffect(() => {
    setItems([])
    setReviews({})
    setPendingFirstByUrl({})
    loadAll()
  }, [datasetId, cluster.cluster_id])

  async function loadAll() {
    setLoading(true)
    try {
      const res = await api.get(`/api/datasets/${datasetId}/clusters/${cluster.cluster_id}/items?page=1&page_size=2000`)
      setItems(res.items)
      const rvRes = await api.get(`/api/datasets/${datasetId}/clusters/${cluster.cluster_id}/image_reviews`)
      setReviews(rvRes.reviews as ImageReviewMap)
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(false)
    }
  }

  const reviewedCount = Object.keys(reviews).length
  const totalCount = items.length

  useEffect(() => {
    onProgress?.(reviewedCount, totalCount)
    if (totalCount > 0 && reviewedCount >= totalCount) onAllReviewed()
    // eslint-disable-next-line
  }, [reviewedCount, totalCount])

  async function saveOne(url: string, verdict: Verdict) {
    setSavingUrl(url)
    try {
      await api.post(`/api/datasets/${datasetId}/image_reviews/upsert`, {
        cluster_id: cluster.cluster_id,
        items: [{ qualification_url: url, verdict }],
      })
      setReviews(prev => ({ ...prev, [url]: { verdict } }))
    } catch (e: any) {
      alert('保存失败：' + (e?.message || '未知错误'))
    } finally {
      setSavingUrl(null)
    }
  }

  if (loading) {
    return <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-300" /></div>
  }

  return (
    <div className="bg-white border border-gray-200 rounded-2xl p-5">
      <div className="flex items-center justify-between mb-3 sticky top-0 bg-white pb-2 z-10">
        <h4 className="font-semibold text-gray-700 text-sm">全部图片（{cluster.count} 张）</h4>
        <span className="text-xs text-gray-400">已判定 {reviewedCount} / {totalCount}</span>
      </div>

      <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 xl:grid-cols-8 gap-2">
        {items.map((it, idx) => {
          const rv = reviews[it.qualification_url]?.verdict
          const isSaving = savingUrl === it.qualification_url
          const [rvFirst, rvSecond] = rv
            ? (rv === '不违规' ? ['不违规', ''] : rv.split('·'))
            : ['', '']
          const shownFirst = rvFirst || pendingFirstByUrl[it.qualification_url] || ''
          const secondOpts = shownFirst && shownFirst !== '不违规'
            ? (LABEL_TAXONOMY[shownFirst] ?? [])
            : []

          const handleFirstChange = (first: string) => {
            if (!first) return
            if (first === '不违规') {
              saveOne(it.qualification_url, '不违规')
              setPendingFirstByUrl(prev => { const n = { ...prev }; delete n[it.qualification_url]; return n })
            } else {
              setPendingFirstByUrl(prev => ({ ...prev, [it.qualification_url]: first }))
            }
          }

          const handleSecondChange = (second: string) => {
            if (!shownFirst || shownFirst === '不违规' || !second) return
            saveOne(it.qualification_url, `${shownFirst}·${second}`)
            setPendingFirstByUrl(prev => { const n = { ...prev }; delete n[it.qualification_url]; return n })
          }

          return (
            <div key={idx} className={`relative rounded-lg overflow-hidden border-2 ${
              rv === '不违规' ? 'border-green-400 ring-2 ring-green-200' :
              rv ? 'border-red-400 ring-2 ring-red-200' :
              'border-gray-200 hover:border-red-300'
            }`}>
              <div className="aspect-square cursor-zoom-in" onClick={() => setLightboxIdx(idx)}>
                <img src={it.qualification_url} alt="" className="w-full h-full object-cover" loading="lazy" />
              </div>
              <div className="grid grid-cols-2 gap-0 border-t border-gray-200 divide-x divide-gray-200">
                <select
                  value={shownFirst}
                  onChange={e => handleFirstChange(e.target.value)}
                  disabled={isSaving}
                  title={shownFirst || '一级'}
                  className={`text-[11px] py-1 focus:outline-none ${
                    rv === '不违规' ? 'bg-green-500 text-white' :
                    rv ? 'bg-red-500 text-white' :
                    shownFirst ? 'bg-red-50 text-red-700' :
                    'bg-white text-gray-500'
                  }`}
                >
                  <option value="" disabled>一级…</option>
                  <option value="不违规">不违规</option>
                  {FIRST_LEVEL_LABELS.filter(l => l !== '不违规').map(l => (
                    <option key={l} value={l}>{l}</option>
                  ))}
                </select>
                <select
                  value={rvSecond}
                  onChange={e => handleSecondChange(e.target.value)}
                  disabled={isSaving || !shownFirst || shownFirst === '不违规'}
                  title={rvSecond || (shownFirst === '不违规' ? '不需选' : '二级')}
                  className={`text-[11px] py-1 focus:outline-none ${
                    rv && rv !== '不违规' ? 'bg-red-500 text-white' :
                    shownFirst === '不违规' ? 'bg-green-50 text-green-700' :
                    shownFirst ? 'bg-white text-gray-500' :
                    'bg-gray-50 text-gray-300'
                  }`}
                >
                  <option value="" disabled>{shownFirst === '不违规' ? '—' : '二级…'}</option>
                  {secondOpts.map(l => <option key={l} value={l}>{l}</option>)}
                </select>
              </div>
            </div>
          )
        })}
      </div>

      {lightboxIdx !== null && (
        <Lightbox
          items={items}
          index={lightboxIdx}
          onClose={() => setLightboxIdx(null)}
          onNav={d => setLightboxIdx(i => i === null ? 0 : Math.max(0, Math.min(items.length - 1, i + d)))}
        />
      )}
    </div>
  )
}

// ─── Image grid (lazy load) ───────────────────────────────────────────────────

function ImageGrid({ datasetId, cluster, uidQuery }: { datasetId: string; cluster: ClusterInfo; uidQuery: string }) {
  const [items, setItems] = useState<ClusterItem[]>([])
  const [total, setTotal] = useState(cluster.count)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [lightboxIdx, setLightboxIdx] = useState<number | null>(null)
  // 图片粒度剔除标记 set
  const [labeledUrls, setLabeledUrls] = useState<Set<string>>(new Set())
  const [togglingUrl, setTogglingUrl] = useState<string | null>(null)
  const PAGE_SIZE = 30

  useEffect(() => {
    setItems([])
    setPage(1)
    setTotal(cluster.count)
    setLabeledUrls(new Set())
    loadPage(1, true)
    loadItemLabels()
  }, [datasetId, cluster.cluster_id])

  async function loadItemLabels() {
    try {
      const res = await api.get(`/api/datasets/${datasetId}/clusters/${cluster.cluster_id}/item_labels`)
      setLabeledUrls(new Set(res.labeled as string[]))
    } catch {
      // ignore
    }
  }

  async function toggleLabel(url: string) {
    if (togglingUrl) return
    setTogglingUrl(url)
    try {
      const res = await api.post(`/api/datasets/${datasetId}/item_labels/toggle`, { qualification_url: url })
      setLabeledUrls(prev => {
        const next = new Set(prev)
        if (res.labeled) next.add(url)
        else next.delete(url)
        return next
      })
    } catch {
      // ignore
    } finally {
      setTogglingUrl(null)
    }
  }

  async function loadPage(p: number, reset = false) {
    setLoading(true)
    try {
      const res = await api.get(`/api/datasets/${datasetId}/clusters/${cluster.cluster_id}/items?page=${p}&page_size=${PAGE_SIZE}`)
      setTotal(res.total)
      setItems(prev => reset ? res.items : [...prev, ...res.items])
      setPage(p)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 xl:grid-cols-8 gap-2">
        {items.map((item, i) => {
          const isLabeled = labeledUrls.has(item.qualification_url)
          const isToggling = togglingUrl === item.qualification_url
          const uidQ = uidQuery.trim()
          const isUidMatch = uidQ.length >= 2 && item.user_id.toLowerCase().includes(uidQ.toLowerCase())
          return (
            <div
              key={i}
              className={`relative rounded-lg overflow-hidden border-2 transition-all group ${
                isUidMatch
                  ? 'border-red-500 ring-2 ring-red-300'
                  : isLabeled
                    ? 'border-green-400 ring-1 ring-green-300'
                    : 'border-gray-200 hover:border-red-400 hover:shadow-md'
              }`}
            >
              {/* 图片本体 — 点击打开 lightbox */}
              <div
                className="aspect-square cursor-pointer"
                onClick={() => setLightboxIdx(i)}
              >
                {isLabeled && (
                  <div className="absolute inset-0 bg-green-400/20 z-10 pointer-events-none" />
                )}
                {isUidMatch && (
                  <div className="absolute top-0 left-0 right-0 bg-red-500 text-white text-[9px] font-bold py-0.5 px-1 z-20 pointer-events-none truncate">
                    🔍 {item.user_id}
                  </div>
                )}
                <img
                  src={item.qualification_url}
                  alt=""
                  className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-200"
                  loading="lazy"
                />
              </div>
              {/* 不违规按钮 — 浮层在图片底部 */}
              <button
                onClick={e => { e.stopPropagation(); toggleLabel(item.qualification_url) }}
                disabled={isToggling}
                title={isLabeled ? '取消剔除' : '标记为不违规（剔除）'}
                className={`absolute bottom-0 left-0 right-0 py-0.5 text-[10px] font-medium transition-all z-20 ${
                  isLabeled
                    ? 'bg-green-500 text-white hover:bg-green-600'
                    : 'bg-black/40 text-white/80 opacity-0 group-hover:opacity-100 hover:bg-black/60'
                }`}
              >
                {isToggling ? '...' : isLabeled ? '✓ 通过' : '不违规'}
              </button>
            </div>
          )
        })}
        {items.length === 0 && !loading && (
          <div className="col-span-full text-center py-8 text-gray-400 text-sm">暂无图片</div>
        )}
      </div>

      {loading && (
        <div className="flex justify-center py-4">
          <Loader2 size={20} className="animate-spin text-gray-400" />
        </div>
      )}

      {!loading && items.length < total && (
        <button
          onClick={() => loadPage(page + 1)}
          className="mx-auto px-4 py-2 text-sm text-gray-600 border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors"
        >
          加载更多（已显示 {items.length} / {total}）
        </button>
      )}

      {lightboxIdx !== null && (
        <Lightbox
          items={items}
          index={lightboxIdx}
          onClose={() => setLightboxIdx(null)}
          onNav={delta => setLightboxIdx(i => i === null ? 0 : Math.max(0, Math.min(items.length - 1, i + delta)))}
        />
      )}
    </div>
  )
}

// ─── Dataset Page ─────────────────────────────────────────────────────────────

function DatasetPage({ onAnnotate, channel, whoami }: { onAnnotate: (ds: Dataset) => void; channel: Channel; whoami: Whoami | null }) {
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)
  const [uploadProgress, setUploadProgress] = useState(0) // 0-100
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const { toasts, push, remove } = useToast()

  useEffect(() => { loadDatasets() }, [channel])

  async function loadDatasets() {
    try {
      const data = await api.get(`/api/datasets?channel=${channel}`)
      setDatasets(data)
    } catch {
      push('加载数据集失败', 'error')
    } finally {
      setLoading(false)
    }
  }

  async function handleUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    if (!file.name.endsWith('.csv')) { push('请上传 CSV 文件', 'error'); return }
    setUploading(true)
    setUploadProgress(0)
    try {
      const CHUNK_SIZE = 4 * 1024 * 1024 // 4MB per chunk
      const totalChunks = Math.ceil(file.size / CHUNK_SIZE)

      // Step 1: init session
      const { session_id } = await api.post('/api/datasets/upload/init', { filename: file.name, channel })

      // Step 2: upload chunks sequentially
      for (let i = 0; i < totalChunks; i++) {
        const slice = file.slice(i * CHUNK_SIZE, (i + 1) * CHUNK_SIZE)
        const buf = await slice.arrayBuffer()
        // convert to base64
        const bytes = new Uint8Array(buf)
        let binary = ''
        for (let j = 0; j < bytes.byteLength; j++) binary += String.fromCharCode(bytes[j])
        const data = btoa(binary)
        await api.post('/api/datasets/upload/chunk', { session_id, chunk_index: i, data })
        setUploadProgress(Math.round(((i + 1) / totalChunks) * 90))
      }

      // Step 3: finalize
      setUploadProgress(95)
      const ds = await api.post('/api/datasets/upload/finalize', { session_id })
      setUploadProgress(100)
      push(`上传成功：${ds.name}（${ds.total_rows} 条记录，${ds.cluster_count} 个簇）`, 'success')
      loadDatasets()
    } catch (err: unknown) {
      push(`上传失败：${err instanceof Error ? err.message : '未知错误'}`, 'error')
    } finally {
      setUploading(false)
      setUploadProgress(0)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  async function handleDelete(ds: Dataset) {
    if (!confirm(`确认删除数据集「${ds.name}」？此操作不可撤销，相关标注也会一并删除。`)) return
    setDeletingId(ds.id)
    try {
      await api.delete(`/api/datasets/${ds.id}`)
      push('已删除', 'success')
      loadDatasets()
    } catch {
      push('删除失败', 'error')
    } finally {
      setDeletingId(null)
    }
  }

  async function handleExport(ds: Dataset) {
    try {
      const res = await fetch(`/api/datasets/${ds.id}/export`)
      if (!res.ok) throw new Error(res.statusText)
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${ds.name.replace(/\.csv$/i, '')}_annotated.csv`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (err) {
      push(`导出失败：${err instanceof Error ? err.message : '未知错误'}`, 'error')
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <ToastContainer toasts={toasts} onRemove={remove} />

      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">样本管理</h1>
          <p className="text-sm text-gray-500 mt-1">上传 CSV 文件，管理标注数据集</p>
        </div>
        <div>
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv"
            className="hidden"
            onChange={handleUpload}
          />
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
            className="flex items-center gap-2 px-5 py-2.5 bg-red-500 hover:bg-red-600 text-white rounded-xl font-medium text-sm transition-colors disabled:opacity-50"
          >
            {uploading ? <Loader2 size={16} className="animate-spin" /> : <Upload size={16} />}
            {uploading ? `上传中… ${uploadProgress}%` : '上传 CSV'}
          </button>
          {uploading && (
            <div className="mt-2 w-full bg-gray-200 rounded-full h-1.5">
              <div
                className="bg-red-500 h-1.5 rounded-full transition-all duration-300"
                style={{ width: `${uploadProgress}%` }}
              />
            </div>
          )}
        </div>
      </div>

      {/* CSV format hint */}
      <div className="bg-blue-50 border border-blue-200 rounded-xl p-4 text-sm text-blue-700">
        <strong>CSV 格式要求：</strong>必须包含以下 3 列（其余列可选，没有则留空）：
        <code className="ml-1 font-mono bg-blue-100 px-1 rounded">user_id</code>、
        <code className="font-mono bg-blue-100 px-1 rounded">qualification_url</code>、
        <code className="font-mono bg-blue-100 px-1 rounded">cluster_id</code>；
        可选列：
        <code className="font-mono bg-blue-100 px-1 rounded">trade_first_name</code>、
        <code className="font-mono bg-blue-100 px-1 rounded">trade_second_name</code>
      </div>

      {/* Dataset list */}
      {loading ? (
        <div className="flex justify-center py-16">
          <Loader2 size={32} className="animate-spin text-gray-300" />
        </div>
      ) : datasets.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-24 text-center">
          <FolderOpen size={48} className="text-gray-300 mb-4" />
          <h3 className="text-lg font-semibold text-gray-500">暂无数据集</h3>
          <p className="text-sm text-gray-400 mt-1">点击「上传 CSV」开始导入数据</p>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {datasets.map(ds => (
            <div
              key={ds.id}
              className="bg-white border border-gray-200 rounded-2xl p-5 hover:shadow-md transition-shadow"
            >
              <div className="flex items-start justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <h3 className="font-semibold text-gray-900 truncate">{ds.name}</h3>
                  <div className="flex items-center gap-4 mt-2 text-sm text-gray-500">
                    <span>{ds.cluster_count.toLocaleString()} 个簇</span>
                    <span>·</span>
                    <span>{ds.total_rows.toLocaleString()} 条记录</span>
                    <span>·</span>
                    <span>{new Date(ds.created_at).toLocaleDateString('zh-CN')}</span>
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <button
                    onClick={() => onAnnotate(ds)}
                    className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-900 text-white rounded-lg text-sm font-medium hover:bg-gray-700 transition-colors"
                  >
                    <Tag size={14} />
                    开始标注
                  </button>
                  <button
                    onClick={() => handleExport(ds)}
                    title="导出已标注数据"
                    className="p-2 text-gray-500 hover:text-green-600 hover:bg-green-50 rounded-lg transition-colors"
                  >
                    <Download size={16} />
                  </button>

                  <button
                    onClick={() => handleDelete(ds)}
                    disabled={deletingId === ds.id}
                    title="删除数据集"
                    className="p-2 text-gray-500 hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors disabled:opacity-50"
                  >
                    {deletingId === ds.id ? <Loader2 size={16} className="animate-spin" /> : <Trash2 size={16} />}
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

    </div>
  )
}

// ─── Annotate Page ────────────────────────────────────────────────────────────

function AnnotatePage({ initialDataset, channel, whoami }: { initialDataset?: Dataset; channel: Channel; whoami: Whoami | null }) {
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [selectedDataset, setSelectedDataset] = useState<Dataset | null>(initialDataset ?? null)
  const [clusters, setClusters] = useState<ClusterInfo[]>([])
  const [annotations, setAnnotations] = useState<Record<string, { remark_first: string; remark_second: string }>>({})
  const [progress, setProgress] = useState<AnnotationProgress | null>(null)
  const [activeCluster, setActiveCluster] = useState<ClusterInfo | null>(null)
  const [loadingClusters, setLoadingClusters] = useState(false)
  const [savingId, setSavingId] = useState<number | null>(null)
  const [myTask, setMyTask] = useState<MyTask | null>(null)  // 仅审核通道用
  const [claimingNext, setClaimingNext] = useState(false)
  const [imgReviewProgress, setImgReviewProgress] = useState<{ reviewed: number; total: number } | null>(null)

  // 评估通道：搜索 UID + 一二级标签筛选
  const [uidQuery, setUidQuery] = useState('')
  const [uidMatchedIds, setUidMatchedIds] = useState<Set<number> | null>(null)
  const [uidSearching, setUidSearching] = useState(false)
  const [firstFilter, setFirstFilter] = useState('')
  const [secondFilter, setSecondFilter] = useState('')
  const [tradeQuery, setTradeQuery] = useState('')  // 按一二级行业搜索

  // 计算当前筛选后命中的簇数（与 ClusterList 内部逻辑一致）
  const filteredCount = useMemo(() => {
    return clusters.filter(c => {
      if (uidMatchedIds && !uidMatchedIds.has(c.cluster_id)) return false
      const ann = annotations[String(c.cluster_id)]
      if (firstFilter === '未打标') { if (ann) return false }
      else if (firstFilter && ann?.remark_first !== firstFilter) return false
      if (secondFilter && ann?.remark_second !== secondFilter) return false
      const tq = tradeQuery.trim().toLowerCase()
      if (tq) {
        const t1 = (c.trade_first_name || '').toLowerCase()
        const t2 = (c.trade_second_name || '').toLowerCase()
        if (!t1.includes(tq) && !t2.includes(tq)) return false
      }
      return true
    }).length
  }, [clusters, annotations, uidMatchedIds, firstFilter, secondFilter, tradeQuery])

  // Annotation form state for active cluster
  const [firstLabel, setFirstLabel] = useState('')
  const [secondLabel, setSecondLabel] = useState('')

  const { toasts, push, remove } = useToast()

  // Load datasets on mount
  useEffect(() => {
    api.get(`/api/datasets?channel=${channel}`).then(setDatasets).catch(() => {})
    // eslint-disable-next-line
  }, [])

  // Load clusters when dataset selected
  useEffect(() => {
    if (!selectedDataset) return
    setLoadingClusters(true)
    setClusters([])
    setActiveCluster(null)
    setMyTask(null)
    setImgReviewProgress(null)
    Promise.all([
      api.get(`/api/datasets/${selectedDataset.id}/clusters`),
      api.get(`/api/datasets/${selectedDataset.id}/cluster_annotations`),
      api.get(`/api/datasets/${selectedDataset.id}/annotation_progress`),
    ]).then(async ([cls, ann, prog]) => {
      setClusters(cls)
      setAnnotations(ann)
      setProgress(prog)
      // 审核通道：进入后自动 my_task；若无当前任务再 claim_next
      if (channel === 'review') {
        try {
          const mt: MyTask = await api.get(`/api/review/datasets/${selectedDataset.id}/my_task`)
          if (mt.current_cluster_id == null) {
            const claim = await api.post(`/api/review/datasets/${selectedDataset.id}/claim_next`, {})
            const cid = claim.cluster_id as number | null
            setMyTask({ ...mt, current_cluster_id: cid })
            const target = cls.find((c: ClusterInfo) => c.cluster_id === cid)
            if (target) setActiveCluster(target)
          } else {
            setMyTask(mt)
            const target = cls.find((c: ClusterInfo) => c.cluster_id === mt.current_cluster_id)
            if (target) setActiveCluster(target)
          }
        } catch { push('抢单失败', 'error') }
      }
    }).catch(() => push('加载簇数据失败', 'error'))
      .finally(() => setLoadingClusters(false))
  }, [selectedDataset])

  // Sync form when active cluster changes
  useEffect(() => {
    if (!activeCluster) return
    setImgReviewProgress(null)
    const ann = annotations[String(activeCluster.cluster_id)]
    setFirstLabel(ann?.remark_first ?? '')
    setSecondLabel(ann?.remark_second ?? '')
  }, [activeCluster])

  // Debounced UID search（仅评估通道）
  useEffect(() => {
    if (channel !== 'evaluation' || !selectedDataset) return
    const q = uidQuery.trim()
    if (q.length < 2) { setUidMatchedIds(null); return }
    setUidSearching(true)
    const t = setTimeout(async () => {
      try {
        const res = await api.get(`/api/datasets/${selectedDataset.id}/search_uid?q=${encodeURIComponent(q)}`)
        setUidMatchedIds(new Set<number>(res.cluster_ids as number[]))
      } catch { setUidMatchedIds(new Set()) }
      finally { setUidSearching(false) }
    }, 300)
    return () => clearTimeout(t)
  }, [uidQuery, channel, selectedDataset])

  // 切换数据集时重置筛选
  useEffect(() => { setUidQuery(''); setUidMatchedIds(null); setFirstFilter(''); setSecondFilter(''); setTradeQuery('') }, [selectedDataset?.id])

  // When first label changes, reset second
  const handleFirstLabelChange = (val: string) => {
    setFirstLabel(val)
    // 不违规时自动填充二级标签
    setSecondLabel(val === '不违规' ? '不违规' : '')
  }

  const secondOptions = firstLabel ? LABEL_TAXONOMY[firstLabel] ?? [] : []
  const isNoViolation = firstLabel === '不违规'

  async function saveAnnotation() {
    if (!selectedDataset || !activeCluster) return
    if (!firstLabel || !secondLabel) { push('请选择一级和二级标签', 'error'); return }

    setSavingId(activeCluster.cluster_id)
    try {
      await api.post(`/api/datasets/${selectedDataset.id}/cluster_annotations`, {
        cluster_id: activeCluster.cluster_id,
        remark_first: firstLabel,
        remark_second: secondLabel,
      })
      const key = String(activeCluster.cluster_id)
      const isNew = !annotations[key]
      setAnnotations(prev => ({ ...prev, [key]: { remark_first: firstLabel, remark_second: secondLabel } }))
      if (isNew) setProgress(prev => prev ? { ...prev, annotated_clusters: prev.annotated_clusters + 1, pending_clusters: prev.pending_clusters - 1 } : prev)
      if (channel === 'review') {
        setClaimingNext(true)
        try {
          const claim = await api.post(`/api/review/datasets/${selectedDataset.id}/claim_next`, {})
          const cid = claim.cluster_id as number | null
          setMyTask(prev => prev ? { ...prev, current_cluster_id: cid, my_done: prev.my_done + 1, done: prev.done + 1 } : prev)
          if (cid == null) {
            setActiveCluster(null)
            push('🎉 已完成本数据集所有可抢任务', 'success')
          } else {
            const target = clusters.find(c => c.cluster_id === cid)
            setActiveCluster(target ?? null)
            setFirstLabel('')
            setSecondLabel('')
            push(`已保存，下一簇 第 ${clusters.findIndex(c => c.cluster_id === cid) + 1} 个 (#${cid})`, 'success')
          }
        } finally {
          setClaimingNext(false)
        }
      } else {
        push('标注已保存', 'success')
      }
    } catch (e: any) {
      push('保存失败', 'error')
    } finally {
      setSavingId(null)
    }
  }

  // v1.5：审核通道 - 手动领下一簇（当前簇所有图已判定后）
  async function claimNextReviewCluster() {
    if (!selectedDataset) return
    setClaimingNext(true)
    try {
      const claim = await api.post(`/api/review/datasets/${selectedDataset.id}/claim_next`, {})
      const cid = claim.cluster_id as number | null
      setMyTask(prev => prev ? { ...prev, current_cluster_id: cid, my_done: prev.my_done + 1, done: prev.done + 1 } : prev)
      if (cid == null) {
        setActiveCluster(null)
        push('🎉 已完成本数据集所有可抢任务', 'success')
      } else {
        const target = clusters.find(c => c.cluster_id === cid)
        setActiveCluster(target ?? null)
        push(`已领到下一簇 第 ${clusters.findIndex(c => c.cluster_id === cid) + 1} 个 (#${cid})`, 'success')
      }
    } catch (e: any) {
      push('领任务失败', 'error')
    } finally {
      setClaimingNext(false)
    }
  }

  async function clearAnnotation() {
    if (!selectedDataset || !activeCluster) return
    const key = String(activeCluster.cluster_id)
    if (!annotations[key]) return
    setSavingId(activeCluster.cluster_id)
    try {
      await api.delete(`/api/datasets/${selectedDataset.id}/cluster_annotations/${activeCluster.cluster_id}`)
      setAnnotations(prev => { const n = { ...prev }; delete n[key]; return n })
      setProgress(prev => prev ? { ...prev, annotated_clusters: prev.annotated_clusters - 1, pending_clusters: prev.pending_clusters + 1 } : prev)
      setFirstLabel('')
      setSecondLabel('')
      push('标注已清除', 'info')
    } catch {
      push('清除失败', 'error')
    } finally {
      setSavingId(null)
    }
  }

  // ── Dataset selector ──
  if (!selectedDataset) {
    return (
      <div className="flex flex-col gap-6">
        <ToastContainer toasts={toasts} onRemove={remove} />
        <div>
          <h1 className="text-2xl font-bold text-gray-900">样本标注</h1>
          <p className="text-sm text-gray-500 mt-1">选择一个数据集开始标注</p>
        </div>
        {datasets.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-24 text-center">
            <FolderOpen size={48} className="text-gray-300 mb-4" />
            <h3 className="text-lg font-semibold text-gray-500">暂无数据集</h3>
            <p className="text-sm text-gray-400 mt-1">请先在「样本管理」页面上传 CSV</p>
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {datasets.map(ds => (
              <button
                key={ds.id}
                onClick={() => setSelectedDataset(ds)}
                className="text-left bg-white border border-gray-200 rounded-2xl p-5 hover:border-red-400 hover:shadow-md transition-all group"
              >
                <div className="flex items-center gap-3 mb-3">
                  <div className="w-10 h-10 rounded-xl bg-red-100 flex items-center justify-center">
                    <Database size={20} className="text-red-500" />
                  </div>
                  <div>
                    <div className="font-semibold text-gray-900 text-sm truncate max-w-[160px]">{ds.name}</div>
                    <div className="text-xs text-gray-400">{new Date(ds.created_at).toLocaleDateString('zh-CN')}</div>
                  </div>
                </div>
                <div className="flex gap-3 text-xs text-gray-500">
                  <span className="bg-gray-100 px-2 py-0.5 rounded-full">{ds.cluster_count} 簇</span>
                  <span className="bg-gray-100 px-2 py-0.5 rounded-full">{ds.total_rows} 条</span>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    )
  }

  // ── Annotation view ──
  const annotatedCount = Object.keys(annotations).length

  return (
    <div className="flex flex-col flex-1 min-h-0 gap-0">
      <ToastContainer toasts={toasts} onRemove={remove} />

      {/* Top bar */}
      <div className="flex items-center gap-3 pb-4 border-b border-gray-200 mb-4">
        <button
          onClick={() => { setSelectedDataset(null); setActiveCluster(null) }}
          className="p-1.5 text-gray-500 hover:text-gray-800 hover:bg-gray-100 rounded-lg transition-colors"
        >
          <ArrowLeft size={18} />
        </button>
        <div className="flex-1 min-w-0">
          <h2 className="font-semibold text-gray-900 truncate">
            {selectedDataset.name}
            {channel === 'review' && <span className="ml-2 text-xs px-2 py-0.5 rounded-full bg-amber-100 text-amber-700 font-medium">审核通道 · 抢单模式</span>}
          </h2>
          <div className="text-xs text-gray-400">
            {channel === 'review' && myTask
              ? `全局 ${myTask.done} / ${myTask.total} · 我已完成 ${myTask.my_done}${activeCluster ? ` · 当前 第 ${clusters.findIndex(c => c.cluster_id === activeCluster.cluster_id) + 1} 个（#${activeCluster.cluster_id}）` : ''}`
              : progress ? `已标注 ${progress.annotated_clusters} / ${progress.total_clusters} 簇` : `${clusters.length} 个簇`}
          </div>
        </div>
        {progress && (
          <div className="flex items-center gap-2">
            <div className="text-xs text-gray-500">
              {Math.round((progress.annotated_clusters / Math.max(progress.total_clusters, 1)) * 100)}%
            </div>
            <div className="w-24 h-1.5 bg-gray-200 rounded-full overflow-hidden">
              <div
                className="h-full bg-red-400 rounded-full transition-all"
                style={{ width: `${Math.round((progress.annotated_clusters / Math.max(progress.total_clusters, 1)) * 100)}%` }}
              />
            </div>
          </div>
        )}
      </div>

      {/* Main layout: cluster list + detail */}
      <div className="flex gap-4 flex-1 min-h-0">

        {/* Cluster sidebar list — 评估通道显示；审核通道抢单模式下隐藏 */}
        {channel !== 'review' && (
          <div className="w-64 shrink-0 flex flex-col min-h-0 relative gap-2">
            {/* 搜索 + 筛选工具栏 */}
            <div className="shrink-0 flex flex-col gap-1.5">
              <div className="relative">
                <input
                  type="text"
                  value={uidQuery}
                  onChange={e => setUidQuery(e.target.value)}
                  placeholder="🔍 搜索 UID…"
                  className="w-full text-xs px-3 py-2 pr-8 rounded-lg border border-gray-200 bg-white focus:outline-none focus:ring-2 focus:ring-red-200 focus:border-red-400"
                />
                {uidQuery && (
                  <button
                    onClick={() => setUidQuery('')}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                  ><X size={12} /></button>
                )}
              </div>
              <div className="relative">
                <input
                  type="text"
                  value={tradeQuery}
                  onChange={e => setTradeQuery(e.target.value)}
                  placeholder="🔍 搜索行业…（如 金融/银行）"
                  className="w-full text-xs px-3 py-2 pr-8 rounded-lg border border-gray-200 bg-white focus:outline-none focus:ring-2 focus:ring-blue-200 focus:border-blue-400"
                />
                {tradeQuery && (
                  <button
                    onClick={() => setTradeQuery('')}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                  ><X size={12} /></button>
                )}
              </div>
              <div className="flex gap-1.5">
                <select
                  value={firstFilter}
                  onChange={e => { setFirstFilter(e.target.value); setSecondFilter('') }}
                  className="w-1/2 text-xs px-2 py-1.5 rounded-lg border border-gray-200 bg-white focus:outline-none focus:ring-2 focus:ring-red-200"
                >
                  <option value="">全部标签</option>
                  <option value="未打标">未打标</option>
                  {FIRST_LEVEL_LABELS.map(l => <option key={l} value={l}>{l}</option>)}
                </select>
                <select
                  value={secondFilter}
                  onChange={e => setSecondFilter(e.target.value)}
                  disabled={!firstFilter || firstFilter === '未打标'}
                  title={secondFilter || '全部细分'}
                  className="w-1/2 text-xs px-2 py-1.5 rounded-lg border border-gray-200 bg-white focus:outline-none focus:ring-2 focus:ring-red-200 disabled:bg-gray-50 disabled:text-gray-400"
                >
                  <option value="">全部细分</option>
                  {(firstFilter && firstFilter !== '未打标' ? (LABEL_TAXONOMY[firstFilter] ?? []) : []).map(l => (
                    <option key={l} value={l}>{l}</option>
                  ))}
                </select>
              </div>
              {(uidQuery.trim().length >= 2 || firstFilter || secondFilter || tradeQuery.trim()) && (
                <div className="flex items-center justify-between px-1">
                  <span className="text-[11px] text-gray-500">
                    {uidSearching ? '搜索中…' : `匹配 ${filteredCount} / ${clusters.length} 个簇`}
                  </span>
                  <button
                    onClick={() => { setUidQuery(''); setFirstFilter(''); setSecondFilter(''); setTradeQuery('') }}
                    className="text-[10px] text-gray-400 hover:text-red-500"
                  >清除</button>
                </div>
              )}
            </div>
            <ClusterList
              clusters={clusters}
              annotations={annotations}
              activeCluster={activeCluster}
              loadingClusters={loadingClusters}
              onSelect={setActiveCluster}
              uidMatchedIds={uidMatchedIds}
              firstFilter={firstFilter}
              secondFilter={secondFilter}
              tradeQuery={tradeQuery}
            />
          </div>
        )}

        {/* Detail panel — split into annotation controls (sticky top) + image grid (independent scroll) */}
        <div className="flex-1 min-w-0 flex flex-col gap-0 min-h-0">
          {!activeCluster ? (
            <div className="flex flex-col items-center justify-center h-full text-center text-gray-400">
              <Tag size={40} className="mb-3 opacity-30" />
              <p className="text-sm">
                {channel === 'review'
                  ? (loadingClusters ? '正在为你分配任务…' : '🎉 本数据集已全部认领完毕')
                  : '从左侧选择一个簇开始标注'}
              </p>
            </div>
          ) : (
            <>
              {/* ── Top: header + 审核通道领下一簇 / 评估通道簇标签 ── */}
              <div className="shrink-0 bg-white border border-gray-200 rounded-2xl p-5 mb-3">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <h3 className="font-bold text-gray-900 text-lg">
                      {(() => {
                        const seq = clusters.findIndex(c => c.cluster_id === activeCluster.cluster_id) + 1
                        return seq > 0 ? `第 ${seq} 个` : `簇 #${activeCluster.cluster_id}`
                      })()}
                      <span className="ml-2 text-sm font-normal text-gray-400">原簇 #{activeCluster.cluster_id}</span>
                      <span className="ml-2 text-sm font-normal text-gray-500">{activeCluster.count} 张图片</span>
                    </h3>
                    <p className="text-sm text-gray-500 mt-0.5">
                      {activeCluster.trade_first_name} {activeCluster.trade_second_name && `· ${activeCluster.trade_second_name}`}
                    </p>
                  </div>

                  {annotations[String(activeCluster.cluster_id)] && (
                    <span className={`text-xs px-2.5 py-1 rounded-full border font-medium ${LABEL_COLORS[annotations[String(activeCluster.cluster_id)].remark_first] ?? 'bg-gray-100 text-gray-600'}` }>
                      ✓ 已标注
                    </span>
                  )}
                </div>

                {/* 簇标签选择器：评估/审核通道一致 */}
                <div className="flex flex-col gap-3 mt-4">
                    <div className="flex items-center gap-3">
                      <label className="text-sm font-medium text-gray-700 w-16 shrink-0">一级标签</label>
                      <select
                        value={firstLabel}
                        onChange={e => handleFirstLabelChange(e.target.value)}
                        className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-300 focus:border-red-400 bg-white"
                      >
                        <option value="">请选择…</option>
                        {FIRST_LEVEL_LABELS.map(l => (
                          <option key={l} value={l}>{l}</option>
                        ))}
                      </select>
                    </div>

                    <div className="flex items-center gap-3">
                      <label className="text-sm font-medium text-gray-700 w-16 shrink-0">二级标签</label>
                      {isNoViolation ? (
                        <span className="flex-1 px-3 py-2 text-sm text-green-700 bg-green-50 border border-green-200 rounded-lg">不违规</span>
                      ) : (
                        <select
                          value={secondLabel}
                          onChange={e => setSecondLabel(e.target.value)}
                          disabled={!firstLabel}
                          className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-300 focus:border-red-400 bg-white disabled:bg-gray-50 disabled:text-gray-400"
                        >
                          <option value="">请选择…</option>
                          {secondOptions.map(l => (
                            <option key={l} value={l}>{l}</option>
                          ))}
                        </select>
                      )}
                    </div>

                    <div className="flex items-center gap-2 pt-1">
                      <button
                        onClick={saveAnnotation}
                        disabled={!firstLabel || !secondLabel || savingId === activeCluster.cluster_id}
                        className="px-5 py-2 bg-red-500 hover:bg-red-600 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-40 flex items-center gap-1.5"
                      >
                        {savingId === activeCluster.cluster_id ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle size={14} />}
                        {channel === 'review' ? '保存并领下一簇' : '保存标注'}
                      </button>
                      {annotations[String(activeCluster.cluster_id)] && (
                        <button
                          onClick={clearAnnotation}
                          disabled={savingId === activeCluster.cluster_id}
                          className="px-4 py-2 text-gray-500 hover:text-red-600 border border-gray-300 hover:border-red-300 rounded-lg text-sm transition-colors"
                        >
                          清除标注
                        </button>
                      )}
                    </div>
                  </div>
              </div>

              {/* ── Bottom: image grid — 评估/审核通道一致 ── */}
              <div className="flex-1 min-h-0 overflow-y-auto bg-white border border-gray-200 rounded-2xl p-5">
                <h4 className="font-semibold text-gray-700 text-sm mb-3 sticky top-0 bg-white pb-2">全部图片（{activeCluster.count} 张）</h4>
                <ImageGrid datasetId={selectedDataset.id} cluster={activeCluster} uidQuery={uidQuery} />
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

// ─── Sidebar ──────────────────────────────────────────────────────────────────

type Tab = 'dataset' | 'annotate'

function Sidebar({
  activeTab, onTabChange,
  channel, onChannelChange, whoami,
}: {
  activeTab: Tab; onTabChange: (t: Tab) => void
  channel: Channel; onChannelChange: (c: Channel) => void
  whoami: Whoami | null
}) {
  const tabs: { id: Tab; label: string; icon: React.ReactNode }[] = [
    { id: 'dataset', label: '样本管理', icon: <Database size={18} /> },
    { id: 'annotate', label: '样本标注', icon: <Tag size={18} /> },
  ]

  return (
    <div className="w-52 shrink-0 bg-gray-900 text-white flex flex-col h-full">
      {/* Logo */}
      <div className="px-5 py-5 border-b border-gray-700">
        <div className="text-sm font-bold text-white">资质聚类标注</div>
        <div className="text-xs text-gray-400 mt-0.5">专业号资质审核平台</div>
      </div>

      {/* Channel switcher */}
      <div className="px-3 pt-3 pb-1">
        <div className="text-[10px] uppercase tracking-wider text-gray-500 px-2 mb-1.5">通道</div>
        <div className="grid grid-cols-2 gap-1 p-1 bg-gray-800 rounded-xl">
          <button
            onClick={() => onChannelChange('evaluation')}
            className={`text-xs font-medium py-1.5 rounded-lg transition-all ${
              channel === 'evaluation' ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-400 hover:text-white'
            }`}
          >评估</button>
          <button
            onClick={() => onChannelChange('review')}
            className={`text-xs font-medium py-1.5 rounded-lg transition-all ${
              channel === 'review' ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-400 hover:text-white'
            }`}
          >审核</button>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex flex-col gap-1 p-3 flex-1">
        {tabs.map(t => (
          <button
            key={t.id}
            onClick={() => onTabChange(t.id)}
            className={`flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all text-left
              ${activeTab === t.id
                ? 'bg-white text-gray-900 shadow-sm'
                : 'text-gray-400 hover:text-white hover:bg-gray-800'
              }`}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </nav>

      <div className="px-5 py-4 border-t border-gray-700 text-xs text-gray-500">
        v1.1.0
        {whoami?.user?.name && <div className="mt-1 text-gray-400 truncate">{whoami.user.name}</div>}
      </div>
    </div>
  )
}

// ─── App root ─────────────────────────────────────────────────────────────────

export default function App() {
  const [activeTab, setActiveTab] = useState<Tab>('dataset')
  const [annotateDataset, setAnnotateDataset] = useState<Dataset | undefined>()
  const [channel, setChannel] = useState<Channel>('evaluation')
  const [whoami, setWhoami] = useState<Whoami | null>(null)

  useEffect(() => {
    fetch('/api/whoami').then(r => r.ok ? r.json() : null).then(setWhoami).catch(() => setWhoami(null))
  }, [])

  function handleAnnotate(ds: Dataset) {
    setAnnotateDataset(ds)
    setActiveTab('annotate')
  }

  function handleTabChange(tab: Tab) {
    setActiveTab(tab)
    if (tab !== 'annotate') setAnnotateDataset(undefined)
  }

  function handleChannelChange(c: Channel) {
    setChannel(c)
    setAnnotateDataset(undefined)
    setActiveTab('dataset')
  }

  return (
    <div className="flex h-screen bg-gray-50 overflow-hidden">
      <Sidebar
        activeTab={activeTab}
        onTabChange={handleTabChange}
        channel={channel}
        onChannelChange={handleChannelChange}
        whoami={whoami}
      />
      <main className="flex-1 p-8 overflow-auto flex flex-col">
        {activeTab === 'dataset' && (
          <DatasetPage key={channel} onAnnotate={handleAnnotate} channel={channel} whoami={whoami} />
        )}
        {activeTab === 'annotate' && (
          <div className="flex flex-col flex-1 min-h-0">
            <AnnotatePage
              key={`${channel}-${annotateDataset?.id}`}
              initialDataset={annotateDataset}
              channel={channel}
              whoami={whoami}
            />
          </div>
        )}
      </main>
    </div>
  )
}
