import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MissionsScreen } from "../screens/MissionsScreen";
import { fetchMissions } from "../api/missions";

vi.mock("../api/missions", () => ({
  fetchMissions: vi.fn(),
}));

vi.mock("../api/profile", () => ({
  usePackProfile: vi.fn(),
}));

import { usePackProfile } from "../api/profile";

const fetchMissionsMock = vi.mocked(fetchMissions);
const profileMock = vi.mocked(usePackProfile);

const btc = {
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
};

describe("missions MINE filter", () => {
  beforeEach(() => {
    fetchMissionsMock.mockReset();
    profileMock.mockReset();
    fetchMissionsMock.mockResolvedValue([btc]);
  });

  it("shows only missions the server profile has locked", async () => {
    const user = userEvent.setup();
    profileMock.mockReturnValue({
      display_name: "Ada",
      telegram_user_id: 7,
      pack_xp: 25,
      wolf_rank: "SCOUT",
      discipline_streak: 1,
      decisions: { mock_setup_btc_h1_active: "NO_TRADE" },
      journal_ids: [],
      replay_count: 0,
      no_trade_count: 1,
    });
    render(
      <MemoryRouter>
        <MissionsScreen />
      </MemoryRouter>,
    );
    expect(await screen.findByText("BTCUSDT")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "MINE" }));
    expect(screen.getByText("BTCUSDT")).toBeInTheDocument();
  });

  it("does not treat a missing profile as a local lock", async () => {
    const user = userEvent.setup();
    profileMock.mockReturnValue(null);
    render(
      <MemoryRouter>
        <MissionsScreen />
      </MemoryRouter>,
    );
    expect(await screen.findByText("BTCUSDT")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "MINE" }));
    expect(screen.queryByText("BTCUSDT")).not.toBeInTheDocument();
    expect(screen.getByText("N/A")).toBeInTheDocument();
  });

  it("says no calls are sealed when the profile has no locks", async () => {
    const user = userEvent.setup();
    profileMock.mockReturnValue({
      display_name: "Ada",
      telegram_user_id: 7,
      pack_xp: 0,
      wolf_rank: "SCOUT",
      discipline_streak: 0,
      decisions: {},
      journal_ids: [],
      replay_count: 0,
      no_trade_count: 0,
    });
    render(
      <MemoryRouter>
        <MissionsScreen />
      </MemoryRouter>,
    );
    await screen.findByText("BTCUSDT");
    await user.click(screen.getByRole("button", { name: "MINE" }));
    expect(screen.getByText("No calls sealed yet.")).toBeInTheDocument();
    expect(screen.queryByText("BTCUSDT")).not.toBeInTheDocument();
  });
});
