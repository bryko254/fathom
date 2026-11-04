"use client";

import { newId } from "@/lib/utils";
import { useCallback, useEffect, useMemo, useState, type Dispatch, type FormEvent, type SetStateAction } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ChartView, DataTable } from "@/components/chart-view";
import { DashboardView } from "@/components/dashboard-view";
import { Trace } from "@/components/trace";
import {
  createConnectionSession,
  createSampleSession,
  executeSql,
  getDashboard,
  getHealth,
  getSamples,
  listDashboards,
  refreshDashboard,
  streamPost,
  uploadSession,
} from "@/lib/api";
import type {
  AgentEvent,
  ChatMessage,
  Confirmation,
  Dashboard,
  DrillCheck,
  Health,
  Sample,
  Session,
  Step,
} from "@/lib/types";

type Pane = "sources" | "work" | "answer";

export function Workspace() {
  const [health, setHealth] = useState<Health | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [samples, setSamples] = useState<Sample[]>([]);
  const [session, setSession] = useState<Session | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [dashboards, setDashboards] = useState<Dashboard[]>([]);
  const [activeDashboard, setActiveDashboard] = useState<Dashboard | null>(null);
  const [question, setQuestion] = useState("");
  const [running, setRunning] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pane, setPane] = useState<Pane>("work");
  const [connection, setConnection] = useState("");
  const [drillOpen, setDrillOpen] = useState(false);
  const [drillChecks, setDrillChecks] = useState<DrillCheck[]>([]);
  const [drilling, setDrilling] = useState(false);
  const [uploading, setUploading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getHealth(), getSamples(), listDashboards()])
      .then(([nextHealth, nextSamples, nextBoards]) => {
        if (cancelled) return;
        setHealth(nextHealth);
        setSamples(nextSamples);
        setDashboards(nextBoards);
      })
      .catch((err: Error) => {
        if (!cancelled) setBootError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const latestResult = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].result) return messages[index].result;
    }
    return null;
  }, [messages]);

  const suggested = samples.find((sample) => sample.id === session?.sampleId)?.question;

  const ask = useCallback(
    async (text: string, sessionId: string) => {
      const trimmed = text.trim();
      if (!trimmed || running) return;
      const assistantId = newId();
      setMessages((current) => [
        ...current,
        { id: newId(), role: "user", content: trimmed },
        { id: assistantId, role: "assistant", content: "", steps: [], result: null },
      ]);
      setQuestion("");
      setRunning(true);
      setError(null);
      setActiveDashboard(null);
      setPane("work");
      let finished = false;
      try {
        await streamPost(`/backend/sessions/${sessionId}/ask`, { question: trimmed }, (event) => {
          if (event.type === "result" || event.type === "error") finished = true;
          applyEvent(assistantId, event, setMessages, setDashboards);
          if (event.type === "result" && window.matchMedia("(max-width: 1023px)").matches) {
            setPane("answer");
          }
        });
        if (!finished) {
          setMessages((current) =>
            current.map((message) =>
              message.id === assistantId && !message.result
                ? { ...message, content: "The analysis stopped unexpectedly." }
                : message,
            ),
          );
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "The question failed.");
        if (!finished) {
          setMessages((current) =>
            current.map((message) =>
              message.id === assistantId && !message.result
                ? { ...message, content: "The analysis stopped unexpectedly." }
                : message,
            ),
          );
        }
      } finally {
        setRunning(false);
      }
    },
    [running],
  );

  async function openSample(sample: Sample, andAsk: boolean) {
    setError(null);
    try {
      const next = await createSampleSession(sample.id);
      setSession(next);
      setMessages([]);
      setActiveDashboard(null);
      setQuestion(sample.question);
      if (andAsk) await ask(sample.question, next.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not open that ledger.");
    }
  }

  async function onUpload(file: File | undefined) {
    if (!file) return;
    if (file.size > 25 * 1024 * 1024) {
      setError("That file is over 25 MB. Upload a .sql, .sqlite, or .db file up to 25 MB.");
      return;
    }
    setError(null);
    setUploading(true);
    try {
      const next = await uploadSession(file);
      setSession(next);
      setMessages([]);
      setActiveDashboard(null);
      setQuestion("");
      setPane("work");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed.");
    } finally {
      setUploading(false);
    }
  }

  async function onConnect(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const next = await createConnectionSession(connection, "Connected database");
      setSession(next);
      setMessages([]);
      setConnection("");
      setActiveDashboard(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not snapshot that database.");
    }
  }

  async function openDashboard(id: string) {
    setError(null);
    try {
      const dashboard = await getDashboard(id);
      setActiveDashboard(dashboard);
      setPane("work");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not open that dashboard.");
    }
  }

  async function onRefresh(resync: boolean) {
    if (!activeDashboard?.id) return;
    setRefreshing(true);
    setError(null);
    try {
      const dashboard = await refreshDashboard(activeDashboard.id, resync);
      setActiveDashboard(dashboard);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Refresh failed.");
    } finally {
      setRefreshing(false);
    }
  }

  async function onConfirm(confirmation: Confirmation) {
    if (!session) return;
    setError(null);
    try {
      await executeSql(session.id, confirmation.sql, confirmation.token);
      setMessages((current) => [
        ...current,
        {
          id: newId(),
          role: "assistant",
          content: "Confirmed. The sandbox ran that statement once. DDL is still blocked.",
        },
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "The confirmed statement failed.");
    }
  }

  async function startDrill() {
    setDrillOpen(true);
    setDrillChecks([]);
    setDrilling(true);
    try {
      await streamPost("/backend/containment", {}, (event) => {
        if (event.type === "drill") {
          setDrillChecks((current) => [...current, event.check]);
        }
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "The drill failed to start.");
    } finally {
      setDrilling(false);
    }
  }

  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3 lg:px-6">
        <div className="flex items-center gap-3">
          <span className="grid size-9 place-items-center rounded-lg bg-primary text-primary-foreground font-heading text-lg">
            F
          </span>
          <div>
            <p className="font-heading text-xl leading-none">Fathom</p>
            <p className="mt-1 text-xs text-muted-foreground">Database copilot · ledger clock 4 Nov 2026</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <StatusPill
            label={
              health
                ? health.inferenceConfigured
                  ? `Vultr Serverless Inference · ${health.model}`
                  : `Vultr Serverless Inference · ${health.model} · no key, fallback analyst`
                : "Vultr Serverless Inference"
            }
          />
          <StatusPill label={health ? `Sandbox · ${health.sandbox}` : "Sandbox"} />
          <Button type="button" variant="outline" size="sm" onClick={startDrill}>
            Containment drill
          </Button>
        </div>
      </header>

      {bootError ? (
        <p className="mx-4 mt-3 rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive lg:mx-6" role="alert">
          The control plane is not responding. Start the API and reload. {bootError}
        </p>
      ) : null}
      {error ? (
        <p className="mx-4 mt-3 rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive lg:mx-6" role="alert">
          {error}
        </p>
      ) : null}

      <div className="flex gap-1 px-4 pt-3 lg:hidden" role="tablist" aria-label="Workspace">
        {(
          [
            ["sources", "Sources"],
            ["work", "Work"],
            ["answer", "Answer"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={pane === id}
            className={`rounded-full px-3 py-1 text-sm ${pane === id ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground"}`}
            onClick={() => setPane(id)}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="grid min-h-0 flex-1 gap-4 px-4 py-4 lg:h-[calc(100vh-4.75rem)] lg:grid-cols-[280px_minmax(0,1fr)_360px] lg:px-6">
        <aside className={`${pane === "sources" ? "flex" : "hidden"} min-h-0 flex-col gap-4 overflow-auto lg:flex`}>
          <BringData
            idPrefix="side"
            compact
            connection={connection}
            uploading={uploading}
            onConnection={setConnection}
            onConnect={onConnect}
            onUpload={onUpload}
          />
          <section>
            <h2 className="text-[11px] font-medium tracking-[0.16em] text-muted-foreground uppercase">Demo datasets</h2>
            <div className="mt-2 space-y-2">
              {samples.map((sample) => (
                <button
                  key={sample.id}
                  type="button"
                  onClick={() => openSample(sample, false)}
                  className={`w-full rounded-xl bg-card p-3 text-left ring-1 ring-foreground/10 ${session?.sampleId === sample.id ? "ring-primary" : ""}`}
                >
                  <span className="block text-sm font-medium">{sample.name}</span>
                  <span className="mt-1 block text-xs text-muted-foreground">{sample.blurb}</span>
                </button>
              ))}
              {samples.length === 0 && !bootError ? <p className="text-sm text-muted-foreground">Loading ledgers…</p> : null}
            </div>
          </section>
          <section>
            <h2 className="text-[11px] font-medium tracking-[0.16em] text-muted-foreground uppercase">Dashboards</h2>
            <ul className="mt-2 space-y-1">
              {dashboards.length === 0 ? <li className="text-sm text-muted-foreground">None saved yet.</li> : null}
              {dashboards.map((dashboard) => (
                <li key={dashboard.id}>
                  <button
                    type="button"
                    className="w-full rounded-md px-2 py-1.5 text-left text-sm hover:bg-muted"
                    onClick={() => dashboard.id && openDashboard(dashboard.id)}
                  >
                    {dashboard.title}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        </aside>

        <main className={`${pane === "work" ? "flex" : "hidden"} min-h-0 flex-col lg:flex`}>
          {activeDashboard ? (
            <DashboardView
              dashboard={activeDashboard}
              busy={refreshing}
              onRefresh={onRefresh}
              onBack={() => setActiveDashboard(null)}
            />
          ) : (
            <Conversation
              session={session}
              messages={messages}
              question={question}
              suggested={suggested}
              running={running}
              uploading={uploading}
              connection={connection}
              onQuestion={setQuestion}
              onConnection={setConnection}
              onAsk={() => session && ask(question, session.id)}
              onConfirm={onConfirm}
              onConnect={onConnect}
              onUpload={onUpload}
            />
          )}
        </main>

        <aside className={`${pane === "answer" ? "block" : "hidden"} min-h-0 lg:block`}>
          <AnswerPanel result={latestResult ?? null} running={running} onOpenDashboard={openDashboard} />
        </aside>
      </div>

      <Dialog open={drillOpen} onOpenChange={setDrillOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Containment drill</DialogTitle>
            <DialogDescription>
              A fresh sandbox tries to drop a table, load an extension, loop forever, read a host file, write onto the
              host, and open a network connection. Docker mode should stop all six. Subprocess mode will not.
            </DialogDescription>
          </DialogHeader>
          <ul className="space-y-2">
            {drillChecks.length === 0 ? (
              <li className="text-sm text-muted-foreground">{drilling ? "Dispatching…" : "No checks yet."}</li>
            ) : null}
            {drillChecks.map((check) => (
              <li key={check.id} className="rounded-lg bg-muted/60 px-3 py-2">
                <p className="text-sm font-medium">
                  {check.passed ? "Contained" : "Not contained"} · {check.title}
                </p>
                <p className="text-xs text-muted-foreground">{check.detail}</p>
              </li>
            ))}
          </ul>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function Conversation({
  session,
  messages,
  question,
  suggested,
  running,
  uploading,
  connection,
  onQuestion,
  onConnection,
  onAsk,
  onConfirm,
  onConnect,
  onUpload,
}: {
  session: Session | null;
  messages: ChatMessage[];
  question: string;
  suggested?: string;
  running: boolean;
  uploading: boolean;
  connection: string;
  onQuestion: (value: string) => void;
  onConnection: (value: string) => void;
  onAsk: () => void;
  onConfirm: (confirmation: Confirmation) => void;
  onConnect: (event: FormEvent) => void;
  onUpload: (file: File | undefined) => void;
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 space-y-4 overflow-auto pb-4">
        {messages.length === 0 ? (
          session ? (
            <div className="mx-auto max-w-xl py-10">
              <p className="text-[11px] font-medium tracking-[0.16em] text-muted-foreground uppercase">Ledger ready</p>
              <h1 className="mt-2 font-heading text-4xl leading-tight">{session.title}</h1>
              <p className="mt-3 text-sm leading-6 text-muted-foreground">
                Ask a question below. Fathom reads the schema in the sandbox, shows the SQL, and corrects itself when a
                query fails.
              </p>
            </div>
          ) : (
            <div className="mx-auto flex max-w-3xl flex-col items-center py-6 text-center sm:py-10">
              <p className="text-[11px] font-medium tracking-[0.16em] text-muted-foreground uppercase">Your database</p>
              <h1 className="mt-2 font-heading text-4xl leading-tight sm:text-5xl">Bring the data. Ask in English.</h1>
              <p className="mt-3 max-w-xl text-sm leading-6 text-muted-foreground">
                Upload a dump or point at Postgres or MySQL. Fathom snapshots it into a read-only sandbox, then explores
                the schema itself.
              </p>
              <BringData
                idPrefix="hero"
                connection={connection}
                uploading={uploading}
                onConnection={onConnection}
                onConnect={onConnect}
                onUpload={onUpload}
              />
            </div>
          )
        ) : (
          messages.map((message) => (
            <article key={message.id}>
              {message.role === "user" ? (
                <p className="ml-auto max-w-xl rounded-2xl bg-primary px-4 py-2 text-sm text-primary-foreground">
                  {message.content}
                </p>
              ) : (
                <div>
                  {message.steps && message.steps.length > 0 ? <Trace steps={message.steps} /> : null}
                  {message.content && !message.result ? (
                    <p className="mt-3 text-sm text-destructive" role="alert">
                      {message.content}
                    </p>
                  ) : null}
                  {message.confirmation ? (
                    <div className="mt-3 rounded-xl bg-card p-3 ring-1 ring-foreground/10">
                      <p className="text-sm">{message.confirmation.reason}</p>
                      <pre className="mt-2 overflow-auto text-xs">{message.confirmation.sql}</pre>
                      <Button type="button" size="sm" className="mt-2" onClick={() => onConfirm(message.confirmation!)}>
                        Run this statement once
                      </Button>
                    </div>
                  ) : null}
                </div>
              )}
            </article>
          ))
        )}
      </div>
      <form
        className="sticky bottom-0 space-y-2 border-t border-border bg-background pt-3"
        onSubmit={(event) => {
          event.preventDefault();
          onAsk();
        }}
      >
        <Label htmlFor="question" className="sr-only">
          Ask a question
        </Label>
        <Textarea
          id="question"
          value={question}
          disabled={!session || running}
          placeholder={session ? "Ask about this ledger" : "Open a ledger first"}
          onChange={(event) => onQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              onAsk();
            }
          }}
        />
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap gap-2">
            {suggested ? (
              <Button type="button" variant="secondary" size="sm" disabled={running || !session} onClick={() => onQuestion(suggested)}>
                {suggested}
              </Button>
            ) : null}
          </div>
          <Button type="submit" disabled={!session || running || !question.trim()}>
            {running ? "Working" : "Ask"}
          </Button>
        </div>
      </form>
    </div>
  );
}

function AnswerPanel({
  result,
  running,
  onOpenDashboard,
}: {
  result: ChatMessage["result"];
  running: boolean;
  onOpenDashboard: (id: string) => void;
}) {
  if (!result) {
    return (
      <div className="rounded-xl bg-card p-4 ring-1 ring-foreground/10">
        <h2 className="font-heading text-xl">Answer</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          {running
            ? "The sandbox is still working. The trace on the left is the live record."
            : "Tables, the chart, and the SQL that passed the sanity check land here."}
        </p>
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <article className="rounded-xl bg-card p-4 ring-1 ring-foreground/10">
        <h2 className="font-heading text-xl">Answer</h2>
        <p className="mt-2 text-sm leading-6">{result.answer}</p>
      </article>
      {result.chart ? (
        <article className="rounded-xl bg-card p-4 ring-1 ring-foreground/10">
          <h3 className="text-sm font-medium">{result.chart.title}</h3>
          <ChartView chart={result.chart} />
        </article>
      ) : null}
      {result.table && result.table.rows.length > 0 ? (
        <article className="rounded-xl bg-card p-4 ring-1 ring-foreground/10">
          <h3 className="mb-2 text-sm font-medium">{result.table.title}</h3>
          <DataTable columns={result.table.columns} rows={result.table.rows} title={result.table.title} />
        </article>
      ) : null}
      {result.queries?.length ? (
        <article className="rounded-xl bg-card p-4 ring-1 ring-foreground/10">
          <h3 className="text-sm font-medium">Statements</h3>
          <ul className="mt-2 space-y-2">
            {result.queries.map((query, index) => (
              <li key={`${query.purpose}-${index}`}>
                <p className="text-xs text-muted-foreground">
                  {query.status} · {query.purpose}
                </p>
                <pre className="mt-1 max-h-28 overflow-auto text-[11px]">{query.sql}</pre>
              </li>
            ))}
          </ul>
        </article>
      ) : null}
      {result.dashboard?.id ? (
        <Button type="button" variant="secondary" onClick={() => onOpenDashboard(result.dashboard!.id!)}>
          Open dashboard
        </Button>
      ) : null}
    </div>
  );
}

function BringData({
  idPrefix,
  compact = false,
  connection,
  uploading,
  onConnection,
  onConnect,
  onUpload,
}: {
  idPrefix: string;
  compact?: boolean;
  connection: string;
  uploading: boolean;
  onConnection: (value: string) => void;
  onConnect: (event: FormEvent) => void;
  onUpload: (file: File | undefined) => void;
}) {
  const [over, setOver] = useState(false);
  return (
    <div className={compact ? "space-y-3" : "mt-6 grid w-full gap-3 text-left sm:grid-cols-2"}>
      <form
        className={
          compact
            ? "space-y-2"
            : `flex min-h-44 flex-col justify-between rounded-2xl bg-card p-4 ring-1 ${over ? "ring-primary" : "ring-foreground/10"}`
        }
        onDragOver={(event) => {
          event.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(event) => {
          event.preventDefault();
          setOver(false);
          onUpload(event.dataTransfer.files?.[0]);
        }}
        onSubmit={(event) => event.preventDefault()}
      >
        <div>
          <h2 className={compact ? "text-[11px] font-medium tracking-[0.16em] text-muted-foreground uppercase" : "font-heading text-2xl"}>
            Upload
          </h2>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            .sql, .sqlite, or .db. 25 MB maximum. MySQL and Postgres dumps are translated into the sandbox.
          </p>
        </div>
        <Label htmlFor={`${idPrefix}-file`} className="mt-3 cursor-pointer">
          <span className="inline-flex rounded-lg bg-primary px-3 py-2 text-sm font-medium text-primary-foreground">
            {uploading ? "Reading the file…" : over ? "Drop to upload" : "Choose a file"}
          </span>
          <Input
            id={`${idPrefix}-file`}
            className="sr-only"
            type="file"
            accept=".sql,.sqlite,.db,.sqlite3"
            disabled={uploading}
            onChange={(event) => {
              onUpload(event.target.files?.[0]);
              event.target.value = "";
            }}
          />
        </Label>
      </form>
      <form className={compact ? "space-y-2" : "flex min-h-44 flex-col justify-between rounded-2xl bg-card p-4 text-left ring-1 ring-foreground/10"} onSubmit={onConnect}>
        <div className="space-y-2">
          <h2 className={compact ? "text-[11px] font-medium tracking-[0.16em] text-muted-foreground uppercase" : "font-heading text-2xl"}>
            Connect a database
          </h2>
          <Label htmlFor={`${idPrefix}-connection`} className="text-xs text-muted-foreground">
            Postgres or MySQL connection string. Fathom takes a read-only snapshot. The password stays on the server.
          </Label>
          <Input
            id={`${idPrefix}-connection`}
            type="password"
            autoComplete="off"
            placeholder="postgresql://readonly:…@host:5432/db?sslmode=require"
            value={connection}
            onChange={(event) => onConnection(event.target.value)}
          />
        </div>
        <Button type="submit" className="mt-3" disabled={!connection.trim() || uploading}>
          Snapshot into the sandbox
        </Button>
      </form>
    </div>
  );
}

function StatusPill({ label }: { label: string }) {
  return <span className="rounded-full bg-card px-2.5 py-1 ring-1 ring-foreground/10">{label}</span>;
}

function applyEvent(
  assistantId: string,
  event: AgentEvent,
  setMessages: Dispatch<SetStateAction<ChatMessage[]>>,
  setDashboards: Dispatch<SetStateAction<Dashboard[]>>,
) {
  if (event.type === "step") {
    setMessages((current) =>
      current.map((message) =>
        message.id === assistantId ? { ...message, steps: upsertStep(message.steps ?? [], event.step) } : message,
      ),
    );
  } else if (event.type === "result") {
    setMessages((current) =>
      current.map((message) =>
        message.id === assistantId ? { ...message, content: event.result.answer, result: event.result } : message,
      ),
    );
    if (event.result.dashboard?.id) {
      const dashboard = event.result.dashboard;
      setDashboards((current) => [dashboard, ...current.filter((item) => item.id !== dashboard.id)]);
    }
  } else if (event.type === "confirmation") {
    setMessages((current) =>
      current.map((message) =>
        message.id === assistantId
          ? { ...message, confirmation: { sql: event.sql, token: event.token, reason: event.reason } }
          : message,
      ),
    );
  } else if (event.type === "error") {
    setMessages((current) =>
      current.map((message) => (message.id === assistantId ? { ...message, content: event.error } : message)),
    );
  }
}

function upsertStep(steps: Step[], step: Step): Step[] {
  const index = steps.findIndex((item) => item.id === step.id);
  if (index === -1) return [...steps, step];
  const next = steps.slice();
  next[index] = step;
  return next;
}
