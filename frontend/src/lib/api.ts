import type { AgentEvent, Dashboard, Health, Sample, Session } from "@/lib/types";

async function readError(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail)) return body.detail.map((item: { msg?: string }) => item.msg).join(" ");
  } catch {
    /* plain text */
  }
  return `Request failed (${res.status})`;
}

export async function getHealth(): Promise<Health> {
  const res = await fetch("/backend/health", { cache: "no-store" });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

export async function getSamples(): Promise<Sample[]> {
  const res = await fetch("/backend/samples", { cache: "no-store" });
  if (!res.ok) throw new Error(await readError(res));
  const body = await res.json();
  return body.samples;
}

export async function createSampleSession(sampleId: string): Promise<Session> {
  const res = await fetch("/backend/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source: "sample", sampleId }),
  });
  if (!res.ok) throw new Error(await readError(res));
  const body = await res.json();
  return body.session;
}

export async function createConnectionSession(connectionString: string, name: string): Promise<Session> {
  const res = await fetch("/backend/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source: "connection", connectionString, name }),
  });
  if (!res.ok) throw new Error(await readError(res));
  const body = await res.json();
  return body.session;
}

export async function uploadSession(file: File): Promise<Session> {
  const data = new FormData();
  data.set("file", file);
  const res = await fetch("/backend/sessions/upload", { method: "POST", body: data });
  if (!res.ok) throw new Error(await readError(res));
  const body = await res.json();
  return body.session;
}

export async function listDashboards(): Promise<Dashboard[]> {
  const res = await fetch("/backend/dashboards", { cache: "no-store" });
  if (!res.ok) throw new Error(await readError(res));
  const body = await res.json();
  return body.dashboards;
}

export async function getDashboard(id: string): Promise<Dashboard> {
  const res = await fetch(`/backend/dashboards/${id}`, { cache: "no-store" });
  if (!res.ok) throw new Error(await readError(res));
  const body = await res.json();
  return body.dashboard;
}

export async function refreshDashboard(id: string, resync: boolean): Promise<Dashboard> {
  const res = await fetch(`/backend/dashboards/${id}/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resync }),
  });
  if (!res.ok) throw new Error(await readError(res));
  const body = await res.json();
  return body.dashboard;
}

export async function executeSql(sessionId: string, sql: string, confirmToken: string): Promise<void> {
  const res = await fetch(`/backend/sessions/${sessionId}/execute`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sql, confirmToken }),
  });
  if (!res.ok) throw new Error(await readError(res));
}

export async function streamPost(path: string, body: unknown, onEvent: (event: AgentEvent) => void): Promise<void> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) throw new Error(await readError(res));
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const line = chunk.split("\n").find((item) => item.startsWith("data:"));
      if (!line) continue;
      onEvent(JSON.parse(line.slice(5).trim()) as AgentEvent);
    }
  }
}
