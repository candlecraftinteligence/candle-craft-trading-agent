import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { MissionDetailScreen } from "../screens/MissionDetailScreen";
import { missionPathFromStartParam } from "../telegram/startParam";

describe("mission deep links", () => {
  it("routes a startapp id and refuses a path escape", () => {
    expect(missionPathFromStartParam("not_a_real_mission")).toBe("/missions/not_a_real_mission");
    expect(missionPathFromStartParam("mock_setup_btc_h1_active")).toBe("/missions/mock_setup_btc_h1_active");
    expect(missionPathFromStartParam("../secrets")).toBeNull();
    expect(missionPathFromStartParam("")).toBeNull();
    expect(missionPathFromStartParam(null)).toBeNull();
  });

  it("shows a not-found state for an unknown mission", async () => {
    render(
      <MemoryRouter initialEntries={["/missions/not_a_real_mission"]}>
        <Routes>
          <Route path="/missions/:id" element={<MissionDetailScreen />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByTestId("mission-missing")).toHaveTextContent(
      "This mission is not in the fixture set.",
    );
  });
});