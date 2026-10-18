import { useEffect, useState, useRef, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { useSearchParams } from 'react-router-dom'
import * as Dialog from '@radix-ui/react-dialog'
import { Search } from 'lucide-react'
import { getConfigOptions, getPlatforms } from '@/lib/app-data'
import type { ConfigOptionsResponse } from '@/lib/config-options'
import { getCaptchaStrategyLabel } from '@/lib/config-options'
import { apiDownload, apiFetch, triggerBrowserDownload } from '@/lib/utils'
import { formatDateTime, translateAccountStatus } from '@/lib/i18n'
import { useI18n } from '@/lib/i18n-context'
import { buildExecutorOptions, buildRegistrationOptions } from '@/lib/registration'
import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import { TaskDock } from '@/components/tasks/TaskDock'
import { RegistrationStatsDialog } from '@/components/registration/RegistrationStatsDialog'
import { AccountAvatar, accountHealth } from '@/components/accounts/AccountAvatar'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { getTaskStatusText, TASK_STATUS_VARIANTS } from '@/lib/tasks'
import { RefreshCw, Copy, Download, Upload, Plus, X, Mail, Trash2, UserCheck, FlaskConical, Crown, Clock, CircleX, Sparkles, BadgeCheck, Gift, Circle, ChevronLeft, ChevronRight, LogIn, Users, Inbox, Rows3, List, Eye, KeyRound, MoreHorizontal, LayoutGrid, Table2 } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

const STATUS_VARIANT: Record<string, any> = {
  registered: 'default', trial: 'success', subscribed: 'success',
  expired: 'warning', invalid: 'danger',
  free: 'secondary', eligible: 'secondary', valid: 'success', unknown: 'secondary',
}

const platformActionsCache = new Map<string, any[]>()
const platformActionsPromiseCache = new Map<string, Promise<any[]>>()

const ACCOUNT_TOOL_BUTTON_CLASS = 'h-8 shrink-0 whitespace-nowrap bg-transparent'
const QUERY_PROXY_PRESETS = [
  'http://127.0.0.1:7897',
  'http://127.0.0.1:7890',
]
const TRIAL_COUNTRY_OPTIONS = [
  ['PH', '菲律宾'],
  ['JP', '日本'],
  ['GB', '英国'],
  ['US', '美国'],
  ['TH', '泰国'],
  ['VN', '越南'],
  ['BR', '巴西'],
  ['CA', '加拿大'],
  ['AU', '澳大利亚'],
  ['DE', '德国'],
  ['FR', '法国'],
  ['SG', '新加坡'],
] as const
const TRIAL_COUNTRY_LABELS: Record<string, string> = Object.fromEntries(TRIAL_COUNTRY_OPTIONS)

function getAccountOverview(acc: any) {
  return acc?.overview || {}
}

function getDisplaySummary(acc: any) {
  return acc?.display_summary && typeof acc.display_summary === 'object' ? acc.display_summary : {}
}

function getVerificationMailbox(acc: any) {
  const providerResources = Array.isArray(acc?.provider_resources) ? acc.provider_resources : []
  const normalized = providerResources.find((item: any) => item?.resource_type === 'mailbox')
  if (normalized) {
    return {
      provider: normalized.provider_name,
      email: normalized.handle || normalized.display_name,
      account_id: normalized.resource_identifier,
    }
  }
  return null
}

function getLifecycleStatus(acc: any) {
  return getDisplaySummary(acc)?.status?.lifecycle || acc?.lifecycle_status || 'registered'
}

function getDisplayStatus(acc: any) {
  return getDisplaySummary(acc)?.status?.display || acc?.display_status || acc?.plan_state || getLifecycleStatus(acc)
}

function getPlanState(acc: any) {
  return getDisplaySummary(acc)?.status?.plan_state || acc?.plan_state || acc?.overview?.plan_state || 'unknown'
}

function getValidityStatus(acc: any) {
  return getDisplaySummary(acc)?.status?.validity || acc?.validity_status || acc?.overview?.validity_status || 'unknown'
}

const LIFECYCLE_ICONS: Record<string, LucideIcon> = {
  registered: UserCheck,
  trial: FlaskConical,
  subscribed: Crown,
  expired: Clock,
  invalid: CircleX,
  free: Sparkles,
  eligible: Gift,
  valid: BadgeCheck,
}

function getStatusIcon(status: string): LucideIcon {
  return LIFECYCLE_ICONS[status] || Circle
}

function getStatusIconColor(variant: string): string {
  if (variant === 'success') return 'text-[var(--status-success)]'
  if (variant === 'warning') return 'text-[var(--status-warning)]'
  if (variant === 'danger') return 'text-[var(--status-danger)]'
  if (variant === 'default') return 'text-[var(--text-accent)]'
  return 'text-[var(--text-muted)]'
}

function getPlanChip(acc: any): { label: string; tone: 'sky' | 'violet'; title: string } | null {
  const overview = getAccountOverview(acc)
  const summaryStatus = getDisplaySummary(acc)?.status || {}
  const raw = [
    acc?.plan_name,
    summaryStatus?.plan_name,
    overview?.plan_name,
    overview?.plan,
    overview?.membership_type,
    overview?.individual_membership_type,
  ].map(value => String(value || '').trim().toLowerCase()).find(Boolean) || ''
  const planState = getPlanState(acc)
  if (raw && (raw.includes('plus') || raw.includes('team') || raw.includes('pro') || raw.includes('premium'))) {
    return { label: 'Plus', tone: 'violet', title: 'Plus' }
  }
  if (raw && (raw.includes('free') || raw.includes('basic'))) {
    return { label: 'Free', tone: 'sky', title: 'Free' }
  }
  if (planState === 'free') return { label: 'Free', tone: 'sky', title: 'Free' }
  if (planState === 'subscribed') return { label: 'Plus', tone: 'violet', title: 'Plus' }
  return null
}

function getValidityChip(acc: any): { label: string; tone: 'emerald' | 'red' | 'gray' } | null {
  const validity = getValidityStatus(acc)
  if (!validity || validity === 'unknown') return null
  if (validity === 'valid') return { label: 'valid', tone: 'emerald' }
  if (validity === 'invalid') return { label: 'invalid', tone: 'red' }
  return { label: validity, tone: 'gray' }
}

function getCheckoutChainChip(acc: unknown): { label: 'cs' | 'oaics'; tone: 'sky' | 'violet'; title: string } | null {
  const overview = getAccountOverview(acc)
  if (overview?.plus_trial_checkout_state !== 'available') return null
  const chain = String(overview?.plus_trial_checkout_chain || '').trim().toLowerCase()
  if (chain === 'cs') return { label: 'cs', tone: 'sky', title: 'cs_live checkout' }
  if (chain === 'oaics') return { label: 'oaics', tone: 'violet', title: 'oaics checkout' }
  return null
}

function getPlusTrialChip(acc: unknown): { label: string; tone: 'violet'; title: string } | null {
  const overview = getAccountOverview(acc)
  if (overview?.plus_trial_eligible !== true) return null
  return { label: '优惠', tone: 'violet', title: 'Plus 1 个月免费优惠' }
}

function getGCashChip(acc: unknown): { label: 'GCash'; tone: 'emerald'; title: string } | null {
  const overview = getAccountOverview(acc)
  const chips = Array.isArray(overview?.chips) ? overview.chips : []
  const qualifies = overview?.gcash_eligible === true
    || overview?.gcash_qualified === true
    || overview?.gcash_available === true
  if (!qualifies && !chips.some((chip: unknown) => String(chip || '').trim().toLowerCase() === 'gcash')) {
    return null
  }
  return { label: 'GCash', tone: 'emerald', title: 'GCash' }
}

function isTrialAccount(acc: any): boolean {
  const overview = getAccountOverview(acc)
  return (
    getPlanState(acc) === 'trial' ||
    getPlanState(acc) === 'eligible' ||
    getLifecycleStatus(acc) === 'trial' ||
    Boolean(overview?.trial_end_time)
  )
}

const STATUS_CHIP_TONES: Record<string, string> = {
  emerald: 'bg-[var(--success-soft)] text-[var(--status-success)]',
  violet: 'bg-[var(--accent-soft)] text-[var(--text-accent)]',
  sky: 'bg-[var(--bg-pane)] text-[var(--text-secondary)]',
  amber: 'bg-[var(--warning-soft)] text-[var(--status-warning)]',
  red: 'bg-[var(--danger-soft)] text-[var(--status-danger)]',
  gray: 'bg-[var(--bg-pane)] text-[var(--text-secondary)]',
}

function StatusIconChip({ label, tone, title }: { label: string; tone: string; title?: string }) {
  return (
    <span
      aria-label={title || label}
      title={title || label}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium ${STATUS_CHIP_TONES[tone] || STATUS_CHIP_TONES.gray}`}
    >
      <span>{label}</span>
    </span>
  )
}

function getPrimaryMetrics(acc: any) {
  const metrics = getDisplaySummary(acc)?.primary_metrics
  return Array.isArray(metrics) ? metrics : []
}

function getSecondaryMetrics(acc: any) {
  const metrics = getDisplaySummary(acc)?.secondary_metrics
  return Array.isArray(metrics) ? metrics : []
}

function getDisplayWarnings(acc: any) {
  const warnings = getDisplaySummary(acc)?.warnings
  return Array.isArray(warnings) ? warnings : []
}

function getDisplayBadges(acc: any) {
  const badges = getDisplaySummary(acc)?.badges
  return Array.isArray(badges) ? badges : []
}

function getDisplaySections(acc: any) {
  const sections = getDisplaySummary(acc)?.sections
  return Array.isArray(sections) ? sections : []
}

function getProviderAccounts(acc: any) {
  return Array.isArray(acc?.provider_accounts) ? acc.provider_accounts : []
}

function getCredentials(acc: any) {
  return Array.isArray(acc?.credentials) ? acc.credentials : []
}

function getPlatformCredentialValue(acc: any, key: string) {
  const credential = getCredentials(acc).find((item: any) => (
    item?.scope === 'platform' && item?.key === key && item?.value != null && item?.value !== ''
  ))
  return credential?.value || ''
}

function getSecuritySettings(acc: any) {
  const overview = getAccountOverview(acc)
  const legacyExtra = overview?.legacy_extra && typeof overview.legacy_extra === 'object'
    ? overview.legacy_extra
    : {}
  const totpSecret = String(
    getPlatformCredentialValue(acc, 'totp_secret')
      || acc?.totp_secret
      || legacyExtra.totp_secret
      || '',
  )
  const otpauth = String(
    getPlatformCredentialValue(acc, 'otpauth')
      || acc?.otpauth
      || legacyExtra.otpauth
      || '',
  )
  const explicitTotpSet = legacyExtra.totp_set ?? acc?.totp_set
  return {
    password: String(acc?.password || ''),
    passwordSet: Boolean(legacyExtra.password_set ?? acc?.password_set ?? acc?.password),
    totpSet: explicitTotpSet == null ? Boolean(totpSecret || otpauth) : Boolean(explicitTotpSet),
    totpSecret,
    otpauth,
  }
}

function formatSurvivalDuration(acc: any, now: number, language: string) {
  const createdAt = new Date(acc?.created_at || '').getTime()
  if (!Number.isFinite(createdAt) || createdAt > now) return '-'

  const lifecycle = getLifecycleStatus(acc)
  const validity = getValidityStatus(acc)
  if (lifecycle === 'invalid' || lifecycle === 'expired' || validity === 'invalid') {
    return language.startsWith('zh') ? '已失效' : 'Expired'
  }

  const totalMinutes = Math.max(0, Math.floor((now - createdAt) / 60000))
  const days = Math.floor(totalMinutes / (24 * 60))
  const hours = Math.floor((totalMinutes % (24 * 60)) / 60)
  const minutes = totalMinutes % 60
  if (language.startsWith('zh')) {
    if (days > 0) return `${days}天${hours}小时`
    if (hours > 0) return `${hours}小时${minutes}分`
    return `${minutes}分钟`
  }
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

function getPrimaryToken(acc: any) {
  if (acc?.primary_token) return acc.primary_token
  const credential = getCredentials(acc).find((item: any) => item?.scope === 'platform' && item?.credential_type === 'token' && item?.value)
  return credential?.value || ''
}

function getAccessToken(acc: any) {
  return String(
    getPlatformCredentialValue(acc, 'access_token')
      || getPlatformCredentialValue(acc, 'accessToken')
      || acc?.primary_token
      || '',
  )
}

async function copyToClipboard(text: string) {
  const value = String(text || '')
  if (!value) throw new Error('没有可复制的内容')
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value)
    return
  }
  const element = document.createElement('textarea')
  element.value = value
  element.setAttribute('readonly', '')
  element.style.position = 'fixed'
  element.style.opacity = '0'
  document.body.appendChild(element)
  element.select()
  const copied = document.execCommand('copy')
  document.body.removeChild(element)
  if (!copied) throw new Error('剪贴板不可用')
}

function escapeCsvField(value: unknown) {
  const text = value == null ? '' : String(value)
  if (!/[",\n\r]/.test(text)) return text
  return `"${text.replace(/"/g, '""')}"`
}

async function loadPlatformActions(platform: string, options?: { force?: boolean }) {
  const key = String(platform || '').trim()
  if (!key) return []
  const force = Boolean(options?.force)
  if (!force && platformActionsCache.has(key)) {
    return platformActionsCache.get(key) || []
  }
  if (!force && platformActionsPromiseCache.has(key)) {
    return platformActionsPromiseCache.get(key) || []
  }
  const pending = apiFetch(`/actions/${key}`)
    .then((data) => {
      const actions = Array.isArray(data?.actions) ? data.actions : []
      platformActionsCache.set(key, actions)
      platformActionsPromiseCache.delete(key)
      return actions
    })
    .catch((error) => {
      platformActionsPromiseCache.delete(key)
      throw error
    })
  platformActionsPromiseCache.set(key, pending)
  return pending
}

function buildActionParamDraft(action: any, acc: any) {
  const params = Array.isArray(action?.params) ? action.params : []
  const emailPrefix = String(acc?.email || '').split('@')[0] || 'Development'
  const draft: Record<string, string> = {}
  params.forEach((param: any) => {
    if (action?.id === 'create_api_key' && param?.key === 'name') {
      draft[param.key] = `${emailPrefix}Development`
      return
    }
    const options = Array.isArray(param?.options) && param.options.length > 0
      ? param.options
      : param?.key === 'proxy'
        ? QUERY_PROXY_PRESETS
        : []
    if (options.length > 0) {
      draft[param?.key || ''] = String(options[0] ?? '')
      return
    }
    draft[param?.key || ''] = param?.type === 'checkbox' ? 'false' : ''
  })
  return draft
}

