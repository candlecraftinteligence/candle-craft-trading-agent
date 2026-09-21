import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import type { Mission } from "../api/types";
import { MissionDetailBody } from "../screens/MissionDetailScreen";

const mission: Mission = {
  cci_setup_id: "mock_setup_journal",
  symbol: "AVAXUSDT",
  timeframe: "H1",
  direction: "LONG",
  quality_tier: "HUNT",
  title: "Resolved fixture",
  thesis_summary: "Synthetic thesis for the separation test.",
  evidence: [{ type: "structure", label: "Base", detail: "A base." }],
  lifecycle: [
    {
      cci_event_id: "evt_journal_01",
      event_type: "SETUP_PUBLISHED",
      state: "ACTIVE",
      occurred_at: "2026-09-17T08:00:00Z",
    },
    {
      cci_event_id: "evt_journal_02",
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
  disclaimer: "Synthetic training fixture.",
  replay: null,
};

describe("CCI outcome and journal", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("keeps a self-reported result out of the CCI outcome", async () => {
    const user = userEvent.setup();
    render(<MissionDetailBody mission={mission} />);

    const outcome = screen.getByTestId("cci-outcome");
    const journal = screen.getByTestId("user-journal");
    expect(outcome).toHaveTextContent("CCI Outcome");
    expect(outcome).toHaveTextContent("TP_HIT");
    expect(journal).toHaveTextContent("USER-REPORTED");
    expect(within(outcome).queryByText("WIN")).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("Note"), "I waited.");
    await user.click(screen.getByRole("radio", { name: "MEASURED" }));
    await user.type(screen.getByLabelText("Risk-plan note"), "Invalidation was the base low.");
    await user.click(screen.getByRole("radio", { name: "WIN" }));
    await user.type(screen.getByLabelText("Lesson"), "The pass would also have been valid.");
    await user.click(screen.getByRole("button", { name: "Save journal" }));

    expect(await within(screen.getByTestId("user-journal")).findByText("WIN")).toBeInTheDocument();
    expect(within(screen.getByTestId("cci-outcome")).getByText("TP_HIT")).toBeInTheDocument();
    expect(within(screen.getByTestId("cci-outcome")).queryByText("WIN")).not.toBeInTheDocument();
    expect(within(screen.getByTestId("user-journal")).getByText("WIN")).toBeInTheDocument();
    expect(screen.getByTestId("user-journal")).toHaveTextContent("USER-REPORTED");
  });
});
