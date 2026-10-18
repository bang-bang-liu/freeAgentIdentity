import { CircleCheck, CircleDashed, CircleX } from 'lucide-react'
import { useI18n } from '@/lib/i18n-context'
import { registrationStageName, type RegistrationStatistics } from '@/lib/registration-stats'

export function RegistrationCharts({ statistics, compact = false }: {
  statistics: RegistrationStatistics
  compact?: boolean
}) {
  const { language } = useI18n()
  const zh = language === 'zh-CN'
  const { success, failed, completed, failure_stages: stages } = statistics
  const radius = 54
  const circumference = 2 * Math.PI * radius
  const successLength = completed ? success / completed * circumference : 0
  const failureLength = completed ? failed / completed * circumference : 0
  const gap = success && failed ? 4 : 0
  const rate = statistics.success_rate === null ? '—' : statistics.success_rate.toLocaleString(language, { maximumFractionDigits: 1 })
  const number = (value: number) => value.toLocaleString(language)

  return (
    <div className={`registration-charts${compact ? ' is-compact' : ''}`}>
      <section className="registration-outcomes" aria-label={zh ? '成功与失败统计' : 'Success and failure statistics'}>
        <div className="chart-section-title">{zh ? '注册结果' : 'Registration outcomes'}<span>{zh ? `${number(completed)} 次已完成` : `${number(completed)} completed`}</span></div>
        <div className="outcome-visual">
          <div className="outcome-ring-wrap">
            <svg className="outcome-ring" viewBox="0 0 136 136" role="img" aria-label={zh ? `注册成功 ${success} 次，失败 ${failed} 次` : `${success} successful registrations, ${failed} failed registrations`}>
              <circle cx="68" cy="68" r={radius} fill="none" stroke="var(--chart-track)" strokeWidth="10" />
              <g transform="rotate(-90 68 68)">
                {success > 0 && <circle cx="68" cy="68" r={radius} fill="none" stroke="var(--chart-success)" strokeWidth="10" strokeDasharray={`${Math.max(0, successLength - gap)} ${circumference}`} strokeDashoffset={-gap / 2} />}
                {failed > 0 && <circle cx="68" cy="68" r={radius} fill="none" stroke="var(--chart-failure)" strokeWidth="10" strokeDasharray={`${Math.max(0, failureLength - gap)} ${circumference}`} strokeDashoffset={-successLength - gap / 2} />}
              </g>
            </svg>
            <div className="outcome-ring-label" aria-hidden="true"><strong>{rate}{statistics.success_rate !== null && <small>%</small>}</strong><span>{zh ? '成功率' : 'Success rate'}</span></div>
          </div>
          <dl className="outcome-counts">
            <div data-outcome="success"><dt><CircleCheck size={15} aria-hidden="true" />{zh ? '成功次数' : 'Successful'}</dt><dd data-stat="success">{number(success)}</dd></div>
            <div data-outcome="failure"><dt><CircleX size={15} aria-hidden="true" />{zh ? '失败次数' : 'Failed'}</dt><dd data-stat="failed">{number(failed)}</dd></div>
          </dl>
        </div>
        <p className="chart-caption">{zh ? '成功率 = 成功 ÷ 已完成；取消与未执行不计入失败。' : 'Success rate = successful ÷ completed. Cancelled and unattempted work is excluded.'}</p>
      </section>
      <section className="registration-failures" aria-label={zh ? '失败阶段分布' : 'Failure stage distribution'}>
        <div className="chart-section-title">{zh ? '失败阶段分布' : 'Failures by stage'}<span>{zh ? '次数 / 占比' : 'Count / share'}</span></div>
        {stages.length > 0 ? (
          <ol className="failure-stage-list">
            {stages.map(({ stage, count }) => (
              <li key={stage} data-stage={stage}>
                <div className="failure-stage-label"><span>{registrationStageName(stage, zh)}</span><span><strong>{number(count)}</strong><small>{failed ? Math.round(count / failed * 100) : 0}%</small></span></div>
                <div className="failure-stage-track" aria-hidden="true"><span style={{ width: `${failed ? count / failed * 100 : 0}%` }} /></div>
              </li>
            ))}
          </ol>
        ) : (
          <div className="chart-empty"><CircleDashed size={26} strokeWidth={1.4} aria-hidden="true" /><div><strong>{zh ? (completed ? '暂无注册失败' : '暂无注册结果') : (completed ? 'No registration failures' : 'No registration outcomes')}</strong><p>{zh ? '发生失败后，会在这里显示对应阶段。' : 'Failed attempts will appear here by stage.'}</p></div></div>
        )}
        {stages.some(item => item.stage === 'unknown') && <p className="chart-caption">{zh ? '未记录阶段的历史失败保留为“未标注”。' : 'Historical failures without stage data remain unspecified.'}</p>}
        {statistics.setup_failures > 0 && (
          <div className="registration-setup-failures" role="note">
            <strong>{zh ? `另有 ${number(statistics.setup_failures)} 批次在初始化时中止` : `${number(statistics.setup_failures)} batches stopped during setup`}</strong>
            <span>{statistics.setup_failure_stages.map(item => `${registrationStageName(item.stage, zh)} ${number(item.count)}`).join(' · ')}{zh ? '，未开始的注册单独计数。' : '; excluded from attempt counts.'}</span>
          </div>
        )}
      </section>
    </div>
  )
}
