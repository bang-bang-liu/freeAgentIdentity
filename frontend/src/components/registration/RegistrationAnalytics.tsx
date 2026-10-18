import { useEffect, useId, useState } from 'react'
import { BarChart3, RefreshCw } from 'lucide-react'
import { apiFetch } from '@/lib/utils'
import { useI18n } from '@/lib/i18n-context'
import type { RegistrationStatistics } from '@/lib/registration-stats'
import { RegistrationCharts } from './RegistrationCharts'

export function RegistrationAnalytics({ platform = 'chatgpt' }: { platform?: string }) {
  const { language } = useI18n()
  const zh = language === 'zh-CN'
  const titleId = useId()
  const [days, setDays] = useState(30)
  const [revision, setRevision] = useState(0)
  const [statistics, setStatistics] = useState<RegistrationStatistics | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    let inFlight = false
    let activeTasks = 0
    const load = async () => {
      if (inFlight || disposed) return
      inFlight = true
      setLoading(true)
      try {
        const data: RegistrationStatistics = await apiFetch(`/tasks/register/stats?platform=${encodeURIComponent(platform)}&days=${days}`, { signal: controller.signal })
        if (disposed) return
        activeTasks = data.active_tasks || 0
        setStatistics(data)
        setError('')
      } catch (error) {
        if (!disposed) setError(error instanceof Error ? error.message : String(error))
      } finally {
        inFlight = false
        if (!disposed) setLoading(false)
      }
    }
    void load()
    const refreshActive = () => { if (!document.hidden && activeTasks > 0) void load() }
    const interval = window.setInterval(refreshActive, 5000)
    document.addEventListener('visibilitychange', refreshActive)
    return () => {
      disposed = true
      controller.abort()
      window.clearInterval(interval)
      document.removeEventListener('visibilitychange', refreshActive)
    }
  }, [days, platform, revision])

  const updatedAt = statistics?.updated_at && new Date(statistics.updated_at)
  const updateLabel = updatedAt && Number.isFinite(updatedAt.getTime())
    ? new Intl.DateTimeFormat(language, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(updatedAt) : ''

  return (
    <section className="registration-analytics" aria-labelledby={titleId} aria-busy={loading}>
      <header className="analytics-header">
        <div className="analytics-heading"><div className="analytics-emblem"><BarChart3 size={18} aria-hidden="true" /></div><div><h2 id={titleId}>{zh ? '自动注册统计' : 'Registration analytics'}</h2><p>{zh ? '每次尝试，都有迹可循。' : 'Track the outcome of every attempt.'}</p></div></div>
        <div className="analytics-controls">
          <label className="sr-only" htmlFor={`${titleId}-period`}>{zh ? '统计时间范围' : 'Statistics period'}</label>
          <select id={`${titleId}-period`} value={days} onChange={event => { setDays(Number(event.target.value)); setStatistics(null) }} className="control-surface control-surface-compact">
            <option value={7}>{zh ? '最近 7 天' : 'Last 7 days'}</option><option value={30}>{zh ? '最近 30 天' : 'Last 30 days'}</option><option value={0}>{zh ? '全部历史' : 'All time'}</option>
          </select>
          <button className="icon-control" onClick={() => setRevision(value => value + 1)} disabled={loading} aria-label={zh ? '刷新注册统计' : 'Refresh registration statistics'} title={zh ? '刷新注册统计' : 'Refresh registration statistics'}><RefreshCw size={15} className={loading ? 'animate-spin' : ''} aria-hidden="true" /></button>
        </div>
      </header>
      {error && <div className="inline-error analytics-error" role="alert">{zh ? '注册统计加载失败' : 'Registration statistics unavailable'}{statistics ? (zh ? '，当前显示上次结果' : '; showing the last result') : ''}。<button onClick={() => setRevision(value => value + 1)} disabled={loading}>{zh ? '重试' : 'Retry'}</button><details><summary>{zh ? '错误详情' : 'Error details'}</summary><p>{error}</p></details></div>}
      {statistics ? <RegistrationCharts statistics={statistics} /> : loading ? <div className="analytics-loading"><span className="skeleton" /><span className="skeleton" /><span className="skeleton" /></div> : null}
      {statistics && <footer className="analytics-footer"><span>{zh ? `${statistics.total_tasks || 0} 批次` : `${statistics.total_tasks || 0} batches`}{(statistics.active_tasks || 0) > 0 && <span className="analytics-live">{zh ? `${statistics.active_tasks} 批次执行中 · 每 5 秒更新` : `${statistics.active_tasks} active · updates every 5s`}</span>}{(statistics.cancelled_tasks || 0) > 0 && <span>{zh ? `已取消 ${statistics.cancelled_tasks} 批次` : `${statistics.cancelled_tasks} cancelled`}</span>}{(statistics.interrupted_tasks || 0) > 0 && <span>{zh ? `已中断 ${statistics.interrupted_tasks} 批次` : `${statistics.interrupted_tasks} interrupted`}</span>}</span><span>{updateLabel ? `${zh ? '更新于' : 'Updated'} ${updateLabel}` : ''}</span></footer>}
    </section>
  )
}
