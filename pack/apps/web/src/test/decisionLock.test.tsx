import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { DecisionPanel } from "../components/DecisionPanel";
import { lockDecision } from "../decisions/localDecisions";

describe("decision lock", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("keeps the first choice and ignores a later click", async () => {
    const user = userEvent.setup();
    render(<DecisionPanel missionId="mock_setup_btc_h1_active" />);

    await user.click(screen.getByRole("button", { name: "NO TRADE" }));
    expect(await screen.findByTestId("decision-lock")).toHaveTextContent("NO TRADE");

    expect(screen.getByRole("button", { name: "NO TRADE" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("decision-lock")).toHaveTextContent("NO TRADE");
    expect(screen.getByRole("button", { name: "TRACK" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "I TOOK THIS" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "WATCH ONLY" })).toBeDisabled();

    expect(lockDecision("mock_setup_btc_h1_active", "TRACK")).toBe("NO_TRADE");

    expect(screen.getByRole("button", { name: "NO TRADE" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "TRACK" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByTestId("decision-lock")).toHaveTextContent("NO TRADE");
    expect(window.localStorage.getItem("cci-pack.decisions.v1")).toContain("NO_TRADE");
    expect(window.localStorage.getItem("cci-pack.decisions.v1")).not.toContain("\"TRACK\"");
  });
});
