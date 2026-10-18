export type FailureStage = { stage: string; count: number }

export type RegistrationStatistics = {
  success: number
  failed: number
  completed: number
  success_rate: number | null
  failure_stages: FailureStage[]
  setup_failures: number
  setup_failure_stages: FailureStage[]
  requested?: number
  remaining?: number
  unattempted?: number
  total_tasks?: number
  active_tasks?: number
  cancelled_tasks?: number
  interrupted_tasks?: number
  updated_at?: string
}

const STAGE_NAMES: Record<string, [string, string]> = {
  initialization: ['环境初始化', 'Initialization'],
  mailbox: ['邮箱准备', 'Mailbox preparation'],
  authorization: ['连接与认证', 'Connection & authentication'],
  verification: ['邮箱验证', 'Email verification'],
  profile: ['创建账号', 'Account creation'],
  credentials: ['获取凭据', 'Credential retrieval'],
  security: ['安全设置', 'Security setup'],
  persistence: ['保存结果', 'Saving results'],
  unknown: ['未标注阶段', 'Unspecified stage'],
}

export function registrationStageName(stage: string, chinese: boolean) {
  return (STAGE_NAMES[stage] || STAGE_NAMES.unknown)[chinese ? 0 : 1]
}

export function taskRegistrationStatistics(task: {
  type?: string
  registration_stats?: RegistrationStatistics
  success?: number
  error_count?: number
} | null): RegistrationStatistics | null {
  if (!task || task.type !== 'register') return null
  if (task.registration_stats) return task.registration_stats
  const success = Math.max(0, Number(task.success) || 0)
  const failed = Math.max(0, Number(task.error_count) || 0)
  return {
    success, failed, completed: success + failed,
    success_rate: success + failed ? Math.round(success / (success + failed) * 1000) / 10 : null,
    failure_stages: failed ? [{ stage: 'unknown', count: failed }] : [],
    setup_failures: 0, setup_failure_stages: [],
  }
}
