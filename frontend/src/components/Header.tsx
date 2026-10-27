import type { ApiStatus } from "../hooks/useApiHealth";
import { StatusPill } from "./StatusPill";

interface HeaderProps {
  status: ApiStatus;
  /** Opens the incident import dialog; the dialog itself lives in App. */
  onOpenImport: () => void;
}

export function Header({ status, onOpenImport }: HeaderProps) {
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
          <button
            type="button"
            className="header__import"
            onClick={onOpenImport}
            aria-haspopup="dialog"
            aria-label="Import incidents"
            title="Import incidents from a CSV or XLSX file"
          >
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path
                d="M12 15.5V3.8m0 0L7.8 8M12 3.8 16.2 8"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
              <path
                d="M4 14.5v4A1.8 1.8 0 0 0 5.8 20.3h12.4A1.8 1.8 0 0 0 20 18.5v-4"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
              />
            </svg>
            <span className="header__import-label">Import incidents</span>
          </button>

          <StatusPill status={status} />
          <span className="header__version">RAG API · v1</span>
        </div>
      </div>
    </header>
  );
}
