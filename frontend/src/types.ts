export type State =
  | "needs-triage"
  | "devin-eligible"
  | "devin-working"
  | "devin-pr-open"
  | "ci-failing"
  | "devin-fixing"
  | "human-review"
  | "devin-declined"
  | "can-close-issue"
  | "devin-blocked"
  | "done";

export interface CheckRun {
  name: string;
  status: string;
  conclusion: string | null;
  details_url: string | null;
  required: boolean;
}

export interface IssueMeta {
  session_id: string | null;
  tier: string | null;
  attempt: number;
  ci_rounds: number;
  human_turns: number;
  confidence: number | null;
  pr_url: string | null;
  branch: string | null;
  escalation: string | null;
  scout_reasoning: string | null;
  suggested_approach: string | null;
  acus: number;
  pr_opened_at: string | null;
}

export interface SessionInfo {
  session_id: string;
  status: string;
  status_enum: string | null;
  tags: string[];
  title: string | null;
  url: string;
  pr_url: string | null;
  acus_consumed: number;
  origin: string | null;
  updated_at: string | null;
}

export interface IssueCard {
  number: number;
  title: string;
  html_url: string;
  created_at: string;
  state: State | null;
  tier: string | null;
  labels: string[];
  meta: IssueMeta;
  session: SessionInfo | null;
  checks: CheckRun[];
  progress_phase: string | null;
  progress_message: string | null;
  progress_at: string | null;
  progress_comment_id: number | null;
  pr_number: number | null;
  pr_merged: boolean;
  ready_to_merge: boolean;
  /** Derived from the state and the live session, never a GitHub label. */
  pickup_status: "awaiting-devin" | null;
  last_synced: number;
  session_url?: string;
}

/** What GitHub last said is left of the hourly REST budget. */
export interface RateBudget {
  remaining: number | null;
  limit: number | null;
  reserve: number;
  resets_at: number | null;
}

export interface Snapshot {
  cards: IssueCard[];
  counts: Record<string, number>;
  tiers: Record<string, number>;
  active_sessions: number;
  budget: RateBudget | null;
  synced_at: number;
}

export interface Metrics {
  headline: {
    acu_per_ready_pr: number | null;
    ready_pr_count: number;
    total_acu: number;
    build_acu: number;
    merged: number;
    retired: number;
    issue_to_pr_seconds: number | null;
    issue_to_pr_count: number;
    open_age_seconds: number | null;
    open_count: number;
  };
  funnel: Record<string, number>;
  by_tier: Record<
    string,
    {
      issues: number;
      acu: number;
      acu_share: number | null;
      merged: number;
      merge_rate: number | null;
      ci_rounds: number;
    }
  >;
  escalations: Record<string, number>;
  sessions: { active: number; by_role: Record<string, number> };
  series: {
    ts: number;
    acu_per_ready_pr: number | null;
    open_sessions: number;
    total_acu: number;
  }[];
}

export interface TriageEstimate {
  issue_count: number;
  estimated_acu: number;
  issues: number[];
}

export type TriageMode = "auto" | "chunked" | "manual";

export interface ConfigPayload {
  path: string;
  repo: string;
  remote: Record<string, unknown> | null;
  next_chunk_at: number | null;
  effective: Record<string, number | string>;
}
