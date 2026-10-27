import type { ApiStatus } from "../hooks/useApiHealth";
import { StatusPill } from "./StatusPill";

export function Header({ status }: { status: ApiStatus }) {
  return (
    <header className="header">
      <div className="header__inner">
        <div className="brand">
          <span className="brand__mark" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none">
              <path
                d="M12 2.6 19.8 7v10L12 21.4 4.2 17V7L12 2.6Z"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinejoin="round"
              />
              <path
                d="M12 8.4 15.2 10v4L12 15.6 8.8 14v-4L12 8.4Z"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <span className="brand__text">
            <span className="brand__name">
              MachineCare <em>AI</em>
            </span>
            <span className="brand__tag">Downtime intelligence</span>
          </span>
        </div>

        <div className="header__meta">
          <StatusPill status={status} />
          <span className="header__version">RAG API · v1</span>
        </div>
      </div>
    </header>
  );
}
