export type QualityTier = "STANDARD" | "HUNT";

export type EvidenceBlock = {
  type: string;
  label: string;
  detail?: string;
};

export type LifecycleEvent = {
  cci_event_id: string;
  event_type: string;
  state: string;
  occurred_at: string;
  outcome_code?: string;
};

export type Mission = {
  cci_setup_id: string;
  symbol: string;
  timeframe: string;
  direction: "LONG" | "SHORT" | string;
  quality_tier: QualityTier | string;
  title: string;
  thesis_summary: string;
  evidence: EvidenceBlock[];
  lifecycle: LifecycleEvent[];
  lifecycle_state: string;
  outcome_code: string | null;
  opened_at: string;
  resolved_at: string | null;
  resolved: boolean;
  synthetic: boolean;
  disclaimer: string;
};
