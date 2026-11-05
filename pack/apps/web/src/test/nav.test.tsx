import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";

vi.mock("../api/missions", () => ({
  fetchMissions: vi.fn().mockResolvedValue([]),
  fetchMission: vi.fn(),
  MissionNotFoundError: class MissionNotFoundError extends Error {},
}));

describe("bottom nav", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("renders exactly Home, Missions, Replay, and Profile", () => {
    render(
      <MemoryRouter>
        <App />
      </MemoryRouter>,
    );
    const nav = screen.getByRole("navigation", { name: "Primary" });
    const labels = within(nav)
      .getAllByRole("link")
      .map((link) => link.textContent?.replace(/\s+/g, " ").trim());
    expect(labels).toEqual(["Home", "Missions", "Replay", "Profile"]);
  });
});
