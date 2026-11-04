export type Sample = {
  id: string;
  name: string;
  blurb: string;
  question: string;
  currency: string;
};

export type Health = {
  ok: boolean;
  sandbox: string;
  provider: string;
  model: string | null;
  baseUrl: string | null;
  inferenceConfigured: boolean;
  ledgerClock: string;
  maxRows: number;
  queryTimeoutS: number;
};

export type Session = {
  id: string;
  title: string;
  sourceKind: string;
  sampleId: string | null;
  dialect: string;
  createdAt: string;
  hasConnection: boolean;
};

export type Step = {
  id: string;
  kind: string;
  title: string;
  status: "running" | "ok" | "error" | "warning" | "fixed" | "blocked";
  detail?: string;
  sql?: string;
  code?: string;
  columns?: string[];
  rows?: Record<string, unknown>[];
  warnings?: string[];
  error?: string;
  elapsedMs?: number;
  stdout?: string;
};

export type ChartSpec = {
  type: "bar" | "line" | "area";
  title: string;
  xKey: string;
  yKey: string;
  data: Record<string, unknown>[];
};

export type Widget = {
  id?: string;
  title: string;
  kind: "chart" | "table" | "metric";
  sql: string;
  spec?: {
    type?: "bar" | "line" | "area";
    title?: string;
    xKey?: string;
    yKey?: string;
    prefix?: string;
  };
  preview?: {
    columns?: string[];
    rows?: Record<string, unknown>[];
    value?: unknown;
    error?: string | null;
    elapsedMs?: number;
  };
};

export type Dashboard = {
  id?: string;
  sessionId?: string;
  title: string;
  createdAt?: string;
  widgets?: Widget[];
};

export type QueryNote = {
  sql: string;
  status: string;
  purpose: string;
};

export type AgentResult = {
  answer: string;
  chart?: ChartSpec | null;
  table?: { title: string; columns: string[]; rows: Record<string, unknown>[] } | null;
  dashboard?: Dashboard | null;
  queries: QueryNote[];
};

export type Confirmation = {
  sql: string;
  token: string;
  reason: string;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  steps?: Step[];
  result?: AgentResult | null;
  confirmation?: Confirmation | null;
};

export type DrillCheck = {
  id: string;
  title: string;
  passed: boolean;
  detail: string;
};

export type AgentEvent =
  | { type: "step"; step: Step }
  | { type: "result"; result: AgentResult }
  | { type: "confirmation"; sql: string; token: string; reason: string }
  | { type: "drill"; check: DrillCheck }
  | { type: "error"; error: string }
  | { type: "done" };
