import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import RootError from "@/app/error";

describe("RootError", () => {
  it("shows a safe message, never the raw error text or stack", () => {
    const error = Object.assign(new Error("connection refused at db.internal:5432"), { digest: "abc123" });
    render(<RootError error={error} reset={() => {}} />);

    expect(screen.getByText(/unexpected error occurred/i)).toBeInTheDocument();
    expect(screen.queryByText(/connection refused/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/db\.internal/i)).not.toBeInTheDocument();
  });

  it("calls reset when Try again is clicked", async () => {
    const user = userEvent.setup();
    const reset = vi.fn();
    render(<RootError error={new Error("boom")} reset={reset} />);

    await user.click(screen.getByRole("button", { name: /try again/i }));

    expect(reset).toHaveBeenCalledOnce();
  });
});
