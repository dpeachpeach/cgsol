import { Card, HTMLTable, NonIdealState, Tag } from "@blueprintjs/core";

import type { Metrics } from "../types";

function pct(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

function num(value: number | null, digits = 2): string {
  return value === null ? "—" : value.toFixed(digits);
}

/** Durations here span minutes to weeks, so a fixed unit is unreadable at one
 *  end or the other. */
function duration(seconds: number | null): string {
  if (seconds === null) return "—";
  const minutes = seconds / 60;
  if (minutes < 90) return `${Math.round(minutes)}m`;
  const hours = minutes / 60;
  if (hours < 48) return `${hours.toFixed(1)}h`;
  return `${(hours / 24).toFixed(1)}d`;
}

function Metric({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <Card className="metric" compact>
      <div className="metric__label">{label}</div>
      <div className="metric__value">{value}</div>
      {sub && <div className="metric__sub">{sub}</div>}
    </Card>
  );
}

export function MetricsPanel({
  metrics,
  computedAt,
}: {
  metrics: Metrics | null;
  computedAt: number | null;
}) {
  if (metrics === null) {
    return <NonIdealState icon="chart" title="No metrics yet" />;
  }
  const { headline, funnel, by_tier: byTier, escalations } = metrics;
  const widest = Math.max(...Object.values(funnel), 1);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="bp5-text-muted" style={{ fontSize: 11 }}>
        Recomputed every minute
        {computedAt === null
          ? ""
          : ` · last at ${new Date(computedAt).toLocaleTimeString()}`}
      </div>
      <div className="metrics">
        <Metric
          label="Autonomy rate"
          value={pct(headline.autonomy_rate)}
          sub="merged PRs needing zero human turns"
        />
        <Metric
          label="ACU per merged PR"
          value={num(headline.acu_per_merged_pr)}
          sub={`${headline.merged} merged`}
        />
        <Metric
          label="CI rounds to green"
          value={num(headline.ci_rounds_to_green, 1)}
          sub="first-pass quality; should trend down"
        />
        <Metric
          label="Retired without code"
          value={String(headline.retired ?? 0)}
          sub="already fixed or duplicate; awaiting a human close"
        />
        <Metric
          label="Issue → PR"
          value={duration(headline.issue_to_pr_seconds)}
          sub={`mean over ${headline.issue_to_pr_count} worker PRs`}
        />
        <Metric
          label="Average age, open issues"
          value={duration(headline.open_age_seconds)}
          sub={`${headline.open_count} open · since the issue landed on this fork`}
        />
        <Metric
          label="Pipeline ACU"
          value={num(headline.total_acu)}
          sub={`build sessions: ${num(headline.build_acu)} (counted separately)`}
        />
      </div>

      <Card compact>
        <div className="metric__label" style={{ marginBottom: 8 }}>
          Funnel
        </div>
        {Object.entries(funnel).map(([stage, count]) => (
          <div className="funnel__row" key={stage}>
            <span style={{ width: 90 }}>{stage}</span>
            <div
              className="funnel__bar"
              style={{ width: `${(count / widest) * 60}%` }}
            />
            <span>{count}</span>
          </div>
        ))}
      </Card>

      <Card compact>
        <div className="metric__label" style={{ marginBottom: 8 }}>
          Spend against outcome, by tier
        </div>
        <HTMLTable compact striped style={{ width: "100%" }}>
          <thead>
            <tr>
              <th>tier</th>
              <th>issues</th>
              <th>ACU</th>
              <th>share of spend</th>
              <th>merge rate</th>
              <th>CI rounds</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(byTier).map(([tier, row]) => (
              <tr key={tier}>
                <td>{tier}</td>
                <td>{row.issues}</td>
                <td>{row.acu.toFixed(2)}</td>
                <td>{pct(row.acu_share)}</td>
                <td>{pct(row.merge_rate)}</td>
                <td>{row.ci_rounds.toFixed(1)}</td>
              </tr>
            ))}
          </tbody>
        </HTMLTable>
      </Card>

      <Card compact>
        <div className="metric__label" style={{ marginBottom: 8 }}>
          Escalations
        </div>
        {Object.keys(escalations).length === 0 ? (
          <span className="bp5-text-muted">None yet.</span>
        ) : (
          Object.entries(escalations).map(([reason, count]) => (
            <Tag key={reason} minimal round style={{ marginRight: 6 }}>
              {reason} · {count}
            </Tag>
          ))
        )}
      </Card>
    </div>
  );
}
