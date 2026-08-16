import {
  Alignment,
  Button,
  Navbar,
  NonIdealState,
  Spinner,
  Tab,
  Tabs,
  Tag,
} from "@blueprintjs/core";
import { useCallback, useEffect, useRef, useState } from "react";

import { api, subscribe } from "./api";
import { Board } from "./components/Board";
import { IssueDrawer } from "./components/IssueDrawer";
import { MetricsPanel } from "./components/MetricsPanel";
import { SettingsDialog } from "./components/SettingsDialog";
import { TriageDialog } from "./components/TriageDialog";
import type { IssueCard, Metrics, RateBudget, Snapshot } from "./types";

type View = "board" | "metrics";

/** Metrics are derived from the projection, not polled from GitHub, so a
 *  refresh costs nothing on the API budget. A minute is slow enough to read a
 *  number before it changes and fast enough that a demo never shows a stale one. */
const METRICS_INTERVAL_MS = 60_000;

/** The hourly REST budget. An hour about to run out should be visible before
 * the board stops moving, not afterwards. */
function BudgetTag({ budget }: { budget: RateBudget }) {
  if (budget.remaining === null) return null;
  const thin = budget.remaining <= budget.reserve * 5;
  const resets = budget.resets_at
    ? new Date(budget.resets_at * 1000).toLocaleTimeString()
    : null;
  return (
    <Tag
      minimal
      round
      intent={thin ? "warning" : "none"}
      title={resets ? `GitHub REST budget, resets ${resets}` : undefined}
    >
      {budget.remaining.toLocaleString()}
      {budget.limit === null ? "" : `/${budget.limit.toLocaleString()}`} API
    </Tag>
  );
}

export function App() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [view, setView] = useState<View>("board");
  const [selected, setSelected] = useState<number | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [triageOpen, setTriageOpen] = useState(false);
  const [connected, setConnected] = useState(false);
  const [metricsAt, setMetricsAt] = useState<number | null>(null);
  const inflight = useRef(false);
  const [sweeping, setSweeping] = useState(false);

  /** `sweep` re-reads GitHub first. Events already carry fresh state, so they
   *  read the projection; the button does not, because a person presses it
   *  precisely when they believe the projection is behind. */
  const refresh = useCallback(async (sweep = false) => {
    if (inflight.current) return;
    inflight.current = true;
    if (sweep) setSweeping(true);
    try {
      const [state, computed] = await Promise.all([
        sweep ? api.reconcile() : api.state(),
        api.metrics(),
      ]);
      setSnapshot(state);
      setMetrics(computed);
      setMetricsAt(Date.now());
    } finally {
      inflight.current = false;
      setSweeping(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    // SSE for latency; the manual refresh and the orchestrator's own reconciler
    // are what make it correct. Staleness is bounded by one poll interval.
    const close = subscribe((event) => {
      setConnected(true);
      if (event.event === "snapshot" || event.event === "reconciled") {
        setSnapshot(event.data as Snapshot);
        return;
      }
      void refresh();
    });
    return close;
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void api.metrics().then((computed) => {
        setMetrics(computed);
        setMetricsAt(Date.now());
      });
    }, METRICS_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, []);

  const onCardChanged = useCallback((card: IssueCard) => {
    setSnapshot((current) =>
      current === null
        ? current
        : {
            ...current,
            cards: current.cards.map((c) =>
              c.number === card.number ? card : c,
            ),
          },
    );
  }, []);

  const syncedAt = snapshot
    ? new Date(snapshot.synced_at * 1000).toLocaleTimeString()
    : "—";

  return (
    <div className="app">
      <Navbar>
        <Navbar.Group align={Alignment.LEFT}>
          <Navbar.Heading className="brand">
            <strong>Superset Control Panel</strong>
          </Navbar.Heading>
          <Navbar.Divider />
          <Tabs
            id="views"
            selectedTabId={view}
            onChange={(id) => setView(id as View)}
            animate={false}
          >
            <Tab id="board" title="Board" />
            <Tab id="metrics" title="Metrics" />
          </Tabs>
        </Navbar.Group>
        <Navbar.Group align={Alignment.RIGHT}>
          <Tag minimal round intent={connected ? "success" : "warning"}>
            {connected ? "live" : "connecting"}
          </Tag>
          <Tag minimal round>
            {snapshot?.active_sessions ?? 0} active sessions
          </Tag>
          <Tag minimal round>
            synced {syncedAt}
          </Tag>
          {snapshot?.budget ? <BudgetTag budget={snapshot.budget} /> : null}
          <Navbar.Divider />
          <Button
            minimal
            icon="refresh"
            loading={sweeping}
            onClick={() => void refresh(true)}
            title="Sync with GitHub now"
          />
          <Button
            minimal
            icon="predictive-analysis"
            text="Triage backlog"
            onClick={() => setTriageOpen(true)}
          />
          <Button minimal icon="cog" onClick={() => setSettingsOpen(true)} />
        </Navbar.Group>
      </Navbar>

      <div className="app__body">
        {snapshot === null ? (
          <NonIdealState icon={<Spinner />} title="Waiting for first sync" />
        ) : view === "board" ? (
          <Board cards={snapshot.cards} onSelect={setSelected} />
        ) : (
          <MetricsPanel metrics={metrics} computedAt={metricsAt} />
        )}
      </div>

      <IssueDrawer
        number={selected}
        onClose={() => setSelected(null)}
        onChanged={onCardChanged}
      />
      <SettingsDialog
        isOpen={settingsOpen}
        onClose={() => setSettingsOpen(false)}
      />
      <TriageDialog isOpen={triageOpen} onClose={() => setTriageOpen(false)} />
    </div>
  );
}
