import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";

import { DeepCard } from "./deep-card";

describe("DeepCard", () => {
  it("shows the summary but not the detail until expanded", () => {
    render(
      <DeepCard title="Task 1" summary="2 of 3 evidence items">
        <p>Full verification note</p>
      </DeepCard>,
    );
    expect(screen.getByText("2 of 3 evidence items")).toBeInTheDocument();
    expect(screen.queryByText("Full verification note")).not.toBeInTheDocument();
  });

  it("expands the detail via the Show more button, not hover alone", async () => {
    const user = userEvent.setup();
    render(
      <DeepCard title="Task 1" summary="2 of 3 evidence items">
        <p>Full verification note</p>
      </DeepCard>,
    );

    const toggle = screen.getByRole("button", { name: /show more/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await user.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Full verification note")).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <DeepCard title="Task 1" summary="2 of 3 evidence items">
        <p>Full verification note</p>
      </DeepCard>,
    );
    const results = await axe(container);
    expect(results.violations).toHaveLength(0);
  });
});