// ── 注册弹框 ────────────────────────────────────────────────
function RegisterModal({
  platformMeta,
  onClose,
  onDone,
  onTaskCreated,
}: {
  platformMeta: any
  onClose: () => void
  onDone: () => void
  onTaskCreated: (taskId: string) => void
}) {
  const { t, language } = useI18n()
  const [configOptions, setConfigOptions] = useState<ConfigOptionsResponse>({
    mailbox_providers: [],
    captcha_providers: [],
    mailbox_settings: [],
    captcha_settings: [],
    captcha_policy: {},
    executor_options: [],
    identity_mode_options: [],
  })
  const [configLoading, setConfigLoading] = useState(true)
  const [regCount, setRegCount] = useState(1)
  const [concurrency, setConcurrency] = useState(1)
  const [dynamicProxy, setDynamicProxy] = useState('http://127.0.0.1:7897')
  const [outlookPoolText, setOutlookPoolText] = useState('')
  const [enableTwoFactorPassword, setEnableTwoFactorPassword] = useState(true)
  const [autoUploadSub2Api, setAutoUploadSub2Api] = useState(false)
  const [sub2ApiConfigOpen, setSub2ApiConfigOpen] = useState(false)
  const [sub2ApiUrl, setSub2ApiUrl] = useState('http://127.0.0.1:8080')
  const [sub2ApiApiKey, setSub2ApiApiKey] = useState('')
  const [startError, setStartError] = useState('')
  const [selection, setSelection] = useState({
    identityProvider: 'mailbox',
    executorType: 'headless',
    mailboxProvider: '',
  })
  const [taskId, setTaskId] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const [starting, setStarting] = useState(false)

  const supportedExecutors: string[] = platformMeta?.supported_executors || []
  const registrationOptions = buildRegistrationOptions(platformMeta, language)
  const executorOptions = buildExecutorOptions(
    supportedExecutors,
    platformMeta?.supported_executor_options || [],
    language,
  )
  const visibleRegistrationOptions = registrationOptions
  const visibleExecutorOptions = executorOptions
  const selectedRegistration = registrationOptions.find(option =>
    option.identityProvider === selection.identityProvider,
  )
  const selectedExecutor = executorOptions.find(option => option.value === selection.executorType)

  useEffect(() => {
    let active = true
    setConfigLoading(true)
    getConfigOptions()
      .then((options) => {
        if (!active) return
        if (options) {
          setConfigOptions(options)
        }
      })
      .catch(() => {
        if (!active) return
        setConfigOptions({
          mailbox_providers: [],
          captcha_providers: [],
          mailbox_settings: [],
          captcha_settings: [],
          captcha_policy: {},
          executor_options: [],
          identity_mode_options: [],
        })
      })
      .finally(() => {
        if (active) setConfigLoading(false)
      })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (configLoading || registrationOptions.length === 0) return
    const defaultRegistration = registrationOptions[0]
    setSelection((current) => {
      const identityProvider = current.identityProvider || defaultRegistration.identityProvider
      const validExecutorOptions = buildExecutorOptions(
        supportedExecutors,
        platformMeta?.supported_executor_options || [],
        language,
      )
        .filter(option => !option.disabled)
      const preferredExecutor = supportedExecutors.includes('headless')
        ? 'headless'
        : supportedExecutors[0] || ''
      const executorType = validExecutorOptions.some(option => option.value === current.executorType)
        ? current.executorType
        : (validExecutorOptions.find(option => option.value === preferredExecutor)?.value || validExecutorOptions[0]?.value || '')
      if (
        current.identityProvider === identityProvider &&
        current.executorType === executorType
      ) {
        return current
      }
      return { ...current, identityProvider, executorType }
    })
  }, [configLoading, registrationOptions, supportedExecutors])

  useEffect(() => {
    if (!selection.identityProvider) return
    const validExecutorOptions = buildExecutorOptions(
      supportedExecutors,
      platformMeta?.supported_executor_options || [],
      language,
    )
      .filter(option => !option.disabled)
    if (!validExecutorOptions.some(option => option.value === selection.executorType)) {
      setSelection(current => {
        const nextExecutorType = validExecutorOptions[0]?.value || ''
        if (current.executorType === nextExecutorType) {
          return current
        }
        return {
          ...current,
          executorType: nextExecutorType,
        }
      })
    }
  }, [selection.identityProvider, selection.executorType, supportedExecutors])

  const enabledMailboxProviders = (configOptions.mailbox_settings || []).filter(item => item.enabled)
  const defaultMailboxProvider = enabledMailboxProviders.find(item => item.is_default) || enabledMailboxProviders[0] || null
  const selectedMailboxProvider = enabledMailboxProviders.find(
    item => item.provider_key === selection.mailboxProvider,
  ) || defaultMailboxProvider
  const selectedMailboxLabel = selectedMailboxProvider?.display_name
    || selectedMailboxProvider?.catalog_label
    || selectedMailboxProvider?.provider_key
    || '-'
  const isProtocolOutlookPool = selection.executorType === 'protocol'
    && selectedMailboxProvider?.provider_key === 'local_ms_pool'
  const hasConfiguredOutlookPool = Boolean(
    selectedMailboxProvider?.config?.local_ms_pool_file
    || selectedMailboxProvider?.auth?.local_ms_pool_text
    || selectedMailboxProvider?.config?.local_ms_pool_text,
  )
  const mailboxSelectionMissing = selection.identityProvider === 'mailbox'
    && !selectedMailboxProvider?.provider_key
  const outlookPoolMissing = isProtocolOutlookPool
    && !outlookPoolText.trim()
    && !hasConfiguredOutlookPool

  const start = async () => {
    setStarting(true)
    setStartError('')
    try {
      if (autoUploadSub2Api && (!sub2ApiUrl.trim() || !sub2ApiApiKey.trim())) {
        setSub2ApiConfigOpen(true)
        throw new Error(t('accounts.sub2ApiConfigRequired'))
      }
      const extra: Record<string, any> = {
        identity_provider: selection.identityProvider,
        enable_2fa_password: enableTwoFactorPassword,
        auto_upload_sub2api_agent_identity: autoUploadSub2Api,
      }
      if (selection.identityProvider === 'mailbox') {
        if (!selectedMailboxProvider?.provider_key) {
          throw new Error(t('accounts.missingDefaultMailbox'))
        }
        extra.mail_provider = selectedMailboxProvider.provider_key
        if (isProtocolOutlookPool) {
          if (!outlookPoolText.trim() && !hasConfiguredOutlookPool) {
            throw new Error(t('accounts.outlookPoolRequired'))
          }
          if (outlookPoolText.trim()) {
            extra.local_ms_pool_text = outlookPoolText.trim()
          }
        }
      }
      const res = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          count: regCount, concurrency,
          executor_type: selection.executorType,
          captcha_solver: 'auto',
          proxy: dynamicProxy.trim() || null,
          sub2api_url: autoUploadSub2Api ? sub2ApiUrl.trim() : null,
          sub2api_api_key: autoUploadSub2Api ? sub2ApiApiKey.trim() : null,
          extra,
        }),
      })
      setTaskId(res.task_id)
      onTaskCreated(res.task_id)
    } catch (error: any) {
      setStartError(error?.message || String(error))
    } finally { setStarting(false) }
  }

  const handleDone = (_status: string) => {
    setDone(true)
    onDone()
  }

  const dialog = (
    <div className="dialog-backdrop">
      <div className="dialog-panel register-dialog flex flex-col"
           role="dialog" aria-modal="true" aria-label={t('accounts.autoRegister')}
           onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <h2 className="text-base font-semibold text-[var(--text-primary)]">{t('accounts.autoRegister')} {platformMeta?.display_name || 'ChatGPT'}</h2>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"><X className="h-4 w-4" /></button>
        </div>
        <div className="register-content px-6 py-5 flex-1 overflow-y-auto flex flex-col gap-6">
          {!taskId ? (
            configLoading ? (
              <div className="text-sm text-[var(--text-muted)]">{t('accounts.loadingRegistrationConfig')}</div>
            ) : (
              <>
                <div>
                  <div className="mt-1 text-sm font-semibold text-[var(--text-primary)]">{t('accounts.selectIdentity')}</div>
                  <div className="mt-3 grid gap-3 md:grid-cols-2">
                    {visibleRegistrationOptions.map(option => {
                      const active = selection.identityProvider === option.identityProvider
                      return (
                        <button
                          key={option.key}
                          type="button"
                          aria-pressed={active}
                          title={option.description}
                          onClick={() => setSelection(current => ({
                            ...current,
                            identityProvider: option.identityProvider,
                          }))}
                          className={`registration-choice border px-4 py-3 text-left transition-colors ${
                            active
                              ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                              : 'border-[var(--border)] bg-[var(--bg-pane)]/45 hover:border-[var(--accent)]/60'
                          }`}
                        >
                          <div className="flex items-center gap-2 text-sm font-medium text-[var(--text-primary)]">
                            {option.identityProvider === 'mailbox' ? <Mail className="h-4 w-4" /> : null}
                            {option.label}
                          </div>
                        </button>
                      )
                    })}
                  </div>
                </div>

                <div>
                  <div className="mt-1 text-sm font-semibold text-[var(--text-primary)]">{t('accounts.selectExecutor')}</div>
                  <div className="mt-3 grid gap-3 md:grid-cols-3">
                    {visibleExecutorOptions.map(option => {
                      const active = selection.executorType === option.value
                      return (
                        <button
                          key={option.value}
                          type="button"
                          aria-pressed={active}
                          disabled={option.disabled}
                          title={option.description}
                          onClick={() => !option.disabled && setSelection(current => ({ ...current, executorType: option.value }))}
                          className={`registration-choice border px-4 py-3 text-left transition-colors ${
                            option.disabled
                              ? 'cursor-not-allowed border-[var(--border)] bg-[var(--bg-hover)] opacity-50'
                              : active
                                ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                                : 'border-[var(--border)] bg-[var(--bg-pane)]/45 hover:border-[var(--accent)]/60'
                          }`}
                        >
                          <div className="text-sm font-medium text-[var(--text-primary)]">{option.label}</div>
                          {option.reason ? (
                            <div className="mt-2 text-xs text-amber-400">{option.reason}</div>
                          ) : null}
                        </button>
                      )
                    })}
                  </div>
                </div>

                {selection.identityProvider === 'mailbox' ? (
                  <div>
                    <label className="text-xs text-[var(--text-muted)] block mb-1">
                      {t('register.mailboxService')}
                    </label>
                    <select
                      value={selectedMailboxProvider?.provider_key || ''}
                      onChange={event => setSelection(current => ({
                        ...current,
                        mailboxProvider: event.target.value,
                      }))}
                      className="control-surface control-surface-compact w-full"
                    >
                      {enabledMailboxProviders.length === 0 ? (
                        <option value="">{t('accounts.missingDefaultMailbox')}</option>
                      ) : enabledMailboxProviders.map(provider => (
                        <option key={provider.provider_key} value={provider.provider_key}>
                          {provider.display_name || provider.catalog_label || provider.provider_key}
                        </option>
                      ))}
                    </select>
                  </div>
                ) : null}

                {isProtocolOutlookPool ? (
                  <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-pane)]/45 p-4">
                    <label className="text-sm font-semibold text-[var(--text-primary)] block mb-1">
                      {t('accounts.outlookPoolLabel')}
                    </label>
                    <div className="mb-2 text-xs text-[var(--text-muted)]">
                      {t('accounts.outlookPoolHint')}
                    </div>
                    <div className="mb-2 text-xs text-sky-300">
                      {t('accounts.outlookAliasHint')}
                    </div>
                    {hasConfiguredOutlookPool ? (
                      <div className="mb-2 text-xs text-emerald-300">
                        {t('accounts.outlookPoolConfiguredHint')}
                      </div>
                    ) : null}
                    <textarea
                      value={outlookPoolText}
                      onChange={(event) => setOutlookPoolText(event.target.value)}
                      rows={7}
                      spellCheck={false}
                      placeholder={t('accounts.outlookPoolPlaceholder')}
                      className="control-surface w-full resize-y font-mono text-xs"
                    />
                  </div>
                ) : null}

                {platformMeta?.name === 'chatgpt' || platformMeta?.value === 'chatgpt' ? (
                  <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-pane)]/45 px-4 py-3">
                    <label className="flex cursor-pointer items-start gap-3">
                      <input
                        type="checkbox"
                        checked={enableTwoFactorPassword}
                        onChange={(event) => setEnableTwoFactorPassword(event.target.checked)}
                        className="mt-0.5 h-4 w-4 accent-[var(--accent)]"
                      />
                      <span>
                        <span className="block text-sm font-medium text-[var(--text-primary)]">
                          {t('accounts.enableTwoFactorPassword')}
                        </span>
                        <span className="mt-1 block text-xs text-[var(--text-muted)]">
                          {t('accounts.enableTwoFactorPasswordHint')}
                        </span>
                      </span>
                    </label>
                  </div>
                ) : null}

                <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-pane)]/45 px-4 py-3">
                  <label className="flex cursor-pointer items-start gap-3">
                    <input
                      type="checkbox"
                      checked={autoUploadSub2Api}
                      onChange={(event) => {
                        const enabled = event.target.checked
                        setAutoUploadSub2Api(enabled)
                        if (enabled) setSub2ApiConfigOpen(true)
                      }}
                      className="mt-0.5 h-4 w-4 accent-[var(--accent)]"
                    />
                    <span>
                      <span className="block text-sm font-medium text-[var(--text-primary)]">
                        {t('accounts.autoUploadSub2Api')}
                      </span>
                      <span className="mt-1 block text-xs text-[var(--text-muted)]">
                        {t('accounts.autoUploadSub2ApiHint')}
                      </span>
                    </span>
                  </label>
                  {autoUploadSub2Api ? (
                    <button
                      type="button"
                      onClick={() => setSub2ApiConfigOpen(true)}
                      className="mt-3 text-xs text-[var(--accent)] hover:underline"
                    >
                      {t('accounts.configureSub2Api')}
                    </button>
                  ) : null}
                </div>

                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="text-xs text-[var(--text-muted)] block mb-1">{t('accounts.registrationCount')}</label>
                    <input type="number" min={1} max={99} value={regCount}
                      onChange={e => setRegCount(Number(e.target.value))}
                      className="control-surface control-surface-compact text-center" />
                  </div>
                  <div>
                    <label className="text-xs text-[var(--text-muted)] block mb-1">{t('accounts.concurrency')}</label>
                    <input type="number" min={1} max={20} value={concurrency}
                      onChange={e => setConcurrency(Number(e.target.value))}
                      className="control-surface control-surface-compact text-center" />
                  </div>
                </div>

                  <div>
                    <label className="text-xs text-[var(--text-muted)] block mb-1">动态 IP 代理（可选）</label>
                    <input
                      type="text"
                      value={dynamicProxy}
                      onChange={(e) => setDynamicProxy(e.target.value)}
                      placeholder="http://127.0.0.1:7897"
                      spellCheck={false}
                      className="control-surface control-surface-compact w-full font-mono text-xs"
                    />
                    <div className="mt-1 text-xs text-[var(--text-muted)]">
                      填写后本次注册使用该动态 IP 代理；留空时使用本地网络直连。
                    </div>
                  </div>


                <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-4 py-3 text-xs text-[var(--text-secondary)]">
                  <div>{t('accounts.identitySummary')}: <span className="text-[var(--text-primary)]">{selectedRegistration?.label || '-'}</span></div>
                  <div className="mt-1">{t('accounts.executorSummary')}: <span className="text-[var(--text-primary)]">{selectedExecutor?.label || '-'}</span></div>
                  {selection.identityProvider === 'mailbox' ? (
                    <div className="mt-1">{t('register.mailboxService')}: <span className="text-[var(--text-primary)]">
                      {selectedMailboxLabel}
                    </span></div>
                  ) : null}
                  <div className="mt-1">{t('accounts.verificationSummary')}: <span className="text-[var(--text-primary)]">{
                    selection.executorType === 'protocol'
                      ? t('accounts.protocolVerificationSummary', { provider: selectedMailboxLabel })
                      : getCaptchaStrategyLabel(selection.executorType, configOptions.captcha_policy, configOptions.captcha_providers, language)
                  }</span></div>
                </div>

                {startError ? (
                  <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
                    {startError}
                  </div>
                ) : null}

                <Button
                  onClick={start}
                  disabled={starting || !selection.identityProvider || !selection.executorType || mailboxSelectionMissing || outlookPoolMissing || (autoUploadSub2Api && (!sub2ApiUrl.trim() || !sub2ApiApiKey.trim()))}
                  className="w-full"
                >
                  {starting ? t('accounts.starting') : t('accounts.startAutoRegister')}
                </Button>
              </>
            )
          ) : (
            <div className="flex min-h-0 flex-1 flex-col gap-3">
              <div className="min-h-0 flex-1">
                <TaskLogPanel taskId={taskId} onDone={handleDone} />
              </div>
            </div>
          )}
        </div>
        <div className="px-6 py-3 border-t border-[var(--border)] flex justify-end">
          <Button variant="outline" size="sm" onClick={onClose}>
            {taskId || done ? t('common.close') : t('common.cancel')}
          </Button>
        </div>
      </div>
      {sub2ApiConfigOpen ? (
        <div
          className="fixed inset-0 z-[120] flex items-center justify-center bg-black/60 p-4"
        >
          <div
            className="w-full max-w-md rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] p-5 shadow-xl"
            onClick={(event) => event.stopPropagation()}
          >
            <h3 className="text-base font-semibold text-[var(--text-primary)]">
              {t('accounts.sub2ApiConfigTitle')}
            </h3>
            <p className="mt-1 text-xs text-[var(--text-muted)]">
              {t('accounts.sub2ApiConfigHint')}
            </p>
            <label className="mt-4 block text-xs text-[var(--text-muted)]">
              {t('accounts.sub2ApiUrl')}
              <input
                type="url"
                value={sub2ApiUrl}
                onChange={(event) => setSub2ApiUrl(event.target.value)}
                placeholder="http://127.0.0.1:8080"
                className="control-surface control-surface-compact mt-1 w-full font-mono text-xs"
              />
            </label>
            <label className="mt-3 block text-xs text-[var(--text-muted)]">
              {t('accounts.sub2ApiAdminApiKey')}
              <input
                type="password"
                value={sub2ApiApiKey}
                onChange={(event) => setSub2ApiApiKey(event.target.value)}
                autoComplete="off"
                placeholder="sk-..."
                className="control-surface control-surface-compact mt-1 w-full font-mono text-xs"
              />
            </label>
            <div className="mt-2 text-xs text-[var(--text-muted)]">
              {t('accounts.sub2ApiApiKeyHint')}
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <Button variant="outline" size="sm" onClick={() => setSub2ApiConfigOpen(false)}>
                {t('common.cancel')}
              </Button>
              <Button
                size="sm"
                disabled={!sub2ApiUrl.trim() || !sub2ApiApiKey.trim()}
                onClick={() => setSub2ApiConfigOpen(false)}
              >
                {t('accounts.saveSub2ApiConfig')}
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )

  return typeof document !== 'undefined' ? createPortal(dialog, document.body) : dialog
}

