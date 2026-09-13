import { AlertTriangle } from "lucide-react";

export interface DegradedBannerProps {
  /** Human-readable reason, e.g. "AI is disabled" or "Realtime updates unavailable — showing last known state." */
  reason?: string;
}

/**
 * Reusable degraded-state banner (design principle: degraded states are
 * designed, never silently absent). Renders nothing when there is no reason —
 * screens that never degrade simply never pass one.
 */
export function DegradedBanner({ reason }: DegradedBannerProps) {
  if (!reason) return null;

  return (
    <div
      role="status"
      className="flex items-center gap-2 border-b border-warning/30 bg-warning/10 px-4 py-2 text-sm text-warning-foreground"
    >
      <AlertTriangle className="size-4 shrink-0 text-warning" aria-hidden="true" />
      <span>{reason}</span>
    </div>
  );
}
