import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const badgeVariants = cva(
  'inline-flex items-center rounded-md border px-2 py-0.5 text-[11px] font-medium',
  {
    variants: {
      variant: {
        default: 'border-[var(--accent-edge)] bg-[var(--accent-soft)] text-[var(--text-accent)]',
        success: 'border-transparent bg-[var(--success-soft)] text-[var(--status-success)]',
        warning: 'border-transparent bg-[var(--warning-soft)] text-[var(--status-warning)]',
        danger: 'border-transparent bg-[var(--danger-soft)] text-[var(--status-danger)]',
        secondary: 'border-[var(--border)] bg-[var(--chip-bg)] text-[var(--text-muted)]',
      },
    },
    defaultVariants: { variant: 'default' },
  }
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />
}

export { Badge, badgeVariants }
