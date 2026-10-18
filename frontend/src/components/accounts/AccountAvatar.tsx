import { Check, Minus, X } from 'lucide-react'
import { useI18n } from '@/lib/i18n-context'

export type AccountHealth = 'valid' | 'invalid' | 'unknown'

export function accountHealth(validity: string): AccountHealth {
  return validity === 'valid' || validity === 'invalid' ? validity : 'unknown'
}

export function AccountAvatar({ email, validity, small = false }: {
  email: string
  validity: string
  small?: boolean
}) {
  const { language } = useI18n()
  const health = accountHealth(validity)
  const zh = language === 'zh-CN'
  const label = health === 'valid' ? (zh ? '可用' : 'Available')
    : health === 'invalid' ? (zh ? '失效' : 'Invalid') : (zh ? '未确认可用' : 'Unverified')
  const initial = Array.from(email.trim().split('@')[0] || '?')[0].toLocaleUpperCase()
  const StatusIcon = health === 'valid' ? Check : health === 'invalid' ? X : Minus

  return (
    <span className={`account-avatar${small ? ' account-avatar-small' : ''}`} data-health={health} role="img" aria-label={`${email}: ${label}`}>
      <span className="account-avatar-initial" aria-hidden="true">{initial}</span>
      <span className="account-avatar-state" aria-hidden="true"><StatusIcon size={11} strokeWidth={2.5} /></span>
    </span>
  )
}
