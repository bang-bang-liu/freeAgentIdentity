import { useEffect, useState } from 'react'
import { apiFetch } from '@/lib/utils'
import { translateAccountStatus } from '@/lib/i18n'
import { useI18n } from '@/lib/i18n-context'
import { Badge } from '@/components/ui/badge'
import { Users, CheckCircle, Clock, XCircle, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'

const PLATFORM_COLORS: Record<string, string> = {
  trae: 'text-blue-400',
  tavily: 'text-purple-400',
  cursor: 'text-emerald-400',
}

const STATUS_VARIANT: Record<string, any> = {
  registered: 'default',
  trial: 'success',
  subscribed: 'success',
  expired: 'warning',
  invalid: 'danger',
  free: 'secondary',
  eligible: 'secondary',
  unknown: 'secondary',
  valid: 'success',
}

export default function Dashboard() {
  const { t, language } = useI18n()
  const [stats, setStats] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true)
    try {
      setStats(await apiFetch('/accounts/stats'))
      setError('')
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const statCards = [
    { label: t('dashboard.totalAccounts'), value: stats?.total ?? '-', icon: Users, color: 'text-[var(--text-accent)]' },
    { label: t('dashboard.trial'), value: stats?.by_plan_state?.trial ?? 0, icon: Clock, color: 'text-amber-400' },
    { label: t('dashboard.subscribed'), value: stats?.by_plan_state?.subscribed ?? 0, icon: CheckCircle, color: 'text-emerald-400' },
    { label: t('dashboard.invalid'), value: (stats?.by_display_status?.expired ?? 0) + (stats?.by_validity_status?.invalid ?? 0), icon: XCircle, color: 'text-red-400' },
  ]
  const platformEntries = Object.entries(stats?.by_platform || {})
  const totalCount = Math.max(Number(stats?.total || 0), 0)

  const renderStatusGroup = (title: string, values: Record<string, number> | undefined, emptyCopy = t('dashboard.noData')) => (
    <div className="space-y-2">
      <div className="px-1 text-sm font-medium text-[var(--text-primary)]">{title}</div>
      {values && Object.keys(values).length > 0 ? (
        <div>
          {Object.entries(values).map(([status, count]) => (
            <div key={status} className="status-row">
              <Badge variant={STATUS_VARIANT[status] || 'secondary'}>{translateAccountStatus(status, language)}</Badge>
              <span className="text-sm tabular-nums text-[var(--text-secondary)]">{count}</span>
            </div>
          ))}
        </div>
      ) : (
        <div className="empty-state-panel">{emptyCopy}</div>
      )}
    </div>
  )

  return (
    <div className="dashboard-workspace" aria-busy={loading}>
      <div className="page-heading">
        <h1>{t('nav.dashboard')}</h1>
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          <RefreshCw className={`mr-2 h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />{t('common.refresh')}
        </Button>
      </div>
      {error && <div className="inline-error mb-6" role="alert">{error}</div>}
      <div className="metric-strip">
        {statCards.map(({ label, value, icon: Icon, color }) => (
          <div key={label}>
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="metric-label">{label}</p>
                <p className="metric-value">{loading && !stats ? <span className="skeleton w-20 h-10" /> : value}</p>
              </div>
              <div className="flex h-9 w-9 items-center justify-center">
                <Icon className={`h-4.5 w-4.5 ${color} opacity-90`} />
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="dashboard-layout">
      <section className="data-section">
        <h2 className="section-heading">{t('dashboard.platformDistribution')}</h2>
        <div className="space-y-3">
          {platformEntries.length > 0 ? platformEntries.map(([platform, count]) => {
            const countValue = Number(count) || 0
            const ratio = totalCount > 0 ? Math.round((countValue / totalCount) * 100) : 0
            return (
              <div key={platform} className="py-3">
                <div className="flex items-center justify-between gap-3">
                  <span className={`text-sm font-medium ${PLATFORM_COLORS[platform] || 'text-[var(--text-secondary)]'}`}>
                    {platform}
                  </span>
                  <span className="text-xs text-[var(--text-muted)]">{countValue} / {ratio}%</span>
                </div>
                <div className="progress-track mt-3">
                  <div className="progress-fill" style={{ width: `${ratio}%` }} />
                </div>
              </div>
            )
          }) : (
            <div className="empty-state-panel">{stats ? t('dashboard.noPlatformData') : t('dashboard.loadingStats')}</div>
          )}
        </div>
      </section>

      <section className="data-section">
        <h2 className="section-heading">{t('dashboard.statusDistribution')}</h2>
        <div className="status-columns">
          {renderStatusGroup(t('dashboard.plan'), stats?.by_plan_state, t('dashboard.noPlanData'))}
          {renderStatusGroup(t('dashboard.lifecycle'), stats?.by_lifecycle_status, t('dashboard.noLifecycleData'))}
          {renderStatusGroup(t('dashboard.validity'), stats?.by_validity_status, t('dashboard.noValidityData'))}
        </div>
      </section>
      </div>
    </div>
  )
}
