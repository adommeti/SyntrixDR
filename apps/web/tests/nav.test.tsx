import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  usePathname: () => "/my-dr",
}));

import { Nav } from "@/components/nav";

describe("Nav", () => {
  it("renders the top-level IA routes (UI_UX.md, D-212)", () => {
    render(<Nav />);

    for (const label of ["My DR", "Plans", "DR Events", "Applications", "People", "Search", "Admin"]) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
  });

  it("marks the active route with aria-current", () => {
    render(<Nav />);
    expect(screen.getByRole("link", { name: "My DR" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Plans" })).not.toHaveAttribute("aria-current");
  });
});
