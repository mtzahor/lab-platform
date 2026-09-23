import { useState } from "react";
import { apiFetch, errorMessage, records, type ApiRecord } from "../api/client";
import { Button, DetailGrid, Panel } from "../components/ui";
import { useApiDetail, useApiMutation } from "../hooks/useApi";
import { titleCase } from "../lib/format";

export function DecisionEnginePanel({
  runId,
  canDiagnose,
}: {
  runId: string;
  canDiagnose: boolean;
}) {
  const path = `/api/v1/workflow-runs/${runId}`;
  const query = useApiDetail("run-diagnoses", `${path}/diagnoses`);
  const [error, setError] = useState<string>();
  const [saved, setSaved] = useState(false);
  const [notes, setNotes] = useState("");
  const [outcome, setOutcome] = useState("accepted");
  const diagnosis = records(query.data)[0];
  const mutation = useApiMutation(
    () => apiFetch<ApiRecord>(`${path}/diagnose`, { method: "POST" }),
    [["run-diagnoses"]],
  );
  const feedback = useApiMutation(() =>
    apiFetch(`${path}/diagnoses/${String(diagnosis?.id)}/feedback`, {
      method: "POST",
      body: JSON.stringify({ outcome, notes }),
    }),
  );
  async function diagnose() {
    setError(undefined);
    setSaved(false);
    try {
      await mutation.mutateAsync();
    } catch (cause) {
      setError(errorMessage(cause));
    }
  }
  async function saveFeedback() {
    setError(undefined);
    try {
      await feedback.mutateAsync();
      setSaved(true);
    } catch (cause) {
      setError(errorMessage(cause));
    }
  }
  const confidence = diagnosis?.confidence as ApiRecord | undefined;
  if (query.data?.enabled === false) return null;
  return (
    <Panel title="Decision Engine" description="Experimental diagnosis · Recommendation only">
      <p>Jev recommendations do not override deterministic test limits or hardware safety rules.</p>
      {canDiagnose && (
        <Button onClick={() => void diagnose()} disabled={mutation.isPending}>
          {mutation.isPending ? "Diagnosing…" : "Diagnose completed run"}
        </Button>
      )}
      {query.error && <p role="alert">{errorMessage(query.error)}</p>}
      {error && <p role="alert">{error}</p>}
      {diagnosis?.status === "unavailable" && (
        <p role="status">Diagnosis unavailable. Test results are unchanged.</p>
      )}
      {diagnosis?.status === "available" && (
        <>
          <DetailGrid
            items={[
              { label: "Likely cause", value: titleCase(String(diagnosis.diagnosis)) },
              {
                label: "Recommended action",
                value: titleCase(String(diagnosis.recommended_action)),
              },
              {
                label: "Action confidence",
                value: `${Math.round(Number(confidence?.recommended_action ?? 0) * 100)}%`,
              },
              {
                label: "Retry safety probability",
                value: `${Math.round(Number(diagnosis.retry_safe_probability ?? 0) * 100)}%`,
              },
              { label: "Severity", value: titleCase(String(diagnosis.severity)) },
              {
                label: "Status",
                value:
                  diagnosis.policy === "human_review"
                    ? "Human review required"
                    : diagnosis.policy === "reject"
                      ? "Rejected by policy"
                      : "Recommendation only",
              },
            ]}
          />
          <p>Confidence estimates require calibration against recorded lab outcomes.</p>
          {canDiagnose && (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void saveFeedback();
              }}
            >
              <label>
                Operator decision
                <select
                  value={outcome}
                  onChange={(event) => {
                    setOutcome(event.target.value);
                    setSaved(false);
                  }}
                >
                  <option value="accepted">Accepted recommendation</option>
                  <option value="rejected">Rejected recommendation</option>
                  <option value="different_action_taken">Different action taken</option>
                </select>
              </label>
              <label>
                Outcome notes (exclude secrets and personal information)
                <textarea
                  value={notes}
                  maxLength={2000}
                  onChange={(event) => {
                    setNotes(event.target.value);
                    setSaved(false);
                  }}
                />
              </label>
              <Button type="submit" disabled={feedback.isPending}>
                Record operator decision
              </Button>
              {saved && <p role="status">Operator decision recorded. No action was executed.</p>}
            </form>
          )}
        </>
      )}
      {typeof diagnosis?.evidence === "string" && (
        <details>
          <summary>Why am I seeing this?</summary>
          <p>This is the exact structured evidence sent for diagnosis.</p>
          <pre className="log-viewer compact-log">{diagnosis.evidence}</pre>
        </details>
      )}
    </Panel>
  );
}
