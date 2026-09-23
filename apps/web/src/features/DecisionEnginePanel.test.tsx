import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DecisionEnginePanel } from "./DecisionEnginePanel";

const state = vi.hoisted(() => ({
  data: {} as Record<string, unknown>,
  mutate: vi.fn().mockResolvedValue({}),
}));
vi.mock("../hooks/useApi", () => ({
  useApiDetail: () => ({ data: state.data }),
  useApiMutation: () => ({ mutateAsync: state.mutate, isPending: false }),
}));

afterEach(cleanup);
beforeEach(() => {
  state.mutate.mockClear();
  state.data = {
    enabled: true,
    items: [
      {
        id: "decision-1",
        status: "available",
        diagnosis: "communication_failure",
        recommended_action: "retry_step",
        severity: "medium",
        policy: "human_review",
        confidence: { recommended_action: 0.88 },
        evidence: '{"network":"timeout"}',
      },
    ],
  };
});

describe("Decision Engine", () => {
  it("shows recommendation, review status and exact evidence", () => {
    render(<DecisionEnginePanel runId="run-1" canDiagnose={true} />);
    expect(screen.getByText("88%")).toBeInTheDocument();
    expect(screen.getByText("Human review required")).toBeInTheDocument();
    expect(screen.getByText('{"network":"timeout"}')).toBeInTheDocument();
    expect(screen.getByText(/do not override deterministic/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Diagnose completed run" }));
    expect(state.mutate).toHaveBeenCalledTimes(1);
  });
  it("hides disabled and shadow results", () => {
    state.data = { enabled: false, items: [] };
    const { container } = render(<DecisionEnginePanel runId="run-1" canDiagnose={true} />);
    expect(container).toBeEmptyDOMElement();
  });
  it("preserves deterministic results on diagnosis failure", () => {
    state.data = { enabled: true, items: [{ status: "unavailable" }] };
    render(<DecisionEnginePanel runId="run-1" canDiagnose={true} />);
    expect(
      screen.getByText("Diagnosis unavailable. Test results are unchanged."),
    ).toBeInTheDocument();
  });
  it("does not show mutation controls to readers", () => {
    render(<DecisionEnginePanel runId="run-1" canDiagnose={false} />);
    expect(
      screen.queryByRole("button", { name: "Diagnose completed run" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Record operator decision" }),
    ).not.toBeInTheDocument();
  });
  it("records feedback without executing recovery", async () => {
    render(<DecisionEnginePanel runId="run-1" canDiagnose={true} />);
    fireEvent.change(screen.getByLabelText("Operator decision"), { target: { value: "rejected" } });
    fireEvent.click(screen.getByRole("button", { name: "Record operator decision" }));
    expect(
      await screen.findByText("Operator decision recorded. No action was executed."),
    ).toBeInTheDocument();
  });
});
