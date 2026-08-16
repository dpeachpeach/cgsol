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
import type { IssueCard, Metrics, Snapshot } from "./types";

type View = "board" | "metrics";

export function App() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [view, setView] = useState<View>("board");
  const [selected, setSelected] = useState<number | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [triageOpen, setTriageOpen] = useState(false);
  const [connected, setConnected] = useState(false);
  const inflight = useRef(false);

  const refresh = useCallback(async () => {
    if (inflight.current) return;
    inflight.current = true;
    try {
      const [state, computed] = await Promise.all([api.state(), api.metrics()]);
      setSnapshot(state);
      setMetrics(computed);
    } finally {
      inflight.current = false;
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
          <Navbar.Divider />
          <Button
            minimal
            icon="refresh"
            onClick={() => void refresh()}
            title="Refresh now"
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
          <MetricsPanel metrics={metrics} />
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
