import { Card, Colors, Icon, Tag } from "@blueprintjs/core";
import type { CSSProperties } from "react";

import type { IssueCard, State } from "../types";

/** The autofix loop is a story worth showing, so ci-failing and devin-fixing
 *  are first-class columns rather than a badge on devin-pr-open. */
export const COLUMNS: {
  state: State;
  label: string;
  intent?: "danger" | "warning" | "success" | "primary";
  accent: string;
}[] = [
  { state: "needs-triage", label: "Needs triage", accent: Colors.GRAY1 },
  {
    state: "devin-eligible",
    label: "Eligible",
    intent: "primary",
    accent: Colors.BLUE2,
  },
  {
    state: "devin-working",
    label: "Working",
    intent: "primary",
    accent: Colors.INDIGO2,
  },
  {
    state: "devin-pr-open",
    label: "PR open",
    intent: "primary",
    accent: Colors.CERULEAN2,
  },
  {
    state: "ci-failing",
    label: "CI failing",
    intent: "danger",
    accent: Colors.RED2,
  },
  {
    state: "devin-fixing",
    label: "Devin fixing",
    intent: "warning",
    accent: Colors.ORANGE2,
  },
  {
    state: "human-review",
    label: "Human review",
    intent: "warning",
    accent: Colors.GOLD2,
  },
  { state: "done", label: "Done", intent: "success", accent: Colors.GREEN2 },
  {
    state: "can-close-issue",
    label: "Can close",
    intent: "success",
    accent: Colors.FOREST2,
  },
  { state: "devin-declined", label: "Declined", accent: Colors.GRAY2 },
  {
    state: "devin-blocked",
    label: "Blocked",
    intent: "danger",
    accent: Colors.VERMILION2,
  },
];

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
        {card.tier && (
          <Tag round intent={TIER_INTENT[card.tier]}>
            {card.tier.replace("tier:", "")}
          </Tag>
        )}
        {card.meta.confidence !== null && (
          <Tag round minimal>
            conf {card.meta.confidence.toFixed(2)}
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
        {card.meta.escalation && (
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
  const untriaged = cards.filter((card) => card.state === null);
  return (
    <div className="board">
      {untriaged.length > 0 && (
        <div
          className="column"
          style={{ "--accent": Colors.GRAY1 } as CSSProperties}
        >
          <div className="column__head">
            <span>Unlabelled</span>
            <Tag round>{untriaged.length}</Tag>
          </div>
          {untriaged.map((card) => (
            <IssueTile key={card.number} card={card} onSelect={onSelect} />
          ))}
        </div>
      )}
      {COLUMNS.map((column) => {
        const items = cards.filter((card) => card.state === column.state);
        return (
          <div
            className="column"
            key={column.state}
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
