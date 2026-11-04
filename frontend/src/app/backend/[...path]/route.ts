import type { NextRequest } from "next/server";

const HOP = new Set(["host", "connection", "content-length", "transfer-encoding"]);

async function proxy(req: NextRequest, ctx: RouteContext<"/backend/[...path]">) {
  const { path } = await ctx.params;
  const base = process.env.API_INTERNAL_URL || "http://127.0.0.1:43124";
  const url = `${base}/${path.join("/")}${req.nextUrl.search}`;
  const headers = new Headers();
  req.headers.forEach((value, key) => {
    if (!HOP.has(key.toLowerCase())) headers.set(key, value);
  });
  const init: RequestInit & { duplex?: "half" } = {
    method: req.method,
    headers,
    redirect: "manual",
  };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = req.body;
    init.duplex = "half";
  }
  const res = await fetch(url, init);
  const out = new Headers(res.headers);
  out.set("cache-control", "no-cache, no-transform");
  out.delete("content-encoding");
  return new Response(res.body, { status: res.status, headers: out });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const DELETE = proxy;
