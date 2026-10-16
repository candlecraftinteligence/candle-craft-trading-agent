import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import type { Mission } from "../api/types";
import { ReplayDrill } from "../components/ReplayDrill";
import { REPLAY_DISCLAIMER } from "../copy";

const mission: Mission = {
  cci_setup_id: "mock_setup_avax_h1_tp",
  symbol: "AVAXUSDT",
  timeframe: "H1",
  direction: "LONG",
  quality_tier: "HUNT",
  title: "Partial targets, then full resolution",
  thesis_summary: "The fixture records TP_HIT.",
  evidence: [{ type: "level", label: "Target path", detail: "TP1 and TP2 are recorded." }],
  lifecycle: [
    {
      cci_event_id: "evt_avax_h1_09",
      event_type: "SETUP_RESOLVED",
      state: "CLOSED",
      occurred_at: "2026-09-19T18:41:00Z",
      outcome_code: "TP_HIT",
    },
  ],
  lifecycle_state: "CLOSED",
  outcome_code: "TP_HIT",
  opened_at: "2026-09-17T08:00:00Z",
  resolved_at: "2026-09-19T18:41:00Z",
  resolved: true,
  synthetic: true,
  disclaimer: "Synthetic.",
  replay: {
    masked_title: "Continuation base",
    masked_thesis: "The later record is withheld.",
    teaching_note: "This file resolved TP_HIT. The aligned training decision is TRACK.",
    preferred_decision: "TRACK",
    evidence: [{ type: "structure", label: "Continuation base", detail: "The base is the reference." }],
  },
};

describe("replay conceal and reveal", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("hides the symbol, outcome, and teaching note until reveal", async () => {
    const user = userEvent.setup();
    render(<ReplayDrill mission={mission} onExit={() => undefined} />);

    expect(screen.queryByText("AVAXUSDT")).not.toBeInTheDocument();
    expect(screen.queryByText("TP_HIT")).not.toBeInTheDocument();
    expect(screen.queryByText(/aligned training decision/i)).not.toBeInTheDocument();
    expect(screen.getByTestId("replay-disclaimer")).toHaveTextContent(REPLAY_DISCLAIMER);
    expect(screen.getByRole("button", { name: "Reveal" })).toBeDisabled();

    await user.click(screen.getByRole("checkbox", { name: "Evidence reviewed" }));
    await user.click(screen.getByRole("button", { name: "HUNT" }));
    await user.click(screen.getByRole("button", { name: "TRACK" }));
    await user.click(screen.getByRole("button", { name: "Reveal" }));

    expect(await screen.findByRole("heading", { name: "AVAXUSDT" })).toBeInTheDocument();
    expect(screen.getByTestId("replay-outcome")).toHaveTextContent("TP_HIT");
    expect(screen.getByText(/aligned training decision is TRACK/i)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Training score" })).toHaveTextContent("100");
    expect(screen.getByTestId("replay-disclaimer")).toHaveTextContent(REPLAY_DISCLAIMER);
    expect(screen.getByTestId("xp-preview")).toHaveTextContent("+15");
    expect(screen.getByTestId("xp-preview")).toHaveTextContent("+25");
  });
});
