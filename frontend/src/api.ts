import type { ConfigPayload, IssueCard, Metrics, Snapshot, State, TriageEstimate } from "./types";

async function json<T>(input: string, init?: RequestInit): Promise<T> {
  const response = await fetch(input, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    throw new Error(`${init?.method ?? "GET"} ${input} → ${response.status}`);
  }
  return (await response.json()) as T;
}

export const api = {
  state: () => json<Snapshot>("/api/state"),
  issue: (number: number) => json<IssueCard>(`/api/issues/${number}`),
  metrics: () => json<Metrics>("/api/metrics"),
  config: () => json<ConfigPayload>("/api/config"),
  putConfig: (body: Record<string, unknown>) =>
    json<{ written: string }>("/api/config", { method: "PUT", body: JSON.stringify(body) }),
  setState: (number: number, state: State) =>
    json<IssueCard>(`/api/issues/${number}/state`, {
      method: "POST",
      body: JSON.stringify({ state }),
    }),
  // Same handler the webhook path uses — the button is not a second code path.
  estimateTriage: () => json<TriageEstimate>("/api/triage?estimate=true", { method: "POST" }),
  triage: () => json<{ queued: number[] }>("/api/triage", { method: "POST" }),
};

export type StreamEvent = { event: string; data: unknown };

/** SSE from the orchestrator. Reconnects on drop; EventSource does that for us. */
export function subscribe(onEvent: (event: StreamEvent) => void): () => void {
  const source = new EventSource("/api/events");
  const forward = (name: string) => (event: MessageEvent<string>) => {
    onEvent({ event: name, data: JSON.parse(event.data) as unknown });
  };
  const names = [
    "snapshot",
    "tick",
    "reconciled",
    "issue.state",
    "issue.updated",
    "issue.escalated",
    "scout.dispatched",
    "scout.finished",
    "worker.dispatched",
    "worker.progress",
    "session.adopted",
    "config.reloaded",
  ];
  for (const name of names) source.addEventListener(name, forward(name) as EventListener);
  return () => source.close();
}
