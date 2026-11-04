"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { DataTable } from "@/components/chart-view";
import type { Step } from "@/lib/types";

const LABEL: Record<Step["status"], string> = {
  running: "Running",
  ok: "Passed",
  error: "Failed",
  warning: "Needs a fix",
  fixed: "Corrected",
  blocked: "Blocked",
};

export function Trace({ steps }: { steps: Step[] }) {
  return (
    <ol className="space-y-3" aria-live="polite">
      {steps.map((step, index) => (
        <li key={step.id} className="grid grid-cols-[1.25rem_minmax(0,1fr)] gap-3">
          <div className="flex flex-col items-center">
            <span
              className={`mt-1 size-2.5 rounded-full ${dotClass(step.status)} ${step.status === "running" ? "motion-safe:animate-pulse" : ""}`}
            />
            {index < steps.length - 1 ? <span className="mt-1 w-px flex-1 bg-border" /> : null}
          </div>
          <article className="min-w-0 pb-2">
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <h3 className="text-sm font-medium">{step.title}</h3>
              <span className={`text-[11px] font-medium tracking-wide uppercase ${tone(step.status)}`}>
                {LABEL[step.status]}
              </span>
              {typeof step.elapsedMs === "number" && step.status !== "running" ? (
                <span className="text-[11px] text-muted-foreground">{step.elapsedMs} ms</span>
              ) : null}
            </div>
            {step.detail ? <p className="mt-1 text-sm text-muted-foreground">{step.detail}</p> : null}
            {step.sql ? <SqlBlock sql={step.sql} /> : null}
            {step.code ? (
              <pre className="mt-2 max-h-40 overflow-auto rounded-md bg-[#221c16] p-2 text-[11px] leading-relaxed text-[#f3eadf]">
                {step.code}
              </pre>
            ) : null}
            {step.rows && step.rows.length > 0 && step.columns ? (
              <div className="mt-2">
                <DataTable columns={step.columns} rows={step.rows} />
              </div>
            ) : null}
          </article>
        </li>
      ))}
    </ol>
  );
}

function SqlBlock({ sql }: { sql: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="mt-2 overflow-hidden rounded-md ring-1 ring-foreground/10">
      <div className="flex items-center justify-between bg-muted/70 px-2 py-1">
        <span className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">SQL</span>
        <Button
          type="button"
          size="xs"
          variant="ghost"
          onClick={async () => {
            await navigator.clipboard.writeText(sql);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1200);
          }}
        >
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      <pre className="max-h-48 overflow-auto bg-card p-2 text-[11px] leading-relaxed">{sql}</pre>
    </div>
  );
}

function dotClass(status: Step["status"]): string {
  if (status === "error" || status === "blocked") return "bg-destructive";
  if (status === "warning") return "bg-[#b8612a]";
  if (status === "fixed" || status === "running") return "bg-primary";
  return "bg-[#8d8478]";
}

function tone(status: Step["status"]): string {
  if (status === "error" || status === "blocked") return "text-destructive";
  if (status === "warning") return "text-[#8a4b16]";
  if (status === "fixed") return "text-primary";
  return "text-muted-foreground";
}
