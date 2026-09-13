"use client";

import { ChevronDown } from "lucide-react";
import { useId, useState, type ReactNode } from "react";

import { cn } from "./utils";

export interface DeepCardProps {
  title: string;
  summary: ReactNode;
  children: ReactNode;
  className?: string;
  defaultExpanded?: boolean;
}

/**
 * Progressive disclosure primitive (design principle 3): the summary is always
 * visible; detail only ever expands via the "Show More" button, never via
 * hover alone (CLAUDE.md UX rule — no critical action lives only in hover).
 */
export function DeepCard({ title, summary, children, className, defaultExpanded = false }: DeepCardProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const detailId = useId();

  return (
    <div className={cn("rounded-lg border border-border bg-card text-card-foreground shadow-sm", className)}>
      <div className="flex items-start justify-between gap-4 p-4">
        <div className="min-w-0 flex-1">
          <h3 className="font-medium">{title}</h3>
          <div className="mt-1 text-sm text-muted-foreground">{summary}</div>
        </div>
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={detailId}
          onClick={() => setExpanded((value) => !value)}
          className="flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-sm text-primary hover:bg-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring"
        >
          {expanded ? "Show less" : "Show more"}
          <ChevronDown
            className={cn("size-4 transition-transform", expanded && "rotate-180")}
            aria-hidden="true"
          />
        </button>
      </div>
      {expanded ? (
        <div id={detailId} className="border-t border-border p-4">
          {children}
        </div>
      ) : null}
    </div>
  );
}
