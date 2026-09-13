import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";

import { DegradedBanner } from "@/components/degraded-banner";

describe("DegradedBanner", () => {
  it("renders nothing when there is no reason", () => {
    const { container } = render(<DegradedBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the reason when degraded", () => {
    render(<DegradedBanner reason="AI is disabled" />);
    expect(screen.getByRole("status")).toHaveTextContent("AI is disabled");
  });

  it("has no accessibility violations when visible", async () => {
    const { container } = render(<DegradedBanner reason="Realtime updates unavailable" />);
    const results = await axe(container);
    expect(results.violations).toHaveLength(0);
  });
});
