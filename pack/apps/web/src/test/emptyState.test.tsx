import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { QUIET_MARKET } from "../copy";
import { HomeScreen } from "../screens/HomeScreen";
import { fetchMissions } from "../api/missions";

vi.mock("../api/missions", () => ({
  fetchMissions: vi.fn(),
}));

const fetchMissionsMock = vi.mocked(fetchMissions);

describe("home empty state", () => {
  beforeEach(() => {
    window.localStorage.clear();
    fetchMissionsMock.mockReset();
  });

  it("emphasizes the quiet tape and Replay when nothing is open", async () => {
    fetchMissionsMock.mockResolvedValue([]);
    render(
      <MemoryRouter>
        <HomeScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText(QUIET_MARKET)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText("On the board")).not.toBeInTheDocument();
      expect(screen.queryByText("The Pack has eyes on these.")).not.toBeInTheDocument();
    });
    expect(screen.getByRole("link", { name: "Run the tape" })).toHaveAttribute("href", "/replay");
    expect(screen.queryByRole("link", { name: "Walk the board" })).not.toBeInTheDocument();
  });

  it("keeps the open-mission preview when a fixture is open", async () => {
    fetchMissionsMock.mockResolvedValue([
      {
        cci_setup_id: "mock_setup_btc_h1_active",
        symbol: "BTCUSDT",
        timeframe: "H1",
        direction: "LONG",
        quality_tier: "HUNT",
        title: "Session-low sweep and reclaim",
        thesis_summary: "Synthetic.",
        evidence: [],
        lifecycle: [],
        lifecycle_state: "ACTIVE",
        outcome_code: null,
        opened_at: "2026-09-21T14:00:00Z",
        resolved_at: null,
        resolved: false,
        synthetic: true,
        disclaimer: "Synthetic.",
        replay: null,
      },
    ]);
    render(
      <MemoryRouter>
        <HomeScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("On the board")).toBeInTheDocument();
    expect(screen.getByText("The Pack has eyes on these.")).toBeInTheDocument();
    expect(screen.getByText(QUIET_MARKET)).toBeInTheDocument();
    expect(screen.getByText("BTCUSDT")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Walk the board" })).toBeInTheDocument();
  });
});
