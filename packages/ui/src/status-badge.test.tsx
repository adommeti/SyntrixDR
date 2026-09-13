import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";

import { StatusBadge } from "./status-badge";

describe("StatusBadge", () => {
  it("renders the label as text, not just a color swatch", () => {
    render(<StatusBadge tone="danger" label="Breached" />);
    expect(screen.getByText("Breached")).toBeInTheDocument();
  });

  it("renders a distinct icon per tone so status never depends on color alone", () => {
    const { container: success } = render(<StatusBadge tone="success" label="On track" />);
    const { container: danger } = render(<StatusBadge tone="danger" label="Breached" />);
    expect(success.querySelector("svg")).toBeInTheDocument();
    expect(danger.querySelector("svg")).toBeInTheDocument();
    expect(success.querySelector("svg")?.outerHTML).not.toEqual(danger.querySelector("svg")?.outerHTML);
  });

  it("has no accessibility violations", async () => {
    const { container } = render(<StatusBadge tone="warning" label="At risk" />);
    const results = await axe(container);
    expect(results.violations).toHaveLength(0);
  });
});
