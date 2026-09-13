"use client";

import { useEffect } from "react";

/**
 * Root error boundary: shows a safe message only, never the raw error/stack
 * (VERIFY.md A7.2). `error.digest` is Next.js's own correlation id for
 * server-side render errors, logged but not required to be shown.
 */
export default function RootError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Placeholder until BUILD-11 wires real client error reporting.
    console.error("Unhandled render error", { digest: error.digest });
  }, [error]);

  return (
    <div className="flex flex-col items-center justify-center gap-4 p-16 text-center">
      <h1 className="text-lg font-semibold">Something went wrong.</h1>
      <p className="text-muted-foreground">
        An unexpected error occurred. Try again, or reload the page if it keeps happening.
      </p>
      <button
        type="button"
        onClick={reset}
        className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring"
      >
        Try again
      </button>
    </div>
  );
}
