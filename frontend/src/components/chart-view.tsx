"use client";

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ChartSpec } from "@/lib/types";

const INK = "#1e4d38";
const COPPER = "#b8612a";

export function ChartView({ chart }: { chart: ChartSpec }) {
  const data = chart.data ?? [];
  if (!data.length) {
    return <p className="text-sm text-muted-foreground">The chart has no rows yet.</p>;
  }
  const axes = [
    <CartesianGrid key="grid" stroke="#e4d8c8" vertical={false} />,
    <XAxis key="x" dataKey={chart.xKey} tick={{ fill: "#5c5348", fontSize: 11 }} interval="preserveStartEnd" />,
    <YAxis key="y" tick={{ fill: "#5c5348", fontSize: 11 }} width={56} />,
    <Tooltip
      key="tip"
      contentStyle={{ background: "#fbf7f0", border: "1px solid #e4d8c8", borderRadius: 8, fontSize: 12 }}
    />,
  ];
  return (
    <div className="h-60 w-full" role="img" aria-label={chart.title}>
      <ResponsiveContainer width="100%" height="100%">
        {chart.type === "line" ? (
          <LineChart data={data}>
            {axes}
            <Line type="monotone" dataKey={chart.yKey} stroke={INK} strokeWidth={2} dot={false} />
          </LineChart>
        ) : chart.type === "area" ? (
          <AreaChart data={data}>
            {axes}
            <Area type="monotone" dataKey={chart.yKey} stroke={INK} fill={COPPER} fillOpacity={0.25} />
          </AreaChart>
        ) : (
          <BarChart data={data}>
            {axes}
            <Bar dataKey={chart.yKey} fill={INK} radius={[3, 3, 0, 0]} />
          </BarChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}

export function DataTable({
  columns,
  rows,
  title,
}: {
  columns: string[];
  rows: Record<string, unknown>[];
  title?: string;
}) {
  if (!rows.length) {
    return <p className="text-sm text-muted-foreground">No rows.</p>;
  }
  return (
    <div className="max-h-72 overflow-auto rounded-lg ring-1 ring-foreground/10">
      <table className="w-full border-collapse text-left text-xs">
        {title ? <caption className="sr-only">{title}</caption> : null}
        <thead className="sticky top-0 bg-card">
          <tr>
            {columns.map((column) => (
              <th key={column} className="px-2 py-1.5 font-medium text-muted-foreground">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 20).map((row, index) => (
            <tr key={index} className="border-t border-border/80">
              {columns.map((column) => (
                <td key={column} className="px-2 py-1.5 align-top font-mono text-[11px]">
                  {formatCell(row[column])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toLocaleString();
  return String(value);
}
