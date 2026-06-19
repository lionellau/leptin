/**
 * @leptin/client — a thin TypeScript SDK over the Leptin local HTTP API.
 *
 * Start the server with `leptin dashboard --port 8765`, then:
 *
 *   import { LeptinClient } from "@leptin/client";
 *   const mem = new LeptinClient("http://127.0.0.1:8765");
 *   const report = await mem.report("all");
 *   console.log(`Saved ${report.tokens_saved} tokens ($${report.usd_saved}).`);
 */

export type MemoryStatus = "active" | "superseded" | "quarantined" | "deleted";

export interface MemoryView {
  memory_id: string;
  subject: string | null;
  content: string;
  tokens: number;
  strength: number;
  status: MemoryStatus;
  access_count: number;
  provenance?: string | null;
  superseded_by?: string | null;
}

export interface DietReport {
  window: string;
  tokens_saved: number;
  usd_saved: number;
  footprint_tokens_reduced: number;
  model: string;
  ops: Record<string, number>;
  active_memories: number;
  guardrail_status: Record<string, unknown> | null;
  top_savers: Array<Record<string, unknown>>;
  note: string | null;
  tuning: Record<string, unknown> | null;
}

export interface LedgerRow {
  ts: number;
  operation: string;
  baseline_tokens: number;
  actual_tokens: number;
  tokens_saved: number;
  usd_saved: number;
  model: string;
  session_id: string | null;
}

export interface CompactResult {
  decayed: number;
  merged: number;
  superseded: number;
  projected_tokens_saved: number;
  tokens_saved: number;
  dry_run: boolean;
  guardrail: {
    recall_before: number;
    recall_after: number;
    passed: boolean;
    rolled_back: boolean;
    max_drop: number;
  };
  diff: Array<{ memory_id: string; action: string }>;
}

export class LeptinClient {
  constructor(
    private readonly baseUrl: string = "http://127.0.0.1:8765",
    private readonly fetchImpl: typeof fetch = fetch,
  ) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  private async get<T>(path: string): Promise<T> {
    const res = await this.fetchImpl(`${this.baseUrl}${path}`);
    if (!res.ok) throw new Error(`Leptin GET ${path} failed: ${res.status}`);
    return (await res.json()) as T;
  }

  private async post<T>(path: string, body: unknown): Promise<T> {
    const res = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`Leptin POST ${path} failed: ${res.status}`);
    return (await res.json()) as T;
  }

  /** The savings report for a window: "session" | "7d" | "all". */
  report(window: "session" | "7d" | "all" = "all"): Promise<DietReport> {
    return this.get<DietReport>(`/api/report?window=${window}`);
  }

  /** Browse memories (glass box). */
  async memories(status: MemoryStatus | "all" = "all"): Promise<MemoryView[]> {
    const { memories } = await this.get<{ memories: MemoryView[] }>(
      `/api/memories?status=${status}`,
    );
    return memories;
  }

  /** Raw ledger rows (savings over time). */
  async ledger(): Promise<LedgerRow[]> {
    const { ledger } = await this.get<{ ledger: LedgerRow[] }>(`/api/ledger`);
    return ledger;
  }

  /** Run (or preview) guardrailed compaction. */
  compact(dryRun = false): Promise<CompactResult> {
    return this.post<CompactResult>(`/api/compact`, { dry_run: dryRun });
  }

  /** Restore a forgotten/quarantined memory. */
  restore(memoryId: string): Promise<{ restored: boolean }> {
    return this.post(`/api/restore`, { memory_id: memoryId });
  }

  /** Forget a memory by id (soft delete → quarantine). */
  forget(memoryId: string): Promise<ForgetResult> {
    return this.post(`/api/forget`, { memory_id: memoryId });
  }

  /** Self-tune the memory policy (offline, guardrailed). */
  tune(dryRun = false): Promise<TuneResult> {
    return this.post(`/api/tune`, { dry_run: dryRun });
  }

  /** Roll back the last tune, or a specific version. */
  rollbackTune(version?: number): Promise<{ rolled_back: boolean; version?: number }> {
    return this.post(`/api/rollback`, version == null ? {} : { version });
  }

  /** The self-tuning report + evolution ledger. */
  tuning(): Promise<{ tuning: TuningReport | null; history: ConfigVersion[] }> {
    return this.get(`/api/tuning`);
  }
}

export interface ForgetResult {
  forgotten: MemoryView[];
  reversible_until: number;
  count: number;
  tokens_saved: number;
}

export interface TuneResult {
  trigger: string;
  dry_run: boolean;
  accepted: boolean;
  changes: Array<{ knob: string; old: number; new: number; direction: string }>;
  objective_before: number;
  objective_after: number;
  recall_before: number;
  recall_after: number;
  reduction_before: number;
  reduction_after: number;
  llm_calls: number;
  tune_tokens: number;
}

export interface TuningReport {
  enabled: boolean;
  last_tune_at: number | null;
  cycles: number;
  accepted: number;
  rejected: number;
  last_delta: number | null;
  current_version: number | null;
  frozen_until: number | null;
  llm_calls: number;
  tune_tokens: number;
}

export interface ConfigVersion {
  id: number;
  ts: number;
  knob: string | null;
  old_value: unknown;
  new_value: unknown;
  direction: string;
  accepted: number;
  reason: string;
  parent_id: number | null;
}

export default LeptinClient;
