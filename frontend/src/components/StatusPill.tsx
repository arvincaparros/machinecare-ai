import type { ApiStatus } from "../hooks/useApiHealth";

const LABELS: Record<ApiStatus, string> = {
  checking: "Checking RAG API",
  online: "RAG API connected",
  offline: "RAG API offline",
};

export function StatusPill({ status }: { status: ApiStatus }) {
  return (
    <span
      className={`status status--${status}`}
      role="status"
      aria-live="polite"
      title={LABELS[status]}
    >
      <span className="status__dot" aria-hidden="true" />
      {LABELS[status]}
    </span>
  );
}
