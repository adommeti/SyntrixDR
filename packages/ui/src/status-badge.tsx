import { AlertTriangle, CheckCircle2, CircleDot, Info, XCircle } from "lucide-react";
import type { ComponentType } from "react";

import { cn } from "./utils";

export type StatusTone = "success" | "warning" | "danger" | "info" | "neutral";

const TONE_ICON: Record<StatusTone, ComponentType<{ className?: string }>> = {
  success: CheckCircle2,
  warning: AlertTriangle,
  danger: XCircle,
  info: Info,
  neutral: CircleDot,
};

const TONE_CLASSES: Record<StatusTone, string> = {
  success: "bg-success/15 text-success border-success/30",
  warning: "bg-warning/15 text-warning border-warning/30",
  danger: "bg-danger/15 text-danger border-danger/30",
  info: "bg-info/15 text-info border-info/30",
  neutral: "bg-muted text-muted-foreground border-border",
};

export interface StatusBadgeProps {
  tone: StatusTone;
  label: string;
  className?: string;
}

/**
 * Never color-only (design principle 5): every tone pairs a distinct icon with
 * the label text, so the status reads correctly under color-blindness or
 * grayscale printing.
 */
export function StatusBadge({ tone, label, className }: StatusBadgeProps) {
  const Icon = TONE_ICON[tone];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
        TONE_CLASSES[tone],
        className,
      )}
    >
      <Icon className="size-3.5" aria-hidden="true" />
      {label}
    </span>
  );
}
