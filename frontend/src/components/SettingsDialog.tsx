import { Button, Callout, Dialog, DialogBody, DialogFooter, NumericInput } from "@blueprintjs/core";
import { useEffect, useState } from "react";

import { api } from "../api";
import type { ConfigPayload } from "../types";

const FIELDS: { key: string; label: string; min: number; max: number; step: number }[] = [
  { key: "confidence_threshold", label: "Dispatch confidence threshold", min: 0, max: 1, step: 0.05 },
  { key: "max_ci_rounds", label: "Max CI autofix rounds", min: 1, max: 10, step: 1 },
  { key: "max_concurrent_workers", label: "Max concurrent workers", min: 1, max: 20, step: 1 },
  { key: "scout_batch_max", label: "Max issues per scout batch", min: 1, max: 50, step: 1 },
  { key: "batch_window_seconds", label: "Webhook batch window (s)", min: 5, max: 300, step: 5 },
];

export function SettingsDialog({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
  const [config, setConfig] = useState<ConfigPayload | null>(null);
  const [values, setValues] = useState<Record<string, number>>({});
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!isOpen) return;
    setSaved(false);
    api
      .config()
      .then((payload) => {
        setConfig(payload);
        setValues(payload.effective);
      })
      .catch((err: Error) => setError(err.message));
  }, [isOpen]);

  async function save() {
    try {
      await api.putConfig(values);
      setSaved(true);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <Dialog isOpen={isOpen} onClose={onClose} title="Settings" icon="cog">
      <DialogBody>
        {error && <Callout intent="danger">{error}</Callout>}
        {/* Settings live in the fork, not on local disk: same bus as everything
            else, and every change is a reviewable commit. */}
        <Callout intent="primary" icon="git-commit">
          Written to <code>{config?.path ?? ".cgsol/config.yaml"}</code> in{" "}
          <code>{config?.repo ?? "the fork"}</code>. The orchestrator reloads on the push webhook.
        </Callout>
        {FIELDS.map((field) => (
          <div key={field.key} style={{ marginTop: 12 }}>
            <div className="metric__label">{field.label}</div>
            <NumericInput
              value={values[field.key] ?? 0}
              min={field.min}
              max={field.max}
              stepSize={field.step}
              onValueChange={(value) => setValues({ ...values, [field.key]: value })}
              fill
            />
          </div>
        ))}
        {saved && <Callout intent="success" style={{ marginTop: 12 }}>Committed.</Callout>}
      </DialogBody>
      <DialogFooter
        actions={
          <>
            <Button text="Close" onClick={onClose} />
            <Button intent="primary" text="Commit to fork" onClick={() => void save()} />
          </>
        }
      />
    </Dialog>
  );
}