// ── 新增账号弹框 ─────────────────────────────────────────
function formatResultValue(value: any) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}

function ResultStat({ label, value }: { label: string; value: any }) {
  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2">
      <div className="text-[11px] uppercase tracking-[0.16em] text-[var(--text-muted)]">{label}</div>
      <div className="mt-1 text-sm font-medium text-[var(--text-primary)] break-all">{formatResultValue(value)}</div>
    </div>
  )
}

function metricToneClass(tone?: string) {
  if (tone === 'good') return 'border-emerald-500/25 bg-emerald-500/10 text-emerald-200'
  if (tone === 'warning') return 'border-amber-500/25 bg-amber-500/10 text-amber-200'
  if (tone === 'danger') return 'border-red-500/25 bg-red-500/10 text-red-200'
  return 'border-[var(--border)] bg-[var(--bg-hover)] text-[var(--text-primary)]'
}

function metricAccentClass(tone?: string) {
  if (tone === 'good') return 'from-emerald-400/70 to-cyan-300/50'
  if (tone === 'warning') return 'from-amber-300/80 to-orange-300/50'
  if (tone === 'danger') return 'from-red-400/80 to-rose-300/50'
  return 'from-[var(--accent)]/80 to-[var(--accent-strong)]/45'
}

function DisplayMetricCard({ metric, compact = false }: { metric: any; compact?: boolean }) {
  return (
    <div className={`group relative overflow-hidden rounded-lg border px-3.5 py-3 ${metricToneClass(metric?.tone)}`}>
      <div className={`pointer-events-none absolute inset-y-0 left-0 w-1 bg-gradient-to-b ${metricAccentClass(metric?.tone)}`} />
      <div className="relative flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-[0.18em] opacity-65">{metric?.label || '-'}</div>
          {metric?.sub ? <div className="mt-1 truncate text-[11px] opacity-65">{metric.sub}</div> : null}
        </div>
        <div className={`${compact ? 'text-sm' : 'text-lg'} shrink-0 font-semibold tracking-[-0.03em]`}>{formatResultValue(metric?.value)}</div>
      </div>
      {typeof metric?.percent === 'number' ? (
        <div className="relative mt-3 h-1.5 overflow-hidden rounded-full bg-black/25">
          <div className={`h-full rounded-full bg-gradient-to-r ${metricAccentClass(metric?.tone)}`} style={{ width: `${Math.max(0, Math.min(100, metric.percent))}%` }} />
        </div>
      ) : null}
    </div>
  )
}

function DisplayWarnings({ warnings }: { warnings: any[] }) {
  if (!warnings.length) return null
  return (
    <div className="space-y-2">
      {warnings.map((item: any, index: number) => (
        <div key={`${item?.key || 'warning'}-${index}`} className={`rounded-xl border px-3 py-2 text-xs ${metricToneClass(item?.tone || 'warning')}`}>
          {item?.message || '-'}
        </div>
      ))}
    </div>
  )
}

