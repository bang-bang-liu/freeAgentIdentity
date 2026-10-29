import { useEffect, useId, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { LoaderCircle, LogIn, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { apiFetch } from '@/lib/utils'
import { loginMethodLabel } from '@/lib/login-refresh'

type PreviewAccount = { account_id: number; email: string; ready: boolean; refresh_mode: string; blocking_reasons: string[] }
type Preview = { accounts: PreviewAccount[]; can_start: boolean }
type Selection = { platform: string; ids: number[]; select_all: boolean; status_filter: string | null; search_filter: string | null }

export function LoginRefreshMenu({ total, statusFilter, searchFilter, selectedIds, singleAccount, onTaskCreated }: {
  total: number; statusFilter: string; searchFilter: string; selectedIds: number[]
  singleAccount?: { id: number; email: string }
  onTaskCreated?: (taskId: string, title: string) => void
}) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const [selection, setSelection] = useState<Selection | null>(null)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [loading, setLoading] = useState(false)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [proxy, setProxy] = useState('http://127.0.0.1:7897')
  const [concurrency, setConcurrency] = useState('1')
  const [otpMode, setOtpMode] = useState('auto_then_manual')
  const readyAccounts = preview?.accounts.filter(account => account.ready) || []
  const inputClass = 'w-full rounded-lg border border-[var(--border)] bg-[var(--bg-pane)] px-3 py-2 text-sm text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]'

  useEffect(() => {
    if (!open || !selection) return
    const controller = new AbortController()
    setLoading(true)
    setPreview(null)
    setError('')
    apiFetch('/login-refresh/preview', { method: 'POST', body: JSON.stringify(selection), signal: controller.signal })
      .then(data => { if (!controller.signal.aborted) setPreview(data) })
      .catch(err => { if (!controller.signal.aborted) setError(err.message || '登录预检失败') })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [open, selection, attempt])

  const openPreview = () => {
    const ids = singleAccount ? [singleAccount.id] : [...selectedIds]
    setSelection({ platform: 'chatgpt', ids, select_all: ids.length === 0,
      status_filter: ids.length ? null : statusFilter || null,
      search_filter: ids.length ? null : searchFilter || null })
  }

  const start = async () => {
    if (starting || !preview?.can_start || !readyAccounts.length || readyAccounts.length > 100) return
    setStarting(true)
    setError('')
    try {
      // Use the reviewed account IDs, even if the list/filter changes later.
      const result = await apiFetch('/login-refresh/create', { method: 'POST', body: JSON.stringify({
        platform: 'chatgpt', ids: readyAccounts.map(account => account.account_id), select_all: false,
        proxy: proxy.trim(), concurrency: Number(concurrency), email_otp_mode: otpMode, setup_security: false,
      }) })
      if (!result?.task_id) throw new Error('创建任务的响应缺少任务 ID')
      setOpen(false)
      onTaskCreated?.(result.task_id, singleAccount ? `${singleAccount.email} · 重新登录` : `重新登录 ${readyAccounts.length} 个账号`)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally { setStarting(false) }
  }

  return <Dialog.Root open={open} onOpenChange={next => { if (!starting) setOpen(next) }}>
    <Dialog.Trigger asChild>
      <Button variant={singleAccount ? 'ghost' : 'outline'} size="sm" onClick={openPreview}
        disabled={total === 0} title={singleAccount ? `重新登录 ${singleAccount.email}` : '重新登录所选账号；未选择时预检当前筛选结果'}
        aria-label={singleAccount ? `重新登录 ${singleAccount.email}` : undefined}
        className={`login-refresh-trigger ${singleAccount ? 'h-8 px-2 text-xs' : 'h-8 shrink-0 whitespace-nowrap bg-transparent'}`}>
        <LogIn className="mr-1 h-3.5 w-3.5 shrink-0" />
        {singleAccount ? '重新登录' : selectedIds.length ? `重新登录 (${selectedIds.length})` : '重新登录'}
      </Button>
    </Dialog.Trigger>
    <Dialog.Portal>
      <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60" />
      <Dialog.Content className="fixed left-1/2 top-1/2 z-[60] flex max-h-[90dvh] w-[calc(100%-2rem)] max-w-2xl -translate-x-1/2 -translate-y-1/2 flex-col rounded-xl border border-[var(--border)] bg-[var(--bg-card)] text-[var(--text-primary)] shadow-2xl">
        <div className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] p-5">
          <div>
            <Dialog.Title className="text-base font-semibold">重新登录 ChatGPT</Dialog.Title>
            <Dialog.Description className="mt-1 text-xs leading-relaxed text-[var(--text-secondary)]">
              验证新 Token 的账号身份后保存。试用查询独立执行。
            </Dialog.Description>
          </div>
          <Dialog.Close className="icon-control" disabled={starting} aria-label="关闭登录预检"><X className="h-4 w-4" /></Dialog.Close>
        </div>
        <div className="min-h-0 space-y-4 overflow-y-auto p-5">
          <div>
            <label htmlFor={`${id}-proxy`} className="mb-1.5 block text-sm">登录代理</label>
            <input id={`${id}-proxy`} className={inputClass} value={proxy} disabled={starting} onChange={e => setProxy(e.target.value)} placeholder="留空使用直连" />
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label htmlFor={`${id}-otp`} className="mb-1.5 block text-sm">邮箱验证码来源</label>
              <select id={`${id}-otp`} className={inputClass} value={otpMode} disabled={starting} onChange={e => setOtpMode(e.target.value)}>
                <option value="auto_then_manual">自动取码，失败后手动输入</option><option value="manual">手动输入</option><option value="auto">仅自动取码</option>
              </select>
            </div>
            {!singleAccount && <div>
              <label htmlFor={`${id}-concurrency`} className="mb-1.5 block text-sm">登录并发数</label>
              <select id={`${id}-concurrency`} className={inputClass} value={concurrency} disabled={starting} onChange={e => setConcurrency(e.target.value)}>
                <option value="1">1</option><option value="2">2</option><option value="3">3</option>
              </select>
            </div>}
          </div>
          {loading && <p role="status" className="flex items-center gap-2 text-sm text-[var(--text-secondary)]"><LoaderCircle className="h-4 w-4 animate-spin" />正在读取本地登录能力…</p>}
          {error && <div role="alert" className="inline-error break-words">{error}{!preview && !loading && <button className="ml-3 underline" onClick={() => setAttempt(value => value + 1)}>重试</button>}</div>}
          {preview && <div className="space-y-2">
            <p className="text-xs text-[var(--text-secondary)]">预检 {preview.accounts.length} 个账号，可登录 {readyAccounts.length} 个</p>
            {readyAccounts.length > 100 && <p role="alert" className="text-sm text-[var(--status-warning)]">单次最多登录 100 个账号，请缩小选择范围。</p>}
            {!preview.accounts.length && <p className="py-4 text-sm text-[var(--text-muted)]">暂无匹配账号</p>}
            {preview.accounts.map(account => <div key={account.account_id} className="rounded-lg border border-[var(--border)] bg-[var(--bg-pane)] px-3 py-2.5 text-sm">
              <div className="break-all font-medium">{account.email || `账号 #${account.account_id}`}</div>
              <p className="mt-1 text-xs text-[var(--text-secondary)]">登录方式：{loginMethodLabel(account.refresh_mode)}</p>
              {!!account.blocking_reasons?.length && <p className="mt-1 whitespace-normal text-xs text-[var(--status-warning)]">{account.blocking_reasons.join('；')}</p>}
            </div>)}
          </div>}
        </div>
        <div className="flex shrink-0 flex-wrap justify-end gap-2 border-t border-[var(--border)] p-4">
          <Dialog.Close asChild><Button variant="outline" size="sm" disabled={starting}>取消</Button></Dialog.Close>
          <Button size="sm" disabled={starting || loading || !preview?.can_start || !readyAccounts.length || readyAccounts.length > 100} onClick={start}>
            {starting ? '正在创建任务…' : `开始登录${readyAccounts.length ? ` (${readyAccounts.length})` : ''}`}
          </Button>
        </div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
