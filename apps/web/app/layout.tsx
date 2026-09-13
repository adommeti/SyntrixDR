import type { Metadata } from "next";
import type { ReactNode } from "react";

import { DegradedBanner } from "@/components/degraded-banner";
import { Nav } from "@/components/nav";
import { Providers } from "@/components/providers";
import { ThemeInitScript } from "@/components/theme-provider";
import { ThemeToggle } from "@/components/theme-toggle";

import "./globals.css";

export const metadata: Metadata = {
  title: "Syntrix DR Command Center",
  description: "Plan, fail over, validate, fail back, audit and report a regional DR from one live model.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <ThemeInitScript />
      </head>
      <body>
        <Providers>
          <div className="flex min-h-screen flex-col">
            <header className="flex items-center justify-between border-b border-border px-4 py-2">
              <Nav />
              <ThemeToggle />
            </header>
            <DegradedBanner />
            <main className="flex-1 p-4">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
