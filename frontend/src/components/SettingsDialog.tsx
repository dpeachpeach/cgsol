import {
  Button,
  Callout,
  Collapse,
  Dialog,
  DialogBody,
  DialogFooter,
  HTMLSelect,
  NumericInput,
  Radio,
  RadioGroup,
} from "@blueprintjs/core";
import { useEffect, useState } from "react";

import { api } from "../api";
import type { ConfigPayload, TriageMode } from "../types";

/** Kept short on purpose: cadence is the only thing most operators need, and a
 *  dialog of a dozen knobs reads as a system nobody has an opinion about. */
const ADVANCED: {
  key: string;
  label: string;
  min: number;
  max: number;
  step: number;
}[] = [
  {
    key: "confidence_threshold",
    label: "Dispatch confidence threshold",
    min: 0,
    max: 1,
    step: 0.05,
  },
  {
    key: "max_ci_rounds",
    label: "Max CI autofix rounds",
    min: 1,
    max: 10,
    step: 1,
  },
  {
    key: "max_concurrent_workers",
    label: "Max concurrent workers",
    // Zero is the stop button. A floor of one would mean the dialog could start
    // spending but never stop it, which is the wrong asymmetry for the control
    // that governs cost.
    min: 0,
    max: 20,
    step: 1,
  },
  {
    key: "scout_batch_max",
    label: "Max issues per scout batch",
    min: 1,
    max: 50,
    step: 1,
  },
  {
    key: "batch_window_seconds",
    label: "Webhook batch window (s)",
    min: 5,
    max: 300,
    step: 5,
  },
];

const INTERVALS: { seconds: number; label: string }[] = [
  { seconds: 900, label: "Every 15 minutes" },
  { seconds: 1800, label: "Every 30 minutes" },
  { seconds: 3600, label: "Every hour" },
  { seconds: 21600, label: "Every 6 hours" },
  { seconds: 86400, label: "Every 24 hours" },
];

const MODE_HELP: Record<TriageMode, string> = {
  auto: "Every new issue is triaged as soon as it arrives. Lowest latency, least control over spend.",
  chunked:
    "New issues pile up and are triaged together on a schedule. One scout session per sweep.",
  manual: "Nothing is triaged until you press Triage backlog.",
};

export function SettingsDialog({
  isOpen,
  onClose,
}: {
  isOpen: boolean;
  onClose: () => void;
}) {
  const [config, setConfig] = useState<ConfigPayload | null>(null);
  const [values, setValues] = useState<Record<string, number | string>>({});
  const [advancedOpen, setAdvancedOpen] = useState(false);
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

  const mode = (values.triage_mode as TriageMode | undefined) ?? "manual";
  const interval = Number(values.triage_interval_seconds ?? 1800);

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
        <RadioGroup
          label="Triage cadence"
          selectedValue={mode}
          onChange={(event) =>
            setValues({
              ...values,
              triage_mode: event.currentTarget.value as TriageMode,
            })
          }
        >
          <Radio label="Auto — triage each issue on arrival" value="auto" />
          <Radio label="Chunked — triage on a schedule" value="chunked" />
          <Radio label="Manual — only when I ask" value="manual" />
        </RadioGroup>
        <Callout icon="info-sign">{MODE_HELP[mode]}</Callout>
        {mode === "chunked" && (
          <div style={{ marginTop: 12 }}>
            <div className="metric__label">Sweep interval</div>
            <HTMLSelect
              fill
              value={interval}
              onChange={(event) =>
                setValues({
                  ...values,
                  triage_interval_seconds: Number(event.currentTarget.value),
                })
              }
            >
              {INTERVALS.map((option) => (
                <option key={option.seconds} value={option.seconds}>
                  {option.label}
                </option>
              ))}
            </HTMLSelect>
            {config?.next_chunk_at != null && (
              <div className="metric__sub" style={{ marginTop: 4 }}>
                Next sweep{" "}
                {new Date(config.next_chunk_at * 1000).toLocaleTimeString()}
              </div>
            )}
          </div>
        )}

        <Button
          minimal
          small
          style={{ marginTop: 16 }}
          icon={advancedOpen ? "chevron-down" : "chevron-right"}
          text="Advanced"
          onClick={() => setAdvancedOpen(!advancedOpen)}
        />
        <Collapse isOpen={advancedOpen}>
          {ADVANCED.map((field) => (
            <div key={field.key} style={{ marginTop: 12 }}>
              <div className="metric__label">{field.label}</div>
              <NumericInput
                value={Number(values[field.key] ?? 0)}
                min={field.min}
                max={field.max}
                stepSize={field.step}
                onValueChange={(value) =>
                  setValues({ ...values, [field.key]: value })
                }
                fill
              />
            </div>
          ))}
        </Collapse>

        {/* Settings live in the fork, not on local disk: same bus as everything
            else, and every change is a reviewable commit. */}
        <Callout intent="primary" icon="git-commit" style={{ marginTop: 16 }}>
          Written to <code>{config?.path ?? ".cgsol/config.yaml"}</code> in{" "}
          <code>{config?.repo ?? "the fork"}</code>. The orchestrator reloads on
          the push webhook.
        </Callout>
        {saved && (
          <Callout intent="success" style={{ marginTop: 12 }}>
            Committed.
          </Callout>
        )}
      </DialogBody>
      <DialogFooter
        actions={
          <>
            <Button text="Close" onClick={onClose} />
            <Button
              intent="primary"
              text="Commit to fork"
              onClick={() => void save()}
            />
          </>
        }
      />
    </Dialog>
  );
}