function DisplaySections({ sections }: { sections: any[] }) {
  if (!sections.length) return null
  return (
    <div className="space-y-3">
      {sections.map((section: any) => (
        <div key={section?.key || section?.title} className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] p-3">
          <div className="text-xs font-semibold text-[var(--text-primary)]">{section?.title || '明细'}</div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {(Array.isArray(section?.items) ? section.items : []).map((item: any, index: number) => (
              <div key={`${item?.title || 'item'}-${index}`} className="rounded-lg border border-[var(--border)] bg-black/20 p-3">
                <div className="text-xs font-semibold text-[var(--text-primary)]">{item?.title || '-'}</div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-[var(--text-secondary)]">
                  {(Array.isArray(item?.metrics) ? item.metrics : []).map((metric: any) => (
                    <div key={metric?.key || metric?.label}>
                      <span className="text-[var(--text-muted)]">{metric?.label || '-'}: </span>
                      <span>{formatResultValue(metric?.value)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

function ActionResultHighlights({ payload }: { payload: any }) {
  if (!payload || typeof payload !== 'object') return null
  const { t } = useI18n()

  const stats: Array<{ label: string; value: any }> = []
  if ('valid' in payload) stats.push({ label: '账号有效', value: payload.valid })
  if (payload.membership_type) stats.push({ label: '套餐', value: payload.membership_type })
  if (payload.plan) stats.push({ label: '套餐', value: payload.plan })
  if (payload.plan_id) stats.push({ label: 'Plan ID', value: payload.plan_id })
  if (typeof payload.has_valid_payment_method === 'boolean') stats.push({ label: '已绑卡', value: payload.has_valid_payment_method })
  if ('trial_eligible' in payload) stats.push({ label: '可试用', value: payload.trial_eligible })
  if (payload.plus_trial_eligible === true) stats.push({ label: t('accounts.plusTrialEligible'), value: '是' })
  if (payload.trial_length_days) stats.push({ label: '试用天数', value: payload.trial_length_days })
  if (payload.remaining_credits) stats.push({ label: '剩余额度', value: payload.remaining_credits })
  if (payload.usage_total) stats.push({ label: '已用额度', value: payload.usage_total })
  if (payload.plan_credits) stats.push({ label: '总额度', value: payload.plan_credits })

  if (stats.length === 0) return null
  return (
    <div className="mb-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {stats.map(item => <ResultStat key={item.label} label={item.label} value={item.value} />)}
    </div>
  )
}

function hidePromotionLinks(payload: unknown): unknown {
  if (Array.isArray(payload)) return payload.map(hidePromotionLinks)
  if (!payload || typeof payload !== 'object') return payload
  return Object.fromEntries(
    Object.entries(payload)
      .filter(([key]) => !['url', 'cashier_url', 'cashierUrl', 'checkout_url', 'checkoutUrl'].includes(key))
      .map(([key, value]) => [key, hidePromotionLinks(value)]),
  )
}

function ActionResultModal({
  title,
  payload,
  onClose,
}: {
  title: string
  payload: any
  onClose: () => void
}) {
  const content = typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2)

  return (
    <div className="dialog-backdrop">
      <div
        className="dialog-panel dialog-panel-lg"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">{title}</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">操作结果</p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => navigator.clipboard.writeText(content)}>
              <Copy className="h-4 w-4 mr-1" />
              复制
            </Button>
            <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <div className="px-6 py-4">
          <ActionResultHighlights payload={payload} />
          <pre className="bg-[var(--bg-hover)] border border-[var(--border)] rounded-xl p-4 text-xs text-[var(--text-secondary)] whitespace-pre-wrap break-all overflow-auto max-h-[65vh]">
            {content}
          </pre>
        </div>
      </div>
    </div>
  )
}

function ActionTaskModal({
  title,
  taskId,
  taskStatus,
  onClose,
  onDone,
}: {
  title: string
  taskId: string
  taskStatus: string | null
  onClose: () => void
  onDone: (status: string) => void
}) {
  const { t, language } = useI18n()
  return (
    <div className="dialog-backdrop">
      <div
        className="dialog-panel flex w-[min(960px,calc(100vw-32px))] max-w-none flex-col overflow-hidden"
        onClick={e => e.stopPropagation()}
        style={{ maxHeight: '90vh' }}
      >
        <div className="relative overflow-hidden border-b border-[var(--border)] px-6 py-5">
          <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_12%_0%,rgba(9,182,162,0.18),transparent_34%),linear-gradient(90deg,rgba(255,255,255,0.04),transparent)]" />
          <div className="relative flex items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="mb-2 inline-flex rounded-full border border-[var(--border)] bg-[var(--chip-bg)] px-3 py-1 text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">
                Platform Action
              </div>
              <h2 className="truncate text-lg font-semibold text-[var(--text-primary)]">{title}</h2>
              <p className="mt-1 text-xs text-[var(--text-muted)]">任务状态、错误摘要与实时日志集中展示</p>
            </div>
            <div className="flex items-center gap-2">
              {taskStatus ? (
                <Badge variant={TASK_STATUS_VARIANTS[taskStatus] || 'secondary'}>
                  {getTaskStatusText(taskStatus, language)}
                </Badge>
              ) : null}
              <button onClick={onClose} className="rounded-full border border-[var(--border)] bg-[var(--bg-hover)] p-2 text-[var(--text-muted)] hover:text-[var(--text-primary)]">
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto px-6 py-5">
          <TaskLogPanel taskId={taskId} onDone={onDone} />
        </div>
        <div className="flex items-center justify-between border-t border-[var(--border)] px-6 py-3 text-xs text-[var(--text-muted)]">
          <span>{t('taskHistory.taskId')}: {taskId}</span>
          <Button variant="outline" size="sm" onClick={onClose}>
            {t('common.close')}
          </Button>
        </div>
      </div>
    </div>
  )
}

function BatchCheckProxyModal({
  defaultProxy,
  selectedCount,
  submitting,
  kind = 'status',
  onClose,
  onSubmit,
}: {
  defaultProxy: string
  selectedCount: number
  submitting: boolean
  kind?: 'status' | 'gcash' | 'trial'
  onClose: () => void
  onSubmit: (proxy: string, checkChain: boolean, country: string) => void | Promise<void>
}) {
  const { t } = useI18n()
  const [proxy, setProxy] = useState(defaultProxy)
  const [country, setCountry] = useState('PH')
  const [checkChain, setCheckChain] = useState(kind !== 'status')

  useEffect(() => {
    setProxy(defaultProxy)
    setCountry('PH')
    setCheckChain(kind !== 'status')
  }, [defaultProxy, kind])

  const normalizedProxy = proxy.trim()
  const selectedPreset = QUERY_PROXY_PRESETS.includes(normalizedProxy)
    ? normalizedProxy
    : '__manual__'

  return (
    <div className="dialog-backdrop">
      <div
        className="dialog-panel dialog-panel-md"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">
              {kind === 'gcash'
                ? t('accounts.gcashCheckProxyTitle')
                : kind === 'trial' ? t('accounts.trialCheckProxyTitle') : t('accounts.batchCheckProxyTitle')}
            </h2>
            <p className="mt-0.5 text-xs text-[var(--text-muted)]">
              {selectedCount > 0
                ? kind === 'gcash'
                  ? t('accounts.gcashCheckSelected', { count: selectedCount })
                  : t('accounts.batchCheckSelected', { count: selectedCount })
                : kind === 'gcash'
                  ? t('accounts.gcashCheck')
                  : kind === 'trial' ? t('accounts.trialCheck') : t('accounts.batchCheckAll')}
            </p>
          </div>
          <button
            onClick={onClose}
            disabled={submitting}
            className="text-[var(--text-muted)] hover:text-[var(--text-primary)] disabled:opacity-50"
            aria-label={t('common.close')}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="space-y-3 px-6 py-5">
          {kind === 'trial' && (
            <label className="block">
              <div className="mb-1.5 text-xs text-[var(--text-muted)]">{t('accounts.trialCheckCountry')}</div>
              <select
                value={country}
                onChange={event => setCountry(event.target.value)}
                disabled={submitting}
                className="w-full rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm text-[var(--text-primary)] outline-none focus:border-[var(--text-accent)] disabled:opacity-60"
              >
                {TRIAL_COUNTRY_OPTIONS.map(([code, label]) => (
                  <option key={code} value={code}>{label} ({code})</option>
                ))}
              </select>
            </label>
          )}
          <label className="block">
            <div className="mb-1.5 text-xs text-[var(--text-muted)]">
              {kind === 'gcash'
                ? t('accounts.gcashCheckProxyLabel')
                : kind === 'trial' ? t('accounts.trialCheckProxyLabel') : t('accounts.batchCheckProxyLabel')}
            </div>
            <div className="flex flex-col gap-2 sm:flex-row">
              <select
                value={selectedPreset}
                onChange={event => {
                  const next = event.target.value === '__manual__' ? '' : event.target.value
                  setProxy(next)
                }}
                disabled={submitting}
                className="w-full shrink-0 rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm text-[var(--text-primary)] outline-none focus:border-[var(--text-accent)] disabled:opacity-60 sm:w-[190px]"
              >
                {QUERY_PROXY_PRESETS.map(option => (
                  <option key={option} value={option}>{option}</option>
                ))}
                <option value="__manual__">{t('accounts.batchCheckProxyManual')}</option>
              </select>
              <input
                type="text"
                value={proxy}
                onChange={event => setProxy(event.target.value)}
                disabled={submitting}
                placeholder="http://127.0.0.1:7897"
                autoFocus
                className="min-w-0 flex-1 rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm text-[var(--text-primary)] outline-none focus:border-[var(--text-accent)] disabled:opacity-60"
              />
            </div>
          </label>
          <p className="text-xs text-[var(--text-muted)]">
            {kind === 'gcash'
              ? t('accounts.gcashCheckProxyHint')
              : kind === 'trial' ? t('accounts.trialCheckProxyHint') : t('accounts.batchCheckProxyHint')}
          </p>
          {kind !== 'gcash' && (
            <label className="flex items-center gap-2 text-xs text-[var(--text-secondary)]">
              <input
                type="checkbox"
                checked={checkChain}
                onChange={event => setCheckChain(event.target.checked)}
                disabled={submitting}
                className="h-4 w-4 accent-[var(--text-accent)]"
              />
              {kind === 'trial' ? t('accounts.trialCheckChain') : t('accounts.batchCheckChain')}
            </label>
          )}
        </div>
        <div className="flex gap-3 border-t border-[var(--border)] px-6 py-4">
          <Button
            onClick={() => onSubmit(normalizedProxy, checkChain, country)}
            disabled={submitting || !normalizedProxy}
            className="flex-1"
          >
            {submitting
              ? t('accounts.batchChecking')
              : kind === 'gcash'
                ? t('accounts.gcashCheckStart')
                : kind === 'trial' ? t('accounts.trialCheckStart') : t('accounts.batchCheckProxyStart')}
          </Button>
          <Button variant="outline" onClick={onClose} disabled={submitting} className="flex-1">
            {t('common.cancel')}
          </Button>
        </div>
      </div>
    </div>
  )
}

function ActionParamsModal({
  action,
  initialValues,
  submitting,
  onClose,
  onSubmit,
}: {
  action: any
  initialValues: Record<string, string>
  submitting: boolean
  onClose: () => void
  onSubmit: (params: Record<string, string>) => void
}) {
  const { t } = useI18n()
  const [form, setForm] = useState<Record<string, string>>(initialValues)

  useEffect(() => {
    setForm(action?.id === 'query_state'
      ? { ...initialValues, check_chain: initialValues.check_chain ?? 'false' }
      : initialValues)
  }, [action?.id, initialValues])

  const params = Array.isArray(action?.params) ? action.params : []
  const missingRequired = params.some((param: any) => param?.required && !String(form[param.key] ?? '').trim())

  return (
    <div className="dialog-backdrop">
      <div
        className="dialog-panel dialog-panel-md"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">{action?.label || '动作参数'}</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">填写执行该动作所需的参数</p>
          </div>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="px-6 py-4 space-y-4">
          {params.map((param: any) => {
            const value = form[param.key] ?? ''
            const isProxyParam = param.key === 'proxy' || param.type === 'proxy'
            if (isProxyParam) {
              const proxyOptions = (Array.isArray(param.options) && param.options.length > 0
                ? param.options
                : QUERY_PROXY_PRESETS
              ).map((option: unknown) => String(option))
              const selectedProxy = proxyOptions.includes(String(value)) ? String(value) : '__manual__'
              return (
                <label key={param.key} className="block">
                  <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                  <div className="flex gap-2">
                    <select
                      value={selectedProxy}
                      onChange={e => {
                        const nextValue = e.target.value === '__manual__' ? '' : e.target.value
                        setForm(current => ({ ...current, [param.key]: nextValue }))
                      }}
                      className="w-[190px] shrink-0 rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                    >
                      {proxyOptions.map((option: string) => (
                        <option key={option} value={option}>{option}</option>
                      ))}
                      <option value="__manual__">手动填写</option>
                    </select>
                    <input
                      type="text"
                      value={value}
                      placeholder={param.placeholder || undefined}
                      onChange={e => setForm(current => ({ ...current, [param.key]: e.target.value }))}
                      className="min-w-0 flex-1 rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                    />
                  </div>
                </label>
              )
            }
            if (param.type === 'checkbox') {
              return (
                <label key={param.key} className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
                  <input
                    type="checkbox"
                    checked={String(value).toLowerCase() === 'true' || String(value) === '1'}
                    onChange={event => setForm(current => ({ ...current, [param.key]: String(event.target.checked) }))}
                    className="h-4 w-4 accent-[var(--text-accent)]"
                  />
                  {param.label || param.key}
                </label>
              )
            }
            if (param.type === 'country') {
              return (
                <label key={param.key} className="block">
                  <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                  <select
                    value={value}
                    onChange={event => setForm(current => ({ ...current, [param.key]: event.target.value }))}
                    className="w-full rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                  >
                    {(Array.isArray(param.options) ? param.options : ['PH']).map((option: string) => (
                      <option key={option} value={option}>{TRIAL_COUNTRY_LABELS[option] || option} ({option})</option>
                    ))}
                  </select>
                </label>
              )
            }
            if (Array.isArray(param.options) && param.options.length > 0) {
              return (
                <label key={param.key} className="block">
                  <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                  <select
                    value={value}
                    onChange={e => setForm(current => ({ ...current, [param.key]: e.target.value }))}
                    className="w-full rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                  >
                    {param.options.map((option: string) => (
                      <option key={option} value={option}>{option}</option>
                    ))}
                  </select>
                </label>
              )
            }
            if (param.type === 'textarea') {
              return (
                <label key={param.key} className="block">
                  <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                  <textarea
                    value={value}
                    placeholder={param.placeholder || undefined}
                    onChange={e => setForm(current => ({ ...current, [param.key]: e.target.value }))}
                    rows={3}
                    className="w-full rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                  />
                </label>
              )
            }
            return (
              <label key={param.key} className="block">
                <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                <input
                  type={param.type === 'number' ? 'number' : 'text'}
                  value={value}
                  placeholder={param.placeholder || undefined}
                  onChange={e => setForm(current => ({ ...current, [param.key]: e.target.value }))}
                  className="w-full rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                />
              </label>
            )
          })}
          {action?.id === 'query_state' && (
            <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
              <input
                type="checkbox"
                checked={String(form.check_chain || '').toLowerCase() === 'true' || String(form.check_chain || '') === '1'}
                onChange={event => setForm(current => ({ ...current, check_chain: String(event.target.checked) }))}
                className="h-4 w-4 accent-[var(--text-accent)]"
              />
              {t('accounts.batchCheckChain')}
            </label>
          )}
        </div>
        <div className="relative px-6 py-4 border-t border-[var(--border)] flex gap-3">
          {missingRequired && (
            <div className="absolute left-6 -translate-y-7 text-[11px] text-[#f0b0b0]">请填写必填参数</div>
          )}
          <Button
            onClick={() => onSubmit(action?.id === 'query_state' ? { ...form, check_chain: form.check_chain ?? 'false' } : form)}
            disabled={submitting || missingRequired}
            className="flex-1"
          >
            {submitting ? '执行中...' : action?.id === 'query_state' ? '开始查询' : '执行'}
          </Button>
          <Button variant="outline" onClick={onClose} disabled={submitting} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}
// ── 行操作菜单 ─────────────────────────────────────────────
function ActionMenu({
  acc,
  onDetail,
  onDelete,
  onResult,
  onChanged,
}: {
  acc: any
  onDetail: () => void
  onDelete: () => void
  onResult: (title: string, payload: any) => void
  onChanged: () => void
}) {
  const { language } = useI18n()
  const [open, setOpen] = useState(false)
  const [actions, setActions] = useState<any[]>([])
  const [running, setRunning] = useState<string | null>(null)
  const [toast, setToast] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const [actionTask, setActionTask] = useState<{ taskId: string; title: string } | null>(null)
  const [actionTaskStatus, setActionTaskStatus] = useState<string | null>(null)
  const [pendingAction, setPendingAction] = useState<{ action: any; params: Record<string, string> } | null>(null)
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0, maxHeight: 320 })
  const menuRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)

  const runAction = (action: any, params: Record<string, any>) => {
    setRunning(action.id)
    setActionTaskStatus(null)
    apiFetch(`/actions/chatgpt/${acc.id}/${action.id}`, { method: 'POST', body: JSON.stringify({ params }) })
      .then(resp => {
        if (resp?.sync) {
          setRunning(null)
          if (!resp.ok) {
            setToast({ type: 'error', text: resp.error || 'Operation failed' })
            return
          }
          onChanged()
          onResult(action.label, hidePromotionLinks(resp.data))
          return
        }
        setActionTask({
          taskId: resp.task_id,
          title: `${acc.email} · ${action.label}`,
        })
      })
      .catch(() => {
        setRunning(null)
        setToast({ type: 'error', text: 'Request failed' })
      })
  }

  const startAction = (action: any) => {
    if (!action) return
    if (Array.isArray(action.params) && action.params.length > 0) {
      setPendingAction({
        action,
        params: buildActionParamDraft(action, acc),
      })
      return
    }
    runAction(action, {})
  }

  const updateMenuPosition = useCallback(() => {
    const trigger = triggerRef.current
    if (!trigger) return

    const rect = trigger.getBoundingClientRect()
    const viewportPadding = 12
    const menuWidth = 220
    const estimatedHeight = Math.min(320, actions.length * 40 + 56)

    let left = rect.right - menuWidth
    if (left < viewportPadding) left = viewportPadding
    if (left + menuWidth > window.innerWidth - viewportPadding) {
      left = Math.max(viewportPadding, window.innerWidth - menuWidth - viewportPadding)
    }

    let top = rect.bottom + 8
    if (top + estimatedHeight > window.innerHeight - viewportPadding) {
      top = Math.max(viewportPadding, rect.top - estimatedHeight - 8)
    }

    setMenuPosition({
      top: Math.round(top),
      left: Math.round(left),
      maxHeight: Math.max(160, window.innerHeight - viewportPadding * 2),
    })
  }, [actions.length])

  useEffect(() => {
    let active = true
    loadPlatformActions('chatgpt')
      .then((items) => {
        if (active) setActions(items)
      })
      .catch(() => {
        if (active) setActions([])
      })
    return () => {
      active = false
    }
  }, [])
  useEffect(() => {
    if (toast) { const t = setTimeout(() => setToast(null), 4000); return () => clearTimeout(t) }
  }, [toast])
  useEffect(() => {
    if (!open) return
    let active = true
    loadPlatformActions('chatgpt', { force: true })
      .then((items) => {
        if (active) setActions(items)
      })
      .catch(() => {
        if (active) setActions([])
      })
    updateMenuPosition()
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return
      setOpen(false)
    }
    const reposition = () => updateMenuPosition()
    document.addEventListener('mousedown', handler)
    window.addEventListener('resize', reposition)
    window.addEventListener('scroll', reposition, true)
    return () => {
      active = false
      document.removeEventListener('mousedown', handler)
      window.removeEventListener('resize', reposition)
      window.removeEventListener('scroll', reposition, true)
    }
  }, [open, updateMenuPosition])

  const handleActionDone = async (status: string) => {
    if (!actionTask) return
    setActionTaskStatus(status)
    setRunning(null)
    try {
      const task = await apiFetch(`/tasks/${actionTask.taskId}`)
      const data = task?.data ?? task?.result?.data
      if (status !== 'succeeded') {
        setToast({ type: 'error', text: task?.error || getTaskStatusText(status, language) })
        return
      }
      onChanged()
      const visibleData = hidePromotionLinks(data)
      if (visibleData && typeof visibleData === 'object' && !Array.isArray(visibleData)) {
        const visibleRecord = visibleData as Record<string, unknown>
        const detailKeys = Object.keys(visibleRecord).filter(key => key !== 'message')
        if (detailKeys.length > 0) {
          onResult(actionTask.title, visibleRecord)
        }
        setToast({
          type: 'success',
          text: typeof visibleRecord.message === 'string' && visibleRecord.message
            ? visibleRecord.message
            : '操作成功',
        })
        return
      }
      setToast({ type: 'success', text: typeof visibleData === 'string' && visibleData ? visibleData : '操作成功' })
    } catch (error: any) {
      setToast({ type: 'error', text: error?.message || '读取任务结果失败' })
    }
  }

  const copyAccessToken = async () => {
    const accessToken = getAccessToken(acc)
    if (!accessToken) {
      setToast({ type: 'error', text: '该账号没有 access_token' })
      return
    }
    try {
      await copyToClipboard(accessToken)
      setToast({ type: 'success', text: 'access_token 已复制' })
    } catch (error: any) {
      setToast({ type: 'error', text: error?.message || '复制 access_token 失败' })
    }
  }

  return (
    <div className="relative flex w-full min-w-0 flex-wrap items-center justify-end gap-0.5 whitespace-nowrap">
      {toast && (
        <div
          className="fixed top-5 right-5 z-[70] flex items-center gap-2.5 rounded-xl border px-4 py-3 text-[13px] font-medium shadow-lg  cursor-pointer transition-all"
          style={{
            background: toast.type === 'success' ? 'rgba(16,185,129,0.12)' : 'rgba(239,68,68,0.12)',
            borderColor: toast.type === 'success' ? 'rgba(16,185,129,0.25)' : 'rgba(239,68,68,0.25)',
            color: toast.type === 'success' ? '#6ee7b7' : '#fca5a5',
          }}
          onClick={() => setToast(null)}
        >
          <span className="text-base">{toast.type === 'success' ? '✓' : '✗'}</span>
          <span>{toast.text}</span>
        </div>
      )}
      {actionTask && (
        <ActionTaskModal
          title={actionTask.title}
          taskId={actionTask.taskId}
          taskStatus={actionTaskStatus}
          onClose={() => {
            setActionTask(null)
            setActionTaskStatus(null)
          }}
          onDone={handleActionDone}
        />
      )}
      {pendingAction && (
        <ActionParamsModal
          action={pendingAction.action}
          initialValues={pendingAction.params}
          submitting={running === pendingAction.action?.id}
          onClose={() => {
            if (!running) setPendingAction(null)
          }}
          onSubmit={(params) => {
            const action = pendingAction.action
            setPendingAction(null)
            runAction(action, params)
          }}
        />
      )}
      <button
        type="button"
        onClick={() => { void copyAccessToken() }}
        className="table-action-btn"
        title="复制 access_token"
        aria-label={`复制 ${acc.email} 的 access_token`}
      >
        <KeyRound size={14} />
      </button>
      <button onClick={onDetail} className="table-action-btn" title="账号详情" aria-label={`查看 ${acc.email} 的详情`}><Eye size={14} /></button>
      {actions.some(action => action.id === 'rebind_email') && (
        <button
          onClick={() => startAction(actions.find(action => action.id === 'rebind_email'))}
          disabled={!!running}
          className="table-action-btn"
          title="换绑邮箱"
        >
          {running === 'rebind_email' ? '换绑中...' : '换绑'}
        </button>
      )}
      {actions.length > 0 && (
        <div className="relative">
          <button ref={triggerRef} onClick={() => setOpen(o => !o)}
            className="table-action-btn" title="更多操作" aria-label="更多操作" aria-expanded={open}><MoreHorizontal size={14} /></button>
          {open && typeof document !== 'undefined' && createPortal(
            <div
              ref={menuRef}
              className="fixed z-[60] w-[220px] overflow-y-auto rounded-2xl border border-[var(--border)] bg-[var(--bg-card)]/96 py-1.5 shadow-[var(--shadow-soft)] "
              style={{ top: menuPosition.top, left: menuPosition.left, maxHeight: menuPosition.maxHeight }}
            >
              {actions.map(a => (
                <button key={a.id}
                  onClick={() => {
                    setOpen(false)
                    startAction(a)
                  }}
                  disabled={!!running}
                  className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] disabled:opacity-50">
                  {running === a.id ? '执行中...' : a.label}
                </button>
              ))}
              <div className="my-1 border-t border-[var(--border)]/70" />
              <button
                onClick={() => {
                  setOpen(false)
                  if (confirm(`确认删除 ${acc.email}？`)) {
                    apiFetch(`/accounts/${acc.id}`, { method: 'DELETE' }).then(onDelete)
                  }
                }}
                className="w-full px-3 py-2 text-left text-xs text-[#f0b0b0] transition-colors hover:bg-[rgba(239,68,68,0.08)] hover:text-[#ffd5d5]"
              >
                删除
              </button>
            </div>,
            document.body,
          )}
        </div>
      )}
      {actions.length === 0 && (
        <button
          onClick={() => { if (confirm(`确认删除 ${acc.email}？`)) apiFetch(`/accounts/${acc.id}`, { method: 'DELETE' }).then(onDelete) }}
          className="table-action-btn table-action-btn-danger" title="删除账号" aria-label={`删除 ${acc.email}`}
        >
          <Trash2 size={14} />
        </button>
      )}
    </div>
  )
}

// ── 账号详情弹框 ───────────────────────────────────────────
function DetailModal({ acc, onClose, onSave }: { acc: any; onClose: () => void; onSave: () => void }) {
  const { t } = useI18n()
  const returnFocusRef = useRef(document.activeElement as HTMLElement | null)
  const [form, setForm] = useState({
    lifecycle_status: getLifecycleStatus(acc),
    primary_token: getPrimaryToken(acc),
  })
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')
  const overview = getAccountOverview(acc)
  const verificationMailbox = getVerificationMailbox(acc)
  const providerAccounts = getProviderAccounts(acc)
  const credentials = getCredentials(acc)
  const primaryMetrics = getPrimaryMetrics(acc)
  const secondaryMetrics = getSecondaryMetrics(acc)
  const warnings = getDisplayWarnings(acc)
  const displayBadges = getDisplayBadges(acc)
  const displaySections = getDisplaySections(acc)
  const copyText = (text: string) => navigator.clipboard.writeText(text)
  const securitySettings = getSecuritySettings(acc)
  const platformCredentials = credentials.filter((item: any) => (
    item.scope === 'platform' && !['totp_secret', 'otpauth'].includes(item.key)
  ))

  const save = async () => {
    setSaving(true)
    try {
      await apiFetch(`/accounts/${acc.id}`, { method: 'PATCH', body: JSON.stringify(form) })
      onSave()
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : String(error))
    } finally { setSaving(false) }
  }

  return (
    <Dialog.Root open onOpenChange={open => { if (!open) onClose() }}>
      <Dialog.Portal>
        <Dialog.Overlay className="detail-scrim" />
        <Dialog.Content className="account-detail-drawer flex flex-col" onCloseAutoFocus={event => { event.preventDefault(); returnFocusRef.current?.focus() }}>
        {/* ── Sticky Header ── */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)] shrink-0">
          <AccountAvatar email={acc.email || ''} validity={getValidityStatus(acc)} small />
          <div className="min-w-0">
            <Dialog.Title className="text-base font-semibold text-[var(--text-primary)]">账号详情</Dialog.Title>
            <Dialog.Description className="text-xs text-[var(--text-muted)] mt-1 break-all">{acc.email}</Dialog.Description>
          </div>
          <Dialog.Close className="icon-control" title={t('common.close')} aria-label={t('common.close')}><X className="h-4 w-4" /></Dialog.Close>
        </div>
        {/* ── Scrollable Content ── */}
        <div className="px-6 py-4 space-y-3 flex-1 overflow-y-auto min-h-0">
          {saveError && <p className="inline-error" role="alert">{saveError}</p>}
          <div className="detail-status-section">
            <div className="relative flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="text-xs uppercase tracking-[0.18em] text-[var(--text-muted)]">核心状态</div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <Badge variant={STATUS_VARIANT[getDisplayStatus(acc)] || 'secondary'}>{getDisplayStatus(acc)}</Badge>
                  <span className="text-lg font-semibold tracking-[-0.03em] text-[var(--text-primary)]">{acc.plan_name || overview.plan_name || overview.plan || getPlanState(acc)}</span>
                  {overview?.plus_trial_eligible === true && (
                    <span className="inline-flex items-center rounded-full bg-violet-500/10 px-2 py-0.5 text-xs font-medium text-violet-500 ring-1 ring-inset ring-violet-500/20">
                      {t('accounts.plusTrialEligible')}
                    </span>
                  )}
                </div>
              </div>
              <div className="grid grid-cols-2 gap-2 text-right text-[11px] text-[var(--text-muted)] sm:grid-cols-3">
                <div className="rounded-xl border border-[var(--border-soft)] bg-black/10 px-2.5 py-2">
                  <div className="uppercase tracking-[0.12em]">生命周期</div>
                  <div className="mt-1 text-[var(--text-primary)]">{getLifecycleStatus(acc)}</div>
                </div>
                <div className="rounded-xl border border-[var(--border-soft)] bg-black/10 px-2.5 py-2">
                  <div className="uppercase tracking-[0.12em]">有效性</div>
                  <div className="mt-1 text-[var(--text-primary)]">{getValidityStatus(acc)}</div>
                </div>
                <div className="rounded-xl border border-[var(--border-soft)] bg-black/10 px-2.5 py-2">
                  <div className="uppercase tracking-[0.12em]">套餐状态</div>
                  <div className="mt-1 text-[var(--text-primary)]">{getPlanState(acc)}</div>
                </div>
              </div>
            </div>
            {secondaryMetrics.length > 0 && (
              <div className="relative mt-4 grid gap-2 sm:grid-cols-2">
                {secondaryMetrics.slice(0, 4).map((metric: any) => (
                  <DisplayMetricCard key={metric.key || metric.label} metric={metric} compact />
                ))}
              </div>
            )}
          </div>

          {primaryMetrics.length > 0 && (
            <div className="grid gap-3 sm:grid-cols-2">
              {primaryMetrics.map((metric: any) => (
                <DisplayMetricCard key={metric.key || metric.label} metric={metric} />
              ))}
            </div>
          )}

          <DisplayWarnings warnings={warnings} />
          <DisplaySections sections={displaySections} />

          <div className="space-y-3 rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <label className="text-xs font-semibold text-[var(--text-primary)]">安全设置</label>
              <div className="flex flex-wrap gap-1.5 text-[11px]">
                <span className="rounded-full border border-[var(--border)] px-2 py-0.5 text-[var(--text-secondary)]">
                  密码: {securitySettings.passwordSet ? '已设置' : '未设置'}
                </span>
                <span className="rounded-full border border-[var(--border)] px-2 py-0.5 text-[var(--text-secondary)]">
                  2FA: {securitySettings.totpSet ? '已启用' : '未设置'}
                </span>
              </div>
            </div>
            <div className="grid gap-2">
              <div>
                <div className="text-[11px] text-[var(--text-muted)]">ChatGPT 密码</div>
                <div className="mt-1 flex items-start gap-1">
                  <div className="flex-1 rounded-md border border-[var(--border)] bg-black/20 px-2 py-1.5 text-xs font-mono text-[var(--text-secondary)] break-all">
                    {securitySettings.password || '未记录'}
                  </div>
                  {securitySettings.password && (
                    <button
                      title="复制密码"
                      aria-label="复制密码"
                      onClick={() => copyText(securitySettings.password)}
                      className="mt-1 shrink-0 text-[var(--text-muted)] hover:text-[var(--text-secondary)]"
                    >
                      <Copy className="h-3 w-3" />
                    </button>
                  )}
                </div>
              </div>
              <div>
                <div className="text-[11px] text-[var(--text-muted)]">TOTP Secret</div>
                <div className="mt-1 flex items-start gap-1">
                  <div className="flex-1 rounded-md border border-[var(--border)] bg-black/20 px-2 py-1.5 text-xs font-mono text-[var(--text-secondary)] break-all">
                    {securitySettings.totpSecret || (securitySettings.totpSet ? '已启用，未返回密钥' : '未记录')}
                  </div>
                  {securitySettings.totpSecret && (
                    <button
                      title="复制 TOTP Secret"
                      aria-label="复制 TOTP Secret"
                      onClick={() => copyText(securitySettings.totpSecret)}
                      className="mt-1 shrink-0 text-[var(--text-muted)] hover:text-[var(--text-secondary)]"
                    >
                      <Copy className="h-3 w-3" />
                    </button>
                  )}
                </div>
              </div>
              <div>
                <div className="text-[11px] text-[var(--text-muted)]">otpauth URI</div>
                <div className="mt-1 flex items-start gap-1">
                  <div className="flex-1 rounded-md border border-[var(--border)] bg-black/20 px-2 py-1.5 text-xs font-mono text-[var(--text-secondary)] break-all max-h-32 overflow-y-auto">
                    {securitySettings.otpauth || (securitySettings.totpSet ? '已启用，未返回 URI' : '未记录')}
                  </div>
                  {securitySettings.otpauth && (
                    <button
                      title="复制 otpauth URI"
                      aria-label="复制 otpauth URI"
                      onClick={() => copyText(securitySettings.otpauth)}
                      className="mt-1 shrink-0 text-[var(--text-muted)] hover:text-[var(--text-secondary)]"
                    >
                      <Copy className="h-3 w-3" />
                    </button>
                  )}
                </div>
              </div>
            </div>
          </div>

          {(displayBadges.length > 0 || verificationMailbox?.email) && (
            <div className="space-y-2">
              {displayBadges.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {displayBadges.map((badge: any, index: number) => (
                    <span key={`${badge?.label || 'badge'}-${index}`} className="rounded-full border border-[var(--border)] bg-[var(--bg-hover)] px-2 py-0.5 text-[11px] text-[var(--text-secondary)]">
                      {badge?.label}
                    </span>
                  ))}
                </div>
              )}
              {verificationMailbox?.email && (
                <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-xs text-[var(--text-secondary)]">
                  验证码邮箱: {verificationMailbox.email} · {verificationMailbox.provider || '-'} · ID {verificationMailbox.account_id || '-'}
                </div>
              )}
            </div>
          )}
          {providerAccounts.length > 0 && (
            <div className="space-y-2">
              <label className="text-xs text-[var(--text-muted)] block">Provider Accounts</label>
              {providerAccounts.map((item: any, index: number) => (
                <div key={`${item.provider_name || 'provider'}-${item.login_identifier || index}`} className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] p-3">
                  <div className="text-xs font-semibold text-[var(--text-primary)]">
                    {item.provider_name || item.provider_type || 'provider'}
                  </div>
                  <div className="mt-1 text-xs text-[var(--text-secondary)] break-all">
                    登录标识: {item.login_identifier || '-'}
                  </div>
                  {item.credentials && Object.keys(item.credentials).length > 0 && (
                    <div className="mt-2 grid gap-2">
                      {Object.entries(item.credentials).map(([key, value]: [string, any]) => (
                        <div key={key}>
                          <div className="text-[11px] text-[var(--text-muted)]">{key}</div>
                          <div className="flex items-start gap-1">
                            <div className="flex-1 rounded-md border border-[var(--border)] bg-black/20 px-2 py-1.5 text-xs font-mono text-[var(--text-secondary)] break-all max-h-40 overflow-y-auto">
                              {String(value || '-')}
                            </div>
                            {value ? (
                              <button onClick={() => copyText(String(value))} className="mt-1 shrink-0 text-[var(--text-muted)] hover:text-[var(--text-secondary)]">
                                <Copy className="h-3 w-3" />
                              </button>
                            ) : null}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
          {platformCredentials.length > 0 && (
            <div className="space-y-2">
              <label className="text-xs text-[var(--text-muted)] block">Platform Credentials</label>
              {platformCredentials.map((item: any) => (
                <div key={`${item.scope}-${item.key}`} className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] p-3">
                  <div className="text-[11px] text-[var(--text-muted)]">{item.key}</div>
                  <div className="mt-1 flex items-start gap-1">
                    <div className="flex-1 rounded-md border border-[var(--border)] bg-black/20 px-2 py-1.5 text-xs font-mono text-[var(--text-secondary)] break-all max-h-40 overflow-y-auto">
                      {item.value}
                    </div>
                    <button onClick={() => copyText(String(item.value || ''))} className="mt-1 shrink-0 text-[var(--text-muted)] hover:text-[var(--text-secondary)]">
                      <Copy className="h-3 w-3" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
          <div>
            <label className="text-xs text-[var(--text-muted)] block mb-1">生命周期状态</label>
            <select value={form.lifecycle_status} onChange={e => setForm(f => ({ ...f, lifecycle_status: e.target.value }))}
              className="control-surface appearance-none">
              {['registered','trial','subscribed','expired','invalid'].map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs text-[var(--text-muted)] block mb-1">主凭证</label>
            <textarea value={form.primary_token} onChange={e => setForm(f => ({ ...f, primary_token: e.target.value }))}
              rows={2} className="control-surface control-surface-mono resize-none" />
          </div>
        </div>
        {/* ── Sticky Footer ── */}
        <div className="flex gap-3 px-6 py-4 border-t border-[var(--border)] shrink-0">
          <Button onClick={save} disabled={saving} className="flex-1">{saving ? '保存中...' : '保存'}</Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}

// ── 导入弹框 ────────────────────────────────────────────────
function ImportModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const submit = async () => {
    const lines = text.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
    if (lines.length === 0) {
      setResult({ type: 'error', text: '请先填写要导入的账号' })
      return
    }
    const invalidLineIndex = lines.findIndex(line => {
      const parts = line.split('----')
      if (parts.length !== 3) return true
      const [email, password, totpSecret] = parts.map(part => part.trim())
      return !email || !email.includes('@') || /\s/.test(email) || !password || !totpSecret
    })
    if (invalidLineIndex >= 0) {
      setResult({ type: 'error', text: `第 ${invalidLineIndex + 1} 行格式不正确，请使用 邮箱----密码----totp` })
      return
    }
    setLoading(true)
    setResult(null)
    try {
      const res = await apiFetch('/accounts/import', { method: 'POST', body: JSON.stringify({ platform: 'chatgpt', lines }) })
      const created = Number(res?.created || 0)
      if (created <= 0) {
        setResult({ type: 'error', text: '没有账号被导入，请确认后端已更新并检查输入格式' })
        return
      }
      setResult({ type: 'success', text: `导入成功 ${created} 个` })
      onDone()
    } catch (e: any) {
      setResult({ type: 'error', text: `失败: ${e.message}` })
    } finally {
      setLoading(false)
    }
  }
  return (
    <div className="dialog-backdrop">
      <div className="dialog-panel dialog-panel-sm p-6" onClick={e => e.stopPropagation()}>
        <h2 className="text-base font-semibold text-[var(--text-primary)] mb-2">批量导入</h2>
        <p className="text-xs text-[var(--text-muted)] mb-3">每行格式: <code className="bg-[var(--bg-hover)] px-1 rounded">邮箱----密码----totp</code></p>
        <textarea value={text} onChange={e => setText(e.target.value)} rows={8}
          className="control-surface control-surface-mono resize-none mb-3" />
        {result && (
          <p className={`mb-3 text-sm ${result.type === 'success' ? 'text-emerald-400' : 'text-red-400'}`}>
            {result.text}
          </p>
        )}
        <div className="flex gap-2">
          <Button onClick={submit} disabled={loading} className="flex-1">{loading ? '导入中...' : '导入'}</Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}

function ExportMenu({
  total,
  statusFilter,
  searchFilter,
  selectedIds,
}: {
  total: number
  statusFilter: string
  searchFilter: string
  selectedIds: number[]
}) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState<string | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const hasSelection = selectedIds.length > 0

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const doExport = async (format: string) => {
    setLoading(format)
    try {
      const { blob, filename } = await apiDownload(`/accounts/export/${format}`, {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids: hasSelection ? selectedIds : [],
          select_all: !hasSelection,
          status_filter: !hasSelection ? statusFilter || null : null,
          search_filter: !hasSelection ? searchFilter || null : null,
        }),
      })
      triggerBrowserDownload(blob, filename)
      setOpen(false)
    } catch (e: any) {
      window.alert(e?.message || '导出失败')
    } finally {
      setLoading(null)
    }
  }

  const options = [
    { key: 'json', label: '导出 JSON' },
    { key: 'csv', label: '导出 CSV' },
    { key: 'any2api', label: '导出 Any2Api' },
    { key: 'sub2api', label: '导出 Sub2Api' },
    { key: 'sub2api-agent-identity', label: '导出 Agent Identity (Sub2Api)' },
    { key: 'cpa', label: '导出 CPA' },
  ]

  return (
    <div className="relative" ref={menuRef}>
      <Button
        variant="outline"
        size="sm"
        onClick={() => setOpen(v => !v)}
        disabled={total === 0 || !!loading}
        className={ACCOUNT_TOOL_BUTTON_CLASS}
      >
        <Download className="h-4 w-4 mr-1 shrink-0" />
        {loading ? '导出中...' : hasSelection ? `导出已选(${selectedIds.length})` : '导出'}
      </Button>
      {open && (
        <div className="absolute right-0 top-10 z-20 min-w-[148px] rounded-lg border border-[var(--border)] bg-[var(--bg-card)] py-1 shadow-lg">
          <div className="px-3 py-1 text-[11px] text-[var(--text-muted)]">
            {hasSelection ? `导出 ${selectedIds.length} 个已选账号` : '导出当前筛选结果'}
          </div>
          {options.map(option => (
            <button
              key={option.key}
              onClick={() => doExport(option.key)}
              className="w-full px-3 py-1.5 text-left text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
            >
              {option.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function LoginRefreshMenu({
  total,
  statusFilter,
  searchFilter,
  selectedIds,
  onTaskCreated,
}: {
  total: number
  statusFilter: string
  searchFilter: string
  selectedIds: number[]
  onTaskCreated?: (taskId: string, title: string) => void
}) {
  const { t } = useI18n()
  const [showPreview, setShowPreview] = useState(false)
  const [loading, setLoading] = useState(false)
  const [preview, setPreview] = useState<any>(null)
  const [proxy, setProxy] = useState('http://127.0.0.1:7897')
  const hasSelection = selectedIds.length > 0

  const openPreview = async () => {
    setLoading(true)
    try {
      const data = await apiFetch('/login-refresh/preview', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids: hasSelection ? selectedIds : [],
          select_all: !hasSelection,
          status_filter: !hasSelection ? statusFilter || null : null,
          search_filter: !hasSelection ? searchFilter || null : null,
          proxy: proxy,
        }),
      })
      setPreview(data)
      setShowPreview(true)
    } catch (e: any) {
      window.alert(e?.message || t('accounts.loginRefreshStartFailed'))
    } finally {
      setLoading(false)
    }
  }

  const startRefresh = async () => {
    setLoading(true)
    try {
      const result = await apiFetch('/login-refresh/create', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids: hasSelection ? selectedIds : [],
          select_all: !hasSelection,
          status_filter: !hasSelection ? statusFilter || null : null,
          search_filter: !hasSelection ? searchFilter || null : null,
          proxy: proxy,
        }),
      })
      setShowPreview(false)
      setPreview(null)
      if (result?.task_id && onTaskCreated) {
        const title = hasSelection
          ? t('accounts.loginRefreshSelected', { count: selectedIds.length })
          : t('accounts.loginRefresh')
        onTaskCreated(result.task_id, title)
      }
    } catch (e: any) {
      window.alert(e?.message || t('accounts.loginRefreshStartFailed'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <Button
        variant="outline"
        size="sm"
        onClick={openPreview}
        disabled={total === 0 || loading}
        className={ACCOUNT_TOOL_BUTTON_CLASS}
      >
        <LogIn className="h-4 w-4 mr-1 shrink-0" />
        {loading ? t('accounts.loginRefreshing') : hasSelection ? t('accounts.loginRefreshSelected', { count: selectedIds.length }) : t('accounts.loginRefresh')}
      </Button>

      {showPreview && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="relative w-full max-w-2xl rounded-xl border border-[var(--border)] bg-[var(--bg-card)] shadow-2xl">
            <div className="flex items-center justify-between border-b border-[var(--border)] px-6 py-4">
              <h2 className="text-lg font-semibold text-[var(--text-primary)]">{t('accounts.loginRefreshPreviewTitle')}</h2>
              <button
                onClick={() => setShowPreview(false)}
                className="rounded-lg p-1 text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
              >
                <X className="h-5 w-5" />
              </button>
            </div>
            <div className="max-h-[60vh] overflow-y-auto px-6 py-4">
              <div className="mb-4">
                <label className="block text-sm font-medium text-[var(--text-primary)] mb-2">
                  {t('accounts.loginRefreshProxy')}
                </label>
                <input
                  type="text"
                  value={proxy}
                  onChange={(e) => setProxy(e.target.value)}
                  placeholder="http://127.0.0.1:7897"
                  className="w-full rounded-lg border border-[var(--border)] bg-[var(--bg-pane)] px-3 py-2 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:ring-2 focus:ring-[var(--accent)]"
                />
              </div>
              <p className="mb-4 text-sm text-[var(--text-secondary)]">{t('accounts.loginRefreshPreviewDesc')}</p>
              {preview?.accounts && preview.accounts.length > 0 ? (
                <div className="space-y-2">
                  {preview.accounts.map((acc: any, idx: number) => (
                    <div key={idx} className="rounded-lg border border-[var(--border)] bg-[var(--bg-pane)] px-4 py-2.5 text-sm">
                      <div className="font-medium text-[var(--text-primary)]">{acc.email}</div>
                      {acc.can_refresh === false && (
                        <div className="mt-1 text-xs text-amber-500">{acc.reason || '无本地凭证'}</div>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-pane)] px-4 py-8 text-center text-sm text-[var(--text-muted)]">
                  暂无可刷新的账号
                </div>
              )}
            </div>
            <div className="flex items-center justify-end gap-3 border-t border-[var(--border)] px-6 py-4">
              <Button variant="outline" size="sm" onClick={() => setShowPreview(false)} disabled={loading}>
                取消
              </Button>
              <Button
                size="sm"
                onClick={startRefresh}
                disabled={loading || !preview?.accounts || preview.accounts.length === 0}
              >
                {loading ? t('accounts.loginRefreshing') : t('accounts.loginRefreshStart')}
              </Button>
            </div>
          </div>
        </div>,
        document.body
      )}
    </>
  )
}

function getPageItems(current: number, totalPages: number): (number | 'ellipsis')[] {
  if (totalPages <= 7) return Array.from({ length: totalPages }, (_, i) => i + 1)
  const items: (number | 'ellipsis')[] = [1]
  const start = Math.max(2, current - 1)
  const end = Math.min(totalPages - 1, current + 1)
  if (start > 2) items.push('ellipsis')
  for (let i = start; i <= end; i++) items.push(i)
  if (end < totalPages - 1) items.push('ellipsis')
  items.push(totalPages)
  return items
}

// ── Main ────────────────────────────────────────────────────
export default function Accounts() {
  const { t, language } = useI18n()
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = 'chatgpt'

  const [accounts, setAccounts] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [now, setNow] = useState(() => Date.now())
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [detail, setDetail] = useState<any | null>(null)
  const [showImport, setShowImport] = useState(false)
  const [registerOpen, setRegisterOpen] = useState(false)
  const showRegister = registerOpen || searchParams.get('register') === '1'
  const setShowRegister = (open: boolean) => {
    setRegisterOpen(open)
    if (!open && searchParams.has('register')) {
      const next = new URLSearchParams(searchParams)
      next.delete('register')
      setSearchParams(next, { replace: true })
    }
  }
  const [loadError, setLoadError] = useState('')
  const [platformsMap, setPlatformsMap] = useState<Record<string, any>>({})
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [actionResult, setActionResult] = useState<{ title: string; payload: any } | null>(null)
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const [batchRefreshing, setBatchRefreshing] = useState(false)
  const [batchProxyDialogOpen, setBatchProxyDialogOpen] = useState(false)
  const [batchProxyStarting, setBatchProxyStarting] = useState(false)
  const [gcashProxyDialogOpen, setGcashProxyDialogOpen] = useState(false)
  const [gcashCheckStarting, setGcashCheckStarting] = useState(false)
  const [trialProxyDialogOpen, setTrialProxyDialogOpen] = useState(false)
  const [trialCheckStarting, setTrialCheckStarting] = useState(false)
  const [batchTask, setBatchTask] = useState<{ taskId: string; title: string } | null>(null)
  const [summary, setSummary] = useState<Record<string, number> | null>(null)
  const [summaryError, setSummaryError] = useState('')
  const [dense, setDense] = useState(() => localStorage.getItem('accounts-density') === 'compact')
  const [accountView, setAccountView] = useState<'grid' | 'table'>(() => localStorage.getItem('accounts-view') === 'table' ? 'table' : 'grid')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const tableWrapRef = useRef<HTMLDivElement>(null)
  const loadSeqRef = useRef(0)
  const summarySeqRef = useRef(0)
  const userPinnedSizeRef = useRef(false)

  useEffect(() => {
    getPlatforms().then((list: any[]) => {
      const map: Record<string, any> = {}
      list.forEach(p => { map[p.name] = p })
      setPlatformsMap(map)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 400)
    return () => clearTimeout(timer)
  }, [search])

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    setSelectedIds(new Set())
  }, [tab, filterStatus, debouncedSearch])

  useEffect(() => {
    setPage(1)
  }, [tab, filterStatus, debouncedSearch, pageSize])

  const load = useCallback(async (p = tab, s = debouncedSearch, fs = filterStatus) => {
    const seq = ++loadSeqRef.current
    setLoading(true)
    try {
      const params = new URLSearchParams({ platform: p, page: String(page), page_size: String(pageSize) })
      if (s) params.set('email', s)
      if (fs) params.set('status', fs)
      const data = await apiFetch(`/accounts?${params}`)
      if (seq !== loadSeqRef.current) return
      setAccounts(data.items); setTotal(data.total)
      setLoadError('')
      const maxPage = Math.max(1, Math.ceil((data.total || 0) / pageSize))
      if (page > maxPage) setPage(maxPage)
    } catch (error) {
      if (seq === loadSeqRef.current) setLoadError(error instanceof Error ? error.message : String(error))
    } finally {
      if (seq === loadSeqRef.current) setLoading(false)
    }
  }, [tab, debouncedSearch, filterStatus, page, pageSize])

  useEffect(() => { load(tab, debouncedSearch, filterStatus) }, [load])

  const loadSummary = useCallback(() => {
    const seq = ++summarySeqRef.current
    // The existing stats endpoint is global; filtered totals keep this summary platform-scoped.
    return Promise.all(['', 'valid', 'invalid', 'subscribed'].map(async status => {
        const params = new URLSearchParams({ platform: tab, status, page: '1', page_size: '1' })
        const result = await apiFetch(`/accounts?${params}`)
        return [status || 'all', Number(result.total)] as const
      })).then(entries => {
      if (seq !== summarySeqRef.current) return
      setSummary(Object.fromEntries(entries))
      setSummaryError('')
    }).catch(error => {
      if (seq !== summarySeqRef.current) return
      setSummary(null)
      setSummaryError(error instanceof Error ? error.message : String(error))
    })
  }, [])

  useEffect(() => {
    void loadSummary()
    return () => { summarySeqRef.current += 1 }
  }, [loadSummary])

  const refreshAccounts = () => { void load(); void loadSummary() }

  useEffect(() => {
    tableWrapRef.current?.closest('main')?.scrollTo({ top: 0 })
  }, [page, pageSize, accountView])

  // 自动适配：按表格可视高度估算每页行数，让一页刚好铺满、无需内部滚动
  const fitRowsToViewport = useCallback(() => {
    const wrap = tableWrapRef.current
    if (accountView !== 'table' || !wrap || userPinnedSizeRef.current || window.innerWidth < 768) return
    const head = wrap.querySelector('thead')
    const rows = wrap.querySelectorAll<HTMLElement>('tbody tr')
    if (!head || rows.length === 0) return
    let total = 0
    const sample = Math.min(rows.length, 8)
    for (let i = 0; i < sample; i++) total += rows[i].offsetHeight
    const rowHeight = total / sample
    const usable = wrap.clientHeight - head.offsetHeight - 8
    const fit = Math.floor(usable / rowHeight)
    if (fit >= 5 && fit <= 200 && fit !== pageSize) setPageSize(fit)
  }, [pageSize, accountView])

  useEffect(() => {
    if (loading || accounts.length === 0) return
    const frame = requestAnimationFrame(fitRowsToViewport)
    return () => cancelAnimationFrame(frame)
  }, [loading, accounts, fitRowsToViewport, dense])

  useEffect(() => {
    let timer: number | undefined
    const onResize = () => {
      window.clearTimeout(timer)
      timer = window.setTimeout(fitRowsToViewport, 200)
    }
    window.addEventListener('resize', onResize)
    return () => {
      window.clearTimeout(timer)
      window.removeEventListener('resize', onResize)
    }
  }, [fitRowsToViewport])

  useEffect(() => {
    setSelectedIds(prev => {
      const visible = new Set(accounts.map(acc => acc.id))
      return new Set([...prev].filter(id => visible.has(id)))
    })
  }, [accounts])

  
  const exportCsv = () => {
    const header = 'email,password,display_status,lifecycle_status,plan_state,validity_status,created_at'
    const rowsSource = selectedIds.size > 0 ? accounts.filter(a => selectedIds.has(a.id)) : accounts
    const rows = rowsSource.map(a => [
      a.email,
      a.password,
      getDisplayStatus(a),
      getLifecycleStatus(a),
      getPlanState(a),
      getValidityStatus(a),
      a.created_at,
    ].map(escapeCsvField).join(','))
    const blob = new Blob([[header, ...rows].join('\n')], { type: 'text/csv' })
    triggerBrowserDownload(blob, `${tab}_accounts.csv`)
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const pageIds = accounts.map(acc => acc.id)
  const allSelectedOnPage = pageIds.length > 0 && pageIds.every(id => selectedIds.has(id))
  const selectedCount = selectedIds.size

  const toggleOne = (id: number) => {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const togglePage = () => {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (allSelectedOnPage) pageIds.forEach(id => next.delete(id))
      else pageIds.forEach(id => next.add(id))
      return next
    })
  }

  const copy = async (text: string) => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
        return
      }
    } catch {
      // Fall through for browsers that expose Clipboard API without granting it.
    }
    const el = document.createElement('textarea')
    el.value = text
    document.body.appendChild(el)
    el.select()
    document.execCommand('copy')
    document.body.removeChild(el)
  }

  const currentPlatformMeta = platformsMap[tab]
  const platformLabel = currentPlatformMeta?.display_name || tab
  const zh = language === 'zh-CN'
  const accountStats = [
    { key: 'all', label: zh ? '账号总量' : 'Total accounts', icon: Users, tone: '' },
    { key: 'valid', label: zh ? '有效账号' : 'Valid accounts', icon: BadgeCheck, tone: 'success' },
    { key: 'invalid', label: zh ? '失效账号' : 'Invalid accounts', icon: CircleX, tone: 'danger' },
    { key: 'subscribed', label: zh ? '已订阅' : 'Subscribed', icon: Crown, tone: 'accent' },
  ]

  return (
    <div className="accounts-workspace flex min-h-0 flex-1 flex-col gap-4 overflow-hidden">
      {detail && <DetailModal acc={detail} onClose={() => setDetail(null)} onSave={() => { setDetail(null); refreshAccounts() }} />}
      {showImport && <ImportModal onClose={() => setShowImport(false)} onDone={() => { setShowImport(false); refreshAccounts() }} />}
      {showRegister && <RegisterModal platformMeta={platformsMap[tab]} onClose={() => setShowRegister(false)} onDone={refreshAccounts} onTaskCreated={taskId => setBatchTask({ taskId, title: `${t('accounts.autoRegister')} ${platformLabel}` })} />}
      {actionResult && <ActionResultModal title={actionResult.title} payload={actionResult.payload} onClose={() => setActionResult(null)} />}
      {batchProxyDialogOpen && (
        <BatchCheckProxyModal
          defaultProxy={QUERY_PROXY_PRESETS[0]}
          selectedCount={selectedCount}
          submitting={batchProxyStarting}
          onClose={() => setBatchProxyDialogOpen(false)}
          onSubmit={async (proxy, checkChain) => {
            setBatchProxyStarting(true)
            try {
              const ids = selectedCount > 0 ? [...selectedIds] : []
              const res = await apiFetch('/accounts/check-all', {
                method: 'POST',
                body: JSON.stringify({ platform: tab, ids, proxy, check_chain: checkChain }),
              })
              if (res?.task_id) {
                setBatchProxyDialogOpen(false)
                setBatchProxyStarting(false)
                setBatchRefreshing(true)
                setBatchTask({
                  taskId: res.task_id,
                  title: selectedCount > 0
                    ? t('accounts.batchCheckSelectedTask', { platform: platformLabel, count: selectedCount })
                    : t('accounts.batchCheckAllTask', { platform: platformLabel }),
                })
              } else {
                setBatchProxyStarting(false)
              }
            } catch (e) {
              console.error(e)
              setBatchProxyStarting(false)
              window.alert(e instanceof Error ? e.message : t('accounts.batchCheckStartFailed'))
            }
          }}
        />
      )}
      {gcashProxyDialogOpen && (
        <BatchCheckProxyModal
          kind="gcash"
          defaultProxy={QUERY_PROXY_PRESETS[0]}
          selectedCount={selectedCount}
          submitting={gcashCheckStarting}
          onClose={() => setGcashProxyDialogOpen(false)}
          onSubmit={async (proxy) => {
            setGcashCheckStarting(true)
            try {
              const ids = selectedCount > 0 ? [...selectedIds] : []
              const res = await apiFetch('/accounts/check-gcash', {
                method: 'POST',
                body: JSON.stringify({ platform: tab, ids, proxy }),
              })
              if (res?.task_id) {
                setGcashProxyDialogOpen(false)
                setGcashCheckStarting(false)
                setBatchRefreshing(true)
                setBatchTask({
                  taskId: res.task_id,
                  title: selectedCount > 0
                    ? t('accounts.gcashCheckSelectedTask', { count: selectedCount })
                    : t('accounts.gcashCheckAllTask'),
                })
              } else {
                setGcashCheckStarting(false)
              }
            } catch (e) {
              console.error(e)
              setGcashCheckStarting(false)
              window.alert(e instanceof Error ? e.message : t('accounts.gcashCheckStartFailed'))
            }
          }}
        />
      )}
      {trialProxyDialogOpen && (
        <BatchCheckProxyModal
          kind="trial"
          defaultProxy={QUERY_PROXY_PRESETS[0]}
          selectedCount={selectedCount}
          submitting={trialCheckStarting}
          onClose={() => setTrialProxyDialogOpen(false)}
          onSubmit={async (proxy, checkChain, country) => {
            setTrialCheckStarting(true)
            try {
              const ids = selectedCount > 0 ? [...selectedIds] : []
              const res = await apiFetch('/accounts/check-trial', {
                method: 'POST',
                body: JSON.stringify({ platform: tab, ids, proxy, country, check_chain: checkChain }),
              })
              if (res?.task_id) {
                setTrialProxyDialogOpen(false)
                setTrialCheckStarting(false)
                setBatchRefreshing(true)
                setBatchTask({
                  taskId: res.task_id,
                  title: selectedCount > 0
                    ? t('accounts.trialCheckSelectedTask', { count: selectedCount })
                    : t('accounts.trialCheckAllTask'),
                })
              } else {
                setTrialCheckStarting(false)
              }
            } catch (e) {
              console.error(e)
              setTrialCheckStarting(false)
              window.alert(e instanceof Error ? e.message : t('accounts.trialCheckStartFailed'))
            }
          }}
        />
      )}
      {loadError && <div className="inline-error" role="alert">{loadError}</div>}
      <Card className="accounts-panel flex min-h-0 flex-1 flex-col overflow-hidden p-0" data-density={dense ? 'compact' : 'comfortable'}>
        <div className="accounts-header">
          <div className="min-w-0">
            <h1>{platformLabel} <span className="heading-suffix">{language === 'zh-CN' ? '账号' : 'accounts'}</span></h1>
            <p className="account-heading-meta">{language === 'zh-CN' ? '账号管理' : 'Account management'}<span>/</span>{t('accounts.count', { count: summary?.all ?? total })}</p>
          </div>
          <div className="account-primary-actions">
            <RegistrationStatsDialog platform={tab} />
            <Button size="sm" variant="outline" onClick={() => setShowImport(true)} className={ACCOUNT_TOOL_BUTTON_CLASS}>
              <Upload className="mr-1.5 h-3.5 w-3.5 shrink-0" />
              {t('accounts.import')}
            </Button>
            {tab === 'chatgpt' ? (
              <>
                <ExportMenu
                  total={total}
                  statusFilter={filterStatus}
                  searchFilter={debouncedSearch}
                  selectedIds={[...selectedIds]}
                />
              </>
            ) : (
              <Button size="sm" variant="outline" onClick={exportCsv} disabled={accounts.length === 0} className={ACCOUNT_TOOL_BUTTON_CLASS}>
                <Download className="mr-1.5 h-3.5 w-3.5 shrink-0" />
                {t('accounts.export')}
              </Button>
            )}
            <Button size="sm" onClick={() => setShowRegister(true)}>
              <Plus className="mr-1.5 h-3.5 w-3.5 shrink-0" />{t('accounts.autoRegister')}
            </Button>
          </div>
        </div>
        <div className="metric-strip account-metrics" aria-label={zh ? '账号统计' : 'Account statistics'}>
          {accountStats.map(({ key, label, icon: Icon, tone }) => (
            <div key={key}>
              <p className="metric-label"><Icon size={15} />{label}</p>
              <p className={`metric-value metric-${tone}`}>{summary ? summary[key].toLocaleString() : summaryError ? '-' : <span className="skeleton w-16 h-9" />}</p>
            </div>
          ))}
        </div>
        {summaryError && <p className="inline-error mb-3" role="alert">{zh ? '统计加载失败' : 'Statistics unavailable'}: {summaryError}</p>}
        <div className="account-status-tabs" role="group" aria-label={zh ? '快捷筛选' : 'Quick filters'}>
          {[{ value: '', label: zh ? '全部账号' : 'All accounts' }, { value: 'valid', label: zh ? '有效' : 'Valid' }, { value: 'invalid', label: zh ? '失效' : 'Invalid' }, { value: 'subscribed', label: zh ? '已订阅' : 'Subscribed' }].map(item => (
            <button key={item.value} aria-pressed={filterStatus === item.value} onClick={() => { setFilterStatus(item.value); setPage(1) }}>
              {item.label}{summary && <span>{summary[item.value || 'all']}</span>}
            </button>
          ))}
        </div>
        {/* Search & Filter Toolbar */}
        <div className="accounts-toolbar">
          <div className="account-filters">
            <div className="account-search relative flex-1">
              <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-2.5 text-[var(--text-muted)]">
                <Search size={14} />
              </div>
              <input
                type="text"
                placeholder={t('accounts.searchPlaceholder')}
                aria-label={t('accounts.searchPlaceholder')}
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="w-full rounded-md border border-[var(--border)] bg-transparent py-1.5 pl-8 pr-8 text-[13px] text-[var(--text-primary)]"
              />
              {search && <button className="search-clear" onClick={() => setSearch('')} title={t('common.clear')} aria-label={t('common.clear')}><X size={13} /></button>}
            </div>
            <select
              value={filterStatus}
              aria-label={t('common.status')}
              onChange={e => setFilterStatus(e.target.value)}
              className="rounded-md border border-[var(--border)] bg-[var(--bg-input)] py-1.5 px-3 text-sm text-[var(--text-primary)]"
            >
              <option value="">{t('accounts.allStatuses')}</option>
              <option value="valid">{translateAccountStatus('valid', language)}</option>
              <option value="registered">{translateAccountStatus('registered', language)}</option>
              <option value="trial">{t('dashboard.trial')}</option>
              <option value="subscribed">{t('dashboard.subscribed')}</option>
              <option value="free">{t('accounts.free')}</option>
              <option value="eligible">{t('accounts.eligible')}</option>
              <option value="expired">{t('accounts.expired')}</option>
              <option value="invalid">{t('dashboard.invalid')}</option>
            </select>
            <div className="account-view-switch" role="group" aria-label={zh ? '账号视图' : 'Account view'}>
              {([{ value: 'grid', label: zh ? '头像视图' : 'Avatar view', icon: LayoutGrid }, { value: 'table', label: zh ? '表格视图' : 'Table view', icon: Table2 }] as const).map(({ value, label, icon: Icon }) => (
                <button key={value} type="button" title={label} aria-label={label} aria-pressed={accountView === value} onClick={() => { setAccountView(value); localStorage.setItem('accounts-view', value) }}>
                  <Icon size={16} aria-hidden="true" />
                </button>
              ))}
            </div>
          </div>
          
          <div className="account-tools">
            {selectedCount > 0 && <span className="selection-count" role="status">{t('accounts.selected', { count: selectedCount })}</span>}
            <LoginRefreshMenu total={total} statusFilter={filterStatus} searchFilter={debouncedSearch} selectedIds={[...selectedIds]} onTaskCreated={(taskId, title) => setBatchTask({ taskId, title })} />
            <Button
              variant="ghost"
              size="sm"
              disabled={batchRefreshing || batchProxyStarting || loading || total === 0}
              className="h-7 px-2.5 text-[var(--text-muted)] hover:text-amber-500 hover:bg-amber-500/10"
              title={selectedCount > 0
                ? t('accounts.batchCheckSelectedTitle', { count: selectedCount })
                : t('accounts.batchCheckAllTitle')}
              onClick={() => setBatchProxyDialogOpen(true)}
            >
              <BadgeCheck className={`mr-1 h-3.5 w-3.5 ${batchRefreshing ? 'animate-pulse' : ''}`} />
              {batchRefreshing
                ? t('accounts.batchChecking')
                : selectedCount > 0
                  ? t('accounts.batchCheckSelected', { count: selectedCount })
                  : t('accounts.batchCheckAll')}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={batchRefreshing || batchProxyStarting || gcashCheckStarting || loading || total === 0}
              className="h-7 px-2.5 text-[var(--text-muted)] hover:text-emerald-500 hover:bg-emerald-500/10"
              title={selectedCount > 0
                ? t('accounts.gcashCheckSelectedTitle', { count: selectedCount })
                : t('accounts.gcashCheckAllTitle')}
              onClick={() => setGcashProxyDialogOpen(true)}
            >
              <BadgeCheck className={`mr-1 h-3.5 w-3.5 ${gcashCheckStarting ? 'animate-pulse' : ''}`} />
              {gcashCheckStarting
                ? t('accounts.batchChecking')
                : selectedCount > 0
                  ? t('accounts.gcashCheckSelected', { count: selectedCount })
                  : t('accounts.gcashCheck')}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={batchRefreshing || batchProxyStarting || gcashCheckStarting || trialCheckStarting || loading || total === 0}
              className="h-7 px-2.5 text-[var(--text-muted)] hover:text-sky-500 hover:bg-sky-500/10"
              title={selectedCount > 0
                ? t('accounts.trialCheckSelectedTitle', { count: selectedCount })
                : t('accounts.trialCheckAllTitle')}
              onClick={() => setTrialProxyDialogOpen(true)}
            >
              <FlaskConical className={`mr-1 h-3.5 w-3.5 ${trialCheckStarting ? 'animate-pulse' : ''}`} />
              {trialCheckStarting
                ? t('accounts.batchChecking')
                : selectedCount > 0
                  ? t('accounts.trialCheckSelected', { count: selectedCount })
                  : t('accounts.trialCheck')}
            </Button>
            <Button variant="ghost" size="sm" onClick={refreshAccounts} disabled={loading} title={t('common.refresh')} aria-label={t('common.refresh')} className="h-8 w-8 p-0 text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            </Button>
            <Button variant="ghost" size="sm" className="h-8 w-8 p-0" title={zh ? (dense ? '标准行高' : '紧凑行高') : (dense ? 'Comfortable rows' : 'Compact rows')} aria-label={zh ? '紧凑行高' : 'Compact rows'} aria-pressed={dense} onClick={() => { setDense(!dense); localStorage.setItem('accounts-density', dense ? 'comfortable' : 'compact') }}>
              {dense ? <List size={16} /> : <Rows3 size={16} />}
            </Button>
            {selectedCount > 0 && (
              <Button
                size="sm"
                variant="ghost"
                disabled={bulkDeleting}
                className="h-7 px-2.5 text-red-500 hover:bg-red-500/10 hover:text-red-600"
                onClick={async () => {
                  if (!confirm(t('accounts.deleteSelectedConfirm', { count: selectedCount }))) return
                  setBulkDeleting(true)
                  try {
                    await Promise.allSettled(
                      [...selectedIds].map(id => apiFetch(`/accounts/${id}`, { method: 'DELETE' }))
                    )
                    setSelectedIds(new Set())
                    refreshAccounts()
                  } finally {
                    setBulkDeleting(false)
                  }
                }}
              >
                <Trash2 className="mr-1.5 h-3.5 w-3.5" />
                {bulkDeleting ? t('common.deleting') : t('common.delete')}
              </Button>
            )}
          </div>
        </div>
        {accountView === 'grid' ? (
          <section className="account-collection" aria-label={zh ? '账号头像列表' : 'Account avatars'} aria-busy={loading}>
            <div className="account-collection-bar">
              <label className="collection-select"><input type="checkbox" className="checkbox-accent" checked={allSelectedOnPage} disabled={!accounts.length} onChange={togglePage} aria-label={zh ? '选择本页账号' : 'Select this page'} />{zh ? '本页账号' : 'This page'}<span>{accounts.length}</span></label>
              <div className="account-health-legend"><span data-health="valid">{zh ? '可用' : 'Available'}</span><span data-health="unknown">{zh ? '未确认' : 'Unverified'}</span><span data-health="invalid">{zh ? '失效' : 'Invalid'}</span></div>
            </div>
            <div className="account-atlas" ref={tableWrapRef}>
              {loading && accounts.length === 0 && Array.from({ length: 8 }, (_, i) => <div key={i} className="account-tile-skeleton" aria-hidden="true"><span className="skeleton" /><span className="skeleton" /></div>)}
              {!loading && accounts.length === 0 && <div className="account-empty atlas-empty"><Inbox size={28} aria-hidden="true" /><h3>{t('accounts.emptyTitle')}</h3>{(search || filterStatus) && <Button variant="link" size="sm" onClick={() => { setSearch(''); setFilterStatus('') }}>{zh ? '清除筛选' : 'Clear filters'}</Button>}</div>}
              {accounts.map(acc => {
                const health = accountHealth(getValidityStatus(acc))
                const healthLabel = health === 'valid' ? (zh ? '可用' : 'Available') : health === 'invalid' ? (zh ? '失效' : 'Invalid') : (zh ? '未确认可用' : 'Unverified')
                const plan = getPlanChip(acc)
                return (
                  <article className="account-tile" key={acc.id} data-selected={selectedIds.has(acc.id)} data-health={health}>
                    <div className="account-tile-top"><input type="checkbox" className="checkbox-accent" checked={selectedIds.has(acc.id)} aria-label={`${zh ? '选择' : 'Select'} ${acc.email}`} onChange={() => toggleOne(acc.id)} /><span>{plan?.label || translateAccountStatus(getLifecycleStatus(acc), language)}</span></div>
                    <button className="account-tile-identity" title={`${acc.email} · ${healthLabel}`} aria-label={acc.email} aria-describedby={`account-health-${acc.id}`} onClick={() => setDetail(acc)}>
                      <AccountAvatar email={acc.email || ''} validity={getValidityStatus(acc)} />
                      <span className="account-tile-email">{acc.email}</span>
                    </button>
                    <span className="account-tile-health" id={`account-health-${acc.id}`}>{healthLabel}</span>
                    <div className="account-tile-footer"><span title={t('accounts.survivalTime')}><Clock size={11} aria-hidden="true" />{formatSurvivalDuration(acc, now, language)}</span><ActionMenu acc={acc} onDetail={() => setDetail(acc)} onDelete={refreshAccounts} onResult={(title, payload) => setActionResult({ title, payload })} onChanged={refreshAccounts} /></div>
                  </article>
                )
              })}
            </div>
          </section>
        ) : (
          <div ref={tableWrapRef} className="accounts-table glass-table-wrap min-h-0 flex-1 overflow-auto" aria-busy={loading}>
        <table className="table-fixed w-full min-w-[860px] text-[13px]">
          <colgroup>
            <col className="w-10" />
            <col className="w-[29%]" />
            <col className="w-[10%]" />
            <col className="w-[24%]" />
            <col className="w-[8%]" />
            <col className="w-[15%]" />
            <col className="w-[14%]" />
          </colgroup>
          <thead className="sticky top-0 z-10 bg-[var(--bg-pane)]/85 shadow-[0_1px_0_var(--border)] backdrop-blur-sm">
            <tr className="border-b border-[var(--border)] text-xs uppercase tracking-wider font-medium text-[var(--text-muted)]">
              <th className="w-10 px-3 py-2 text-left">
                <input
                  type="checkbox"
                  checked={allSelectedOnPage}
                  aria-label={zh ? '选择本页账号' : 'Select this page'}
                  onChange={togglePage}
                  className="checkbox-accent rounded-[3px] border-[var(--border)] focus:ring-[var(--text-primary)] focus:ring-offset-0 bg-transparent text-[var(--text-primary)]"
                />
              </th>
              <th className="px-3 py-2 text-left">{t('common.email')}</th>
              <th className="px-3 py-2 text-left">{t('common.password')}</th>
              <th className="px-3 py-2 text-left">{t('common.status')}</th>
              <th className="px-3 py-2 text-left">{t('accounts.survivalTime')}</th>
              <th className="px-3 py-2 text-left">{t('accounts.registeredAt')}</th>
              <th className="px-3 py-2 text-right">{t('common.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {loading && accounts.length === 0 && Array.from({ length: 5 }, (_, index) => (
              <tr key={`loading-${index}`} aria-hidden="true">{Array.from({ length: 7 }, (_, cell) => <td key={cell}><span className="skeleton" /></td>)}</tr>
            ))}
            {!loading && accounts.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-24 text-center">
                  <div className="account-empty flex flex-col items-center justify-center space-y-3">
                    <div className="flex h-12 w-12 items-center justify-center rounded-full bg-[var(--bg-pane)] border border-[var(--border)] shadow-sm">
                      <Inbox className="h-6 w-6 text-[var(--text-muted)]" />
                    </div>
                    <h3 className="text-sm font-medium text-[var(--text-primary)]">{t('accounts.emptyTitle')}</h3>
                    {(search || filterStatus) && <Button variant="link" size="sm" onClick={() => { setSearch(''); setFilterStatus('') }}>{zh ? '清除筛选' : 'Clear filters'}</Button>}
                  </div>
                </td>
              </tr>
            )}
            {accounts.map(acc => (
              (() => {
                const overview = getAccountOverview(acc)
                const verificationMailbox = getVerificationMailbox(acc)
                const primaryMetrics = getPrimaryMetrics(acc)
                const displayBadges = getDisplayBadges(acc)
                const survivalTime = formatSurvivalDuration(acc, now, language)
                return (
              <tr key={acc.id} className="group border-b border-[var(--border)]/30 hover:bg-[var(--text-primary)]/[0.02] transition-colors cursor-pointer"
                  onClick={() => setDetail(acc)}>
                <td className="px-3 py-2.5" onClick={e => e.stopPropagation()}>
                  <input
                    type="checkbox"
                    checked={selectedIds.has(acc.id)}
                    aria-label={`${zh ? '选择' : 'Select'} ${acc.email}`}
                    onChange={() => toggleOne(acc.id)}
                    className="checkbox-accent rounded-[3px] border-[var(--border)] focus:ring-[var(--text-primary)] focus:ring-offset-0 bg-transparent text-[var(--text-primary)] transition-all opacity-40 group-hover:opacity-100 data-[state=checked]:opacity-100"
                  />
                </td>
                <td className="px-3 py-2.5 font-mono text-sm text-[var(--text-primary)] align-top">
                  <div className="flex min-w-0 items-center gap-2">
                    <AccountAvatar email={acc.email || ''} validity={getValidityStatus(acc)} small />
                    <button className="account-email" title={acc.email} onClick={() => setDetail(acc)}>{acc.email}</button>
                    <button onClick={e => { e.stopPropagation(); copy(acc.email) }} title="复制邮箱" className="text-[var(--text-muted)] hover:text-[var(--text-primary)] opacity-0 group-hover:opacity-100 transition-opacity"><Copy className="h-3 w-3" /></button>
                  </div>
                  {verificationMailbox && verificationMailbox.email !== acc.email && (verificationMailbox.email || verificationMailbox.account_id || verificationMailbox.provider) && (
                    <div
                      className="mt-1 truncate text-xs text-[var(--text-muted)] flex items-center gap-1"
                      title={`验证邮箱: ${verificationMailbox.email || '-'} · ${verificationMailbox.provider || '-'}`}
                    >
                      <Mail className="w-3 h-3 flex-shrink-0" />
                      <span className="truncate">{verificationMailbox.email || '-'}</span>
                    </div>
                  )}
                  {overview?.remote_email && overview.remote_email !== acc.email && (
                    <div className="mt-1 truncate text-xs text-[var(--text-muted)]" title={`远端邮箱: ${overview.remote_email}`}>
                      远端邮箱: {overview.remote_email}
                    </div>
                  )}
                  {displayBadges.length > 0 && (
                    <div className="mt-1.5 flex flex-wrap gap-1">
                      {displayBadges.slice(0, 3).map((badge: any, index: number) => (
                        <span key={`${badge?.label || 'badge'}-${index}`} className="rounded border border-[var(--border)]/50 bg-[var(--bg-pane)]/40 px-1 py-0.5 text-[11px] font-medium text-[var(--text-muted)] shadow-sm">
                          {badge?.label}
                        </span>
                      ))}
                    </div>
                  )}
                </td>
                <td className="px-3 py-2.5 font-mono text-[13px] text-[var(--text-muted)] align-top">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate" aria-label={zh ? '密码已隐藏' : 'Password hidden'}>******</span>
                    <button onClick={e => { e.stopPropagation(); copy(acc.password) }} title={zh ? '复制密码' : 'Copy password'} aria-label={zh ? '复制密码' : 'Copy password'} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"><Copy className="h-3 w-3" /></button>
                  </div>
                </td>
                <td className="px-3 py-2.5 align-top">
                  <div className="min-w-0 flex flex-wrap items-center gap-1.5">
                    {(() => {
                      const status = getDisplayStatus(acc);
                      const variant = String(STATUS_VARIANT[status] || 'secondary');
                      const StatusIcon = getStatusIcon(status);
                      const styles = (({
                        success: "bg-[var(--success-soft)] text-[var(--status-success)]",
                        warning: "bg-[var(--warning-soft)] text-[var(--status-warning)]",
                        danger: "bg-[var(--danger-soft)] text-[var(--status-danger)]",
                        secondary: "bg-[var(--text-primary)]/5 text-[var(--text-secondary)] ring-[var(--border)]",
                        default: "bg-[var(--accent-soft)] text-[var(--text-accent)]"
                      } as Record<string, string>)[variant]) || "bg-[var(--text-primary)]/5 text-[var(--text-secondary)] ring-[var(--border)]";
                      
                      return (
                        <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium ${styles}`}>
                          <StatusIcon className={`mr-1 h-3 w-3 ${getStatusIconColor(variant)}`} />
                          {translateAccountStatus(status, language)}
                        </span>
                      );
                    })()}
                    {(() => {
                      const planChip = getPlanChip(acc)
                      const validityChip = getValidityChip(acc)
                      const checkoutChainChip = getCheckoutChainChip(acc)
                      const plusTrialChip = getPlusTrialChip(acc)
                      const gcashChip = getGCashChip(acc)
                      const trialActive = isTrialAccount(acc) && getDisplayStatus(acc) !== 'trial'
                      if (!planChip && !validityChip && !checkoutChainChip && !plusTrialChip && !gcashChip && !trialActive) return null
                      return (
                        <div className="flex max-w-full flex-wrap items-center gap-1">
                          {planChip && <StatusIconChip {...planChip} />}
                          {validityChip && validityChip.label !== getDisplayStatus(acc) && (
                            <StatusIconChip
                              tone={validityChip.tone}
                              label={translateAccountStatus(validityChip.label, language)}
                            />
                          )}
                          {checkoutChainChip && <StatusIconChip {...checkoutChainChip} />}
                          {plusTrialChip && <StatusIconChip {...plusTrialChip} />}
                          {gcashChip && <StatusIconChip {...gcashChip} />}
                          {trialActive && (
                            <StatusIconChip
                              label={translateAccountStatus('trial', language)}
                              tone="amber"
                            />
                          )}
                        </div>
                      )
                    })()}
                    {primaryMetrics.length > 0 && (
                      <div className="basis-full flex max-w-full flex-col gap-1">
                        {primaryMetrics.slice(0, 2).map((metric: any) => (
                          <div key={metric.key || metric.label} className="flex items-center gap-1.5">
                            <span className="h-1 w-1 rounded-full bg-[var(--text-muted)] opacity-50"></span>
                            <span className="text-xs tracking-tight text-[var(--text-muted)] whitespace-nowrap">
                              <span className="font-medium text-[var(--text-secondary)] mr-0.5">{metric.label}:</span>
                              {metric.value}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </td>
                <td className="px-3 py-2.5 align-top">
                  <span
                    className={`whitespace-nowrap font-mono text-xs ${survivalTime === '已失效' || survivalTime === 'Expired' ? 'text-red-400' : 'text-[var(--text-secondary)]'}`}
                    title={acc.created_at ? formatDateTime(acc.created_at, language) : undefined}
                  >
                    {survivalTime}
                  </span>
                </td>
                <td className="px-3 py-2.5 font-mono text-xs text-[var(--text-muted)] whitespace-nowrap align-top">
                  {(acc.registered_at || acc.created_at) ? formatDateTime(acc.registered_at || acc.created_at, language, { 
                    month: '2-digit', day: '2-digit',
                    hour: '2-digit', minute: '2-digit',
                    hour12: false 
                  }) : '-'}
                </td>
                <td className="px-3 py-2.5 align-top" onClick={e => e.stopPropagation()}>
                  <div className="flex items-center justify-end opacity-60 group-hover:opacity-100 transition-opacity">
                    <ActionMenu
                      acc={acc}
                      onDetail={() => setDetail(acc)}
                      onDelete={refreshAccounts}
                      onResult={(title, payload) => setActionResult({ title, payload })}
                      onChanged={refreshAccounts}
                    />
                  </div>
                </td>
              </tr>
                )
              })()
            ))}
          </tbody>
        </table>
          </div>
        )}
        {/* Pagination */}
        <div className="account-pagination flex shrink-0 flex-wrap items-center justify-between gap-3 py-3">
          <div className="flex items-center gap-3 text-xs text-[var(--text-muted)]">
            <span>{t('accounts.pageSummary', { page, pages: totalPages, count: total })}</span>
            <select
              value={pageSize}
              aria-label={zh ? '每页账号数' : 'Accounts per page'}
              onChange={e => {
                userPinnedSizeRef.current = true
                setPageSize(Number(e.target.value))
              }}
              className="rounded-md border border-[var(--border)] bg-transparent py-1 pl-2 pr-7 text-xs text-[var(--text-primary)] appearance-none focus:border-[var(--text-primary)] focus:outline-none focus:ring-1 focus:ring-[var(--text-primary)]"
              style={{ backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E")`, backgroundPosition: 'right 6px center', backgroundRepeat: 'no-repeat' }}
            >
              {[...new Set([10, 20, 50, 100, 200, pageSize])].sort((a, b) => a - b).map(size => (
                <option key={size} value={size}>{t('accounts.pageSizeOption', { size })}</option>
              ))}
            </select>
          </div>
          <div className="flex items-center gap-1">
            <button
              type="button"
              title={t('accounts.prevPage')}
              disabled={page <= 1 || loading}
              onClick={() => setPage(p => Math.max(1, p - 1))}
              className="table-action-btn h-7 w-7 p-0"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </button>
            {getPageItems(page, totalPages).map((item, index) =>
              item === 'ellipsis' ? (
                <span key={`ellipsis-${index}`} className="px-1 text-xs text-[var(--text-muted)]">…</span>
              ) : (
                <button
                  key={`page-${item}`}
                  type="button"
                  disabled={loading}
                  onClick={() => setPage(item)}
                  className={`table-action-btn h-7 min-w-[28px] ${item === page ? 'border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--text-primary)]' : ''}`}
                >
                  {item}
                </button>
              ),
            )}
            <button
              type="button"
              title={t('accounts.nextPage')}
              disabled={page >= totalPages || loading}
              onClick={() => setPage(p => Math.min(totalPages, p + 1))}
              className="table-action-btn h-7 w-7 p-0"
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </Card>
      {!showRegister && <TaskDock key={batchTask?.taskId || 'idle'} task={batchTask} onDone={() => { setBatchRefreshing(false); refreshAccounts() }} />}
    </div>
  )
}
