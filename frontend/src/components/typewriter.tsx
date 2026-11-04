"use client";

import { useEffect, useState } from "react";

function prefersReducedMotion() {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

/** Reveals `text` character by character; caret blinks while typing (or always if `keepCaret`). */
export function Typewriter({
  text,
  msPerChar = 12,
  maxDurationMs = 2500,
  keepCaret = false,
  onDone,
  className,
}: {
  text: string;
  msPerChar?: number;
  maxDurationMs?: number;
  keepCaret?: boolean;
  onDone?: () => void;
  className?: string;
}) {
  const [count, setCount] = useState(0);

  useEffect(() => {
    const duration = prefersReducedMotion() ? 0 : Math.min(text.length * msPerChar, maxDurationMs);
    let frame = 0;
    let start: number | null = null;
    const step = (now: number) => {
      if (start === null) start = now;
      const progress = duration <= 0 ? 1 : Math.min(1, (now - start) / duration);
      setCount(Math.ceil(progress * text.length));
      if (progress < 1) frame = requestAnimationFrame(step);
      else onDone?.();
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, msPerChar, maxDurationMs]);

  const typing = count < text.length;
  return (
    <span className={className} aria-label={text}>
      <span aria-hidden>{text.slice(0, count)}</span>
      {typing || keepCaret ? <span aria-hidden className="tw-caret" /> : null}
    </span>
  );
}

export function ThinkingDots() {
  return (
    <span className="tw-thinking" role="status" aria-label="Thinking">
      <span />
      <span />
      <span />
    </span>
  );
}
