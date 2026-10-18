import * as Dialog from '@radix-ui/react-dialog'
import { BarChart3, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useI18n } from '@/lib/i18n-context'
import { RegistrationAnalytics } from './RegistrationAnalytics'

export function RegistrationStatsDialog({ platform }: { platform: string }) {
  const { language } = useI18n()
  const zh = language === 'zh-CN'
  return (
    <Dialog.Root>
      <Dialog.Trigger asChild><Button size="sm" variant="outline"><BarChart3 className="mr-1.5 h-3.5 w-3.5" aria-hidden="true" />{zh ? '注册统计' : 'Registration stats'}</Button></Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="analytics-dialog-overlay" />
        <Dialog.Content className="analytics-dialog">
          <div className="analytics-dialog-heading"><div><Dialog.Title>{zh ? '注册表现' : 'Registration performance'}</Dialog.Title><Dialog.Description>{zh ? '历史结果持续保存，随时查看成功率与失败阶段。' : 'Review persisted outcomes, success rate and failure stages.'}</Dialog.Description></div><Dialog.Close asChild><button className="icon-control" aria-label={zh ? '关闭注册统计' : 'Close registration statistics'}><X size={19} aria-hidden="true" /></button></Dialog.Close></div>
          <RegistrationAnalytics platform={platform} />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
