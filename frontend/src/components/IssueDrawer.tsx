import {
  AnchorButton,
  Button,
  ButtonGroup,
  Callout,
  Drawer,
  DrawerSize,
  HTMLTable,
  Tag,
} from "@blueprintjs/core";
import { useEffect, useState } from "react";

import { api } from "../api";
import type { IssueCard, State } from "../types";

const HUMAN_STATES: State[] = ["human-review", "done", "devin-blocked", "needs-triage"];

/** The board's Backlog column: nothing has judged this issue yet. */
function inBacklog(card: IssueCard): boolean {
  return card.state === null || card.state === "needs-triage";
}

export function IssueDrawer({
  number,
  onClose,
  onChanged,
}: {
  number: number | null;
  onClose: () => void;
  onChanged: (card: IssueCard) => void;
}) {
  const [card, setCard] = useState<IssueCard | null>(null);
  const [busy, setBusy] = useState(false);
  const [triaging, setTriaging] = useState(false);
  const [queued, setQueued] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (number === null) {
      setCard(null);
      return;
    }
    setQueued(false);
    api
      .issue(number)
      .then(setCard)
      .catch((err: Error) => setError(err.message));
  }, [number]);

  async function triage() {
    if (card === null) return;
    setTriaging(true);
    try {
      // The scout labels the issue; the verdict arrives by webhook, so the
      // card is not updated optimistically here — that would be a guess.
      await api.triageIssue(card.number);
      setQueued(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setTriaging(false);
    }
  }

  async function move(state: State) {
    if (card === null) return;
    setBusy(true);
    try {
      // Straight through to GitHub, then optimistic locally; the webhook confirms.
      const updated = await api.setState(card.number, state);
      setCard(updated);
      onChanged(updated);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const sessionUrl = card?.session_url ?? card?.session?.url;

  return (
    <Drawer
      isOpen={number !== null}
      onClose={onClose}
      size={DrawerSize.STANDARD}
      title={card ? `#${card.number} ${card.title}` : `#${number}`}
    >
      {error && <Callout intent="danger">{error}</Callout>}
      {card && (
        <div style={{ overflow: "auto" }}>
          <div className="drawer__section">
            <ButtonGroup>
              <AnchorButton icon="git-repo" href={card.html_url} target="_blank" text="Issue" />
              <AnchorButton
                icon="git-pull"
                href={card.meta.pr_url ?? "#"}
                target="_blank"
                disabled={!card.meta.pr_url}
                text="Pull request"
              />
              {/* The handoff: a session in waiting_for_user, picked up mid-flight
                  by a human with the full context already loaded. */}
              <AnchorButton
                icon="desktop"
                href={sessionUrl ?? "#"}
                target="_blank"
                disabled={!sessionUrl}
                text="Devin session"
              />
            </ButtonGroup>
          </div>

          {card.progress_phase && (
            <div className="drawer__section">
              <div className="metric__label">Worker progress</div>
              <p className="mono">
                {card.progress_phase}
                {card.progress_message ? ` — ${card.progress_message}` : ""}
              </p>
            </div>
          )}

          {inBacklog(card) && (
            <div className="drawer__section">
              <Button
                icon="predictive-analysis"
                intent="primary"
                text={queued ? "Scout dispatched" : "Triage issue"}
                loading={triaging}
                disabled={queued}
                onClick={() => void triage()}
              />
              <p className="bp5-text-muted">
                {queued
                  ? "A scout is reading it; the verdict lands as a label."
                  : "Sends this one issue to a scout, without triaging the backlog."}
              </p>
            </div>
          )}

          <div className="drawer__section">
            <div className="metric__label">Scout verdict</div>
            {card.meta.scout_reasoning ? (
              <>
                <p className="mono">{card.meta.scout_reasoning}</p>
                {card.meta.suggested_approach && (
                  <p className="mono">Approach: {card.meta.suggested_approach}</p>
                )}
                <Tag minimal>{card.meta.tier ?? "untierd"}</Tag>
              </>
            ) : (
              <p className="bp5-text-muted">Not triaged yet.</p>
            )}
          </div>

          <div className="drawer__section">
            <div className="metric__label">Session</div>
            {card.session ? (
              <HTMLTable compact striped style={{ width: "100%" }}>
                <tbody>
                  <tr>
                    <td>status</td>
                    <td>
                      {card.session.status_enum ?? card.session.status}
                      {card.session.origin === "automation" && <Tag minimal>automation</Tag>}
                    </td>
                  </tr>
                  <tr>
                    <td>ACU burn</td>
                    <td>{card.session.acus_consumed.toFixed(2)}</td>
                  </tr>
                  <tr>
                    <td>attempt / CI rounds</td>
                    <td>
                      {card.meta.attempt} / {card.meta.ci_rounds}
                    </td>
                  </tr>
                  <tr>
                    <td>human turns</td>
                    <td>{card.meta.human_turns}</td>
                  </tr>
                </tbody>
              </HTMLTable>
            ) : (
              <p className="bp5-text-muted">No session dispatched.</p>
            )}
          </div>

          <div className="drawer__section">
            <div className="metric__label">CI</div>
            {card.checks.length === 0 ? (
              <p className="bp5-text-muted">No checks reported.</p>
            ) : (
              <HTMLTable compact striped style={{ width: "100%" }}>
                <tbody>
                  {card.checks.map((check) => (
                    <tr key={check.name}>
                      <td>{check.name}</td>
                      <td>
                        <Tag
                          minimal
                          intent={
                            check.conclusion === "success"
                              ? "success"
                              : check.conclusion === null
                                ? "none"
                                : "danger"
                          }
                        >
                          {check.conclusion ?? check.status}
                        </Tag>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </HTMLTable>
            )}
          </div>

          <div className="drawer__section">
            <div className="metric__label">Move</div>
            <ButtonGroup>
              {HUMAN_STATES.map((state) => (
                <Button
                  key={state}
                  text={state}
                  disabled={busy || card.state === state}
                  onClick={() => void move(state)}
                />
              ))}
            </ButtonGroup>
          </div>
        </div>
      )}
    </Drawer>
  );
}
