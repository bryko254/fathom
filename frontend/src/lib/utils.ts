export { cn } from "cn"

// crypto.randomUUID only exists in secure contexts (HTTPS or localhost).
// The demo VM is served over plain HTTP, so fall back to a random id.
export function newId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
