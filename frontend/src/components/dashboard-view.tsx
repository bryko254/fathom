"use client";

import { Button } from "@/components/ui/button";
import { ChartView, DataTable } from "@/components/chart-view";
import type { Dashboard, Widget } from "@/lib/types";

export function DashboardView({
  dashboard,
  busy,
  onRefresh,
  onBack,
}: {
  dashboard: Dashboard;
  busy: boolean;
  onRefresh: (resync: boolean) => void;
  onBack: () => void;
}) {
  return (
    <section className="flex min-h-0 flex-1 flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-[11px] font-medium tracking-[0.16em] text-muted-foreground uppercase">Dashboard</p>
          <h2 className="font-heading text-2xl">{dashboard.title}</h2>
        </div>
        <div className="flex gap-2">
          <Button type="button" variant="outline" size="sm" onClick={onBack}>
            Back to the question
          </Button>
          <Button type="button" size="sm" disabled={busy} onClick={() => onRefresh(false)}>
            {busy ? "Refreshing" : "Refresh"}
          </Button>
        </div>
      </div>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {(dashboard.widgets ?? []).map((widget) => (
          <WidgetCard key={widget.id ?? widget.title} widget={widget} />
        ))}
      </div>
    </section>
  );
}

function WidgetCard({ widget }: { widget: Widget }) {
  const preview = widget.preview;
  const error = preview?.error;
  return (
    <article className="flex flex-col gap-2 rounded-xl bg-card p-3 ring-1 ring-foreground/10">
      <h3 className="text-sm font-medium">{widget.title}</h3>
      {error ? <p className="text-sm text-destructive">{error}</p> : null}
      {widget.kind === "metric" ? (
        <p className="font-heading text-3xl">
          {widget.spec?.prefix ?? ""}
          {preview?.value === undefined || preview?.value === null ? "—" : String(preview.value)}
        </p>
      ) : null}
      {widget.kind === "chart" && widget.spec?.xKey && widget.spec.yKey ? (
        <ChartView
          chart={{
            type: widget.spec.type ?? "bar",
            title: widget.title,
            xKey: widget.spec.xKey,
            yKey: widget.spec.yKey,
            data: preview?.rows ?? [],
          }}
        />
      ) : null}
      {widget.kind === "table" ? (
        <DataTable columns={preview?.columns ?? []} rows={preview?.rows ?? []} title={widget.title} />
      ) : null}
      <details className="text-xs text-muted-foreground">
        <summary className="cursor-pointer">SQL behind this tile</summary>
        <pre className="mt-2 max-h-36 overflow-auto whitespace-pre-wrap">{widget.sql}</pre>
      </details>
    </article>
  );
}
