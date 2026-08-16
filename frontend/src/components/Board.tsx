import { Card, Colors, Icon, Tag } from "@blueprintjs/core";
import type { CSSProperties } from "react";

import type { IssueCard, State } from "../types";

/** Five columns, eleven labels: the label set is the state machine and stays
 *  as it is on GitHub, but a column per label spends the width of the board on
 *  transitions that are empty whenever nothing is mid-flight. Each card still
 *  carries its own label, so `ci-failing` is legible without a column of its
 *  own. */
export const COLUMNS: {
  key: string;
  label: string;
  states: (State | null)[];
  intent?: "danger" | "warning" | "success" | "primary";
  accent: string;
}[] = [
  {
    key: "needs-triage",
    label: "Needs triage",
    states: [null, "needs-triage"],
    accent: Colors.GRAY1,
  },
  {
    key: "triaged",
    label: "Triaged",
    states: ["devin-eligible"],
    intent: "primary",
    accent: Colors.BLUE2,
  },
  {
    key: "working",
    label: "Devin working",
    states: ["devin-working", "devin-pr-open", "ci-failing", "devin-fixing"],
    intent: "primary",
    accent: Colors.INDIGO2,
  },
  {
    key: "human",
    label: "Request human",
    states: ["human-review", "devin-blocked"],
    intent: "warning",
    accent: Colors.GOLD2,
  },
  {
    key: "ready",
    label: "Ready to close / merge",
    // Declined sits here rather than under "request human": the pipeline is
    // finished with it and the only move left is a close. Keeping it out of the
    // human column stops thirteen "not for an agent" verdicts from burying the
    // handful that genuinely want a person.
    states: ["can-close-issue", "done", "devin-declined"],
    intent: "success",
    accent: Colors.GREEN2,
  },
];

const CONFIDENT = 0.3;

/** `human-review` covers two unlike things: work that finished and wants a
 *  merge, and work the pipeline refused to trust. Only the second is a request
 *  for a person, so a confident card is filed as ready rather than pending. An
 *  escalation of any other kind (ci-unfixable, ambiguous-requirement) always
 *  wants the human, however confident the analyst was. */
function columnKey(card: IssueCard): string {
  if (card.state !== "human-review") {
    return (
      COLUMNS.find((column) => column.states.includes(card.state))?.key ?? ""
    );
  }
  const escalation = card.meta.escalation;
  if (escalation && escalation !== "low-confidence") return "human";
  const confidence = card.meta.confidence;
  return confidence === null || confidence >= CONFIDENT ? "ready" : "human";
}

const STATE_LABEL: Record<string, string> = {
  "needs-triage": "needs triage",
  "devin-eligible": "eligible",
  "devin-working": "working",
  "devin-pr-open": "PR open",
  "ci-failing": "CI failing",
  "devin-fixing": "Devin fixing",
  "human-review": "human review",
  "devin-declined": "declined",
  "devin-blocked": "blocked",
  "can-close-issue": "can close",
  done: "done",
};

const STATE_INTENT: Record<string, "danger" | "warning" | "success" | "primary"> =
  {
    "devin-pr-open": "primary",
    "ci-failing": "danger",
    "devin-fixing": "warning",
    "devin-blocked": "danger",
    done: "success",
    "can-close-issue": "success",
  };

const TIER_INTENT: Record<string, "success" | "warning" | "danger"> = {
  "tier:trivial": "success",
  "tier:medium": "warning",
  "tier:hard": "danger",
};

function since(ts: number): string {
  if (!ts) return "never";
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (seconds < 60) return `${seconds}s ago`;
  return `${Math.round(seconds / 60)}m ago`;
}

function IssueTile({
  card,
  onSelect,
}: {
  card: IssueCard;
  onSelect: (n: number) => void;
}) {
  const failing = card.checks.filter((check) => check.conclusion === "failure");
  return (
    <Card
      className="card"
      interactive
      compact
      onClick={() => onSelect(card.number)}
    >
      <div className="card__title">
        <strong>#{card.number}</strong> {card.title}
      </div>
      <div className="card__meta">
        {card.state && (
          <Tag round minimal intent={STATE_INTENT[card.state]}>
            {STATE_LABEL[card.state] ?? card.state}
          </Tag>
        )}
        {card.tier && (
          <Tag round intent={TIER_INTENT[card.tier]}>
            {card.tier.replace("tier:", "")}
          </Tag>
        )}
        {card.meta.acus > 0 && (
          <Tag round minimal icon="dollar">
            {card.meta.acus.toFixed(2)} ACU
          </Tag>
        )}
        {card.meta.ci_rounds > 0 && (
          <Tag round intent="warning">
            CI ×{card.meta.ci_rounds}
          </Tag>
        )}
        {failing.length > 0 && (
          <Tag round intent="danger">
            {failing.length} red
          </Tag>
        )}
        {card.meta.escalation && columnKey(card) === "human" && (
          <Tag round intent="danger">
            {card.meta.escalation}
          </Tag>
        )}
      </div>
      <div className="card__foot">
        <span>
          {card.meta.session_id ? <Icon icon="pulse" size={11} /> : null} synced{" "}
          {since(card.last_synced)}
        </span>
        {card.meta.pr_url && <Icon icon="git-pull" size={11} />}
      </div>
    </Card>
  );
}

export function Board({
  cards,
  onSelect,
}: {
  cards: IssueCard[];
  onSelect: (n: number) => void;
}) {
  return (
    <div className="board">
      {COLUMNS.map((column) => {
        const items = cards.filter((card) => columnKey(card) === column.key);
        return (
          <div
            className="column"
            key={column.key}
            style={{ "--accent": column.accent } as CSSProperties}
          >
            <div className="column__head">
              <span>{column.label}</span>
              <Tag round intent={column.intent}>
                {items.length}
              </Tag>
            </div>
            {items.map((card) => (
              <IssueTile key={card.number} card={card} onSelect={onSelect} />
            ))}
          </div>
        );
      })}
    </div>
  );
}
