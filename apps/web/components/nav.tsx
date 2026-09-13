"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

// Top-level Information Architecture (UI_UX.md, D-212). Sub-routes (per-Event,
// per-Task, admin sub-pages, etc.) are added by the increments that build them.
const NAV_ITEMS = [
  { href: "/my-dr", label: "My DR" },
  { href: "/plans", label: "Plans" },
  { href: "/dr-events", label: "DR Events" },
  { href: "/applications", label: "Applications" },
  { href: "/people", label: "People" },
  { href: "/search", label: "Search" },
  { href: "/admin", label: "Admin" },
] as const;

export function Nav() {
  const pathname = usePathname();

  return (
    <nav aria-label="Primary" className="flex items-center gap-1">
      {NAV_ITEMS.map((item) => {
        const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "rounded-md px-3 py-1.5 text-sm font-medium hover:bg-muted",
              active ? "bg-muted text-foreground" : "text-muted-foreground",
            )}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
