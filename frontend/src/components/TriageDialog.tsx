import { Alert, Spinner } from "@blueprintjs/core";
import { useEffect, useState } from "react";

import { api } from "../api";
import type { TriageEstimate } from "../types";

/** Retroactive triage. Routes through the same handler the webhook does, and
 *  quotes the bill before spending it. */
export function TriageDialog({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
  const [estimate, setEstimate] = useState<TriageEstimate | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isOpen) {
      setEstimate(null);
      return;
    }
    api
      .estimateTriage()
      .then(setEstimate)
      .catch((err: Error) => setError(err.message));
  }, [isOpen]);

  return (
    <Alert
      isOpen={isOpen}
      icon="predictive-analysis"
      intent="primary"
      cancelButtonText="Cancel"
      confirmButtonText="Triage"
      onCancel={onClose}
      onConfirm={() => {
        void api.triage().catch((err: Error) => setError(err.message));
        onClose();
      }}
    >
      {error && <p className="bp5-text-danger">{error}</p>}
      {estimate === null ? (
        <Spinner size={20} />
      ) : (
        <p>
          {estimate.issue_count} issues, est. {estimate.estimated_acu.toFixed(1)} ACU in one scout
          session. Proceed?
        </p>
      )}
    </Alert>
  );
}
