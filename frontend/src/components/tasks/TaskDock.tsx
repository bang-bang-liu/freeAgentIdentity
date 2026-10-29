import { useState } from 'react'
import { Activity, ChevronDown, ChevronUp } from 'lucide-react'
import { useI18n } from '@/lib/i18n-context'
import { getTaskStatusText } from '@/lib/tasks'
import { TaskLogPanel, type TaskProgress } from './TaskLogPanel'

export function TaskDock({ task, onDone, initialExpanded = false }: {
  task: { taskId: string; title: string } | null
  onDone: (status: string) => void
  initialExpanded?: boolean
}) {
  const { language, t } = useI18n()
  const [expanded, setExpanded] = useState(initialExpanded)
  const [progress, setProgress] = useState<TaskProgress | null>(null)
  const percent = progress && progress.total > 0
    ? Math.min(100, Math.max(0, progress.current / progress.total * 100)) : 0

  return (
    <section className="task-dock" aria-label={t('taskDock.title')}>
      <div className="task-dock-bar">
        <div className="task-dock-symbol"><Activity size={18} /></div>
        <div className="min-w-0">
          <div className="task-dock-title">
            <span className="truncate">{task?.title || t('taskDock.title')}</span>
            <span className="task-dock-status" role="status">
              {task ? (progress ? getTaskStatusText(progress.status, language) : t('taskDock.connecting')) : t('taskDock.empty')}
            </span>
            {progress?.registration && <span className="task-dock-outcomes"><span>{language === 'zh-CN' ? '成功' : 'Success'} {progress.success || 0}</span><span>{language === 'zh-CN' ? '失败' : 'Failed'} {progress.failed || 0}</span></span>}
          </div>
          {task && progress && progress.total > 0 && (
            <div className="task-dock-progress">
              <progress value={percent} max={100} aria-label={t('taskLog.progress')} />
              <span>{progress.current} / {progress.total}</span>
            </div>
          )}
        </div>
        {task && <button className="task-dock-toggle" onClick={() => setExpanded(value => !value)} aria-expanded={expanded} aria-controls="account-task-log">
          {t(expanded ? 'taskDock.hide' : 'taskDock.show')}
          {expanded ? <ChevronDown size={15} /> : <ChevronUp size={15} />}
        </button>}
      </div>
      {task && <div id="account-task-log" className="task-dock-content" hidden={!expanded}>
        <TaskLogPanel taskId={task.taskId} onDone={onDone} onProgress={setProgress} />
      </div>}
    </section>
  )
}
