import { Card, HTMLTable, NonIdealState, Tag } from "@blueprintjs/core";

import type { Metrics } from "../types";

function pct(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

function num(value: number | null, digits = 2): string {
  return value === null ? "—" : value.toFixed(digits);
}

function Metric({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <Card className="metric" compact>
      <div className="metric__label">{label}</div>
      <div className="metric__value">{value}</div>
      {sub && <div className="bp5-text-muted bp5-text-small">{sub}</div>}
    </Card>
  );
}

export function MetricsPanel({ metrics }: { metrics: Metrics | null }) {
  if (metrics === null) {
    return <NonIdealState icon="chart" title="No metrics yet" />;
  }
  const { headline, funnel, by_tier: byTier, escalations } = metrics;
  const widest = Math.max(...Object.values(funnel), 1);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
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
            <div className="funnel__bar" style={{ width: `${(count / widest) * 60}%` }} />
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
