import { Button, Card, Colors, Icon, Tag } from "@blueprintjs/core";
import { type CSSProperties, useState } from "react";

import type { IssueCard, State } from "../types";

/** Four columns, eleven labels: the label set is the state machine and stays
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
  /** Starts as a rail. For a column that is only ever read deliberately: the
   *  declines are the majority of any real backlog and none of them is a thing
   *  to do, so they earn a count and nothing else until asked for. */
  collapsed?: boolean;
}[] = [
  {
    key: "backlog",
    label: "Backlog",
    states: [null, "needs-triage", "devin-eligible"],
    accent: Colors.GRAY1,
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
    states: ["can-close-issue", "done"],
    intent: "success",
    accent: Colors.GREEN2,
  },
  {
    key: "declined",
    label: "Declined",
    states: ["devin-declined"],
    accent: Colors.GRAY3,
    collapsed: true,
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

function sinceStamp(stamp: string | null): string {
  if (!stamp) return "just now";
  const at = Date.parse(stamp);
  return Number.isNaN(at) ? "just now" : since(at / 1000);
}

const PROGRESS_LABEL: Record<string, string> = {
  "drafting-pr": "drafting PR",
  "pr-opened": "PR opened",
};

const UNLABELLED = "unlabelled";

function statusKey(card: IssueCard): string {
  return card.state ?? UNLABELLED;
}

/** Filters on the GitHub status, not on the column: the columns are a grouping
 *  of eleven labels, and the label is the thing a maintainer actually asks
 *  about ("show me what CI is failing on"). Nothing selected means everything. */
function BoardFilters({
  cards,
  selected,
  onChange,
}: {
  cards: IssueCard[];
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
}) {
  const counts = new Map<string, number>();
  for (const card of cards) {
    const key = statusKey(card);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const order = [UNLABELLED, ...COLUMNS.flatMap((c) => c.states)].filter(
    (key): key is string => key !== null,
  );
  const present = order.filter((key) => counts.has(key));

  const toggle = (key: string) => {
    const next = new Set(selected);
    if (!next.delete(key)) next.add(key);
    onChange(next);
  };

  return (
    <div className="board__filters">
      <span className="bp5-text-muted">Status</span>
      {present.map((key) => (
        <Tag
          key={key}
          round
          interactive
          minimal={!selected.has(key)}
          intent={selected.has(key) ? "primary" : undefined}
          onClick={() => toggle(key)}
        >
          {(STATE_LABEL[key] ?? key)} {counts.get(key)}
        </Tag>
      ))}
      {selected.size > 0 && (
        <Button
          minimal
          small
          icon="cross"
          text="Clear"
          onClick={() => onChange(new Set())}
        />
      )}
    </div>
  );
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
        {/* Narration, and only until GitHub can speak for itself: once the PR
            exists the card has a real state to show and the worker's account of
            what it was doing is no longer the most interesting thing on it. */}
        {card.progress_phase && !card.meta.pr_url && columnKey(card) === "working" && (
          <Tag round intent="primary" icon="build">
            {PROGRESS_LABEL[card.progress_phase] ?? card.progress_phase} ·{" "}
            {sinceStamp(card.progress_at)}
          </Tag>
        )}
        {card.ready_to_merge && (
          <Tag round intent="success" icon="git-merge">
            ready to merge
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
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const visible =
    selected.size === 0
      ? cards
      : cards.filter((card) => selected.has(statusKey(card)));

  const toggle = (key: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (!next.delete(key)) next.add(key);
      return next;
    });

  return (
    <>
      <BoardFilters cards={cards} selected={selected} onChange={setSelected} />
      <div className="board">
        {COLUMNS.map((column) => {
          const items = visible.filter(
            (card) => columnKey(card) === column.key,
          );
          // Filtering on a status is asking for it explicitly, so a chip that
          // selects declines opens the rail rather than hiding the answer.
          const open =
            !column.collapsed ||
            expanded.has(column.key) ||
            column.states.some(
              (state) => state !== null && selected.has(state),
            );
          return (
            <div
              className={open ? "column" : "column column--rail"}
              key={column.key}
              style={{ "--accent": column.accent } as CSSProperties}
            >
              <div
                className="column__head"
                onClick={column.collapsed ? () => toggle(column.key) : undefined}
                style={column.collapsed ? { cursor: "pointer" } : undefined}
              >
                <span>
                  {column.collapsed && (
                    <Icon icon={open ? "chevron-down" : "chevron-right"} size={11} />
                  )}{" "}
                  {column.label}
                </span>
                <Tag round intent={column.intent}>
                  {items.length}
                </Tag>
              </div>
              {open &&
                items.map((card) => (
                  <IssueTile key={card.number} card={card} onSelect={onSelect} />
                ))}
            </div>
          );
        })}
      </div>
    </>
  );
}
