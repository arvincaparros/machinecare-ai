import type { Source } from "../types";

interface SourcesPanelProps {
  sources: Source[];
  citedIds: string[];
  /** True while no answer has been produced yet (no question asked). */
  idle: boolean;
}

function DocumentIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M7 3.5h6.2L18 8.3V19a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 19V5a1.5 1.5 0 0 1 1-1.5Z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path
        d="M13 3.8V8h4.2M9 12.5h6M9 16h4.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function SourcesPanel({ sources, citedIds, idle }: SourcesPanelProps) {
  return (
    <div className="sources">
      <div className="sources__head">
        <span className="sources__icon" aria-hidden="true">
          <DocumentIcon />
        </span>
        <span className="sources__title">Sources</span>
        <span className="sources__count">
          {idle
            ? "Awaiting question"
            : `${sources.length} record${sources.length === 1 ? "" : "s"}`}
        </span>
      </div>

      {sources.length === 0 ? (
        <p className="sources__none">
          {idle
            ? "Ask a question to see the downtime records behind the answer."
            : "No incident record passed the evidence threshold for this answer."}
        </p>
      ) : (
        <ul className="sources__list">
          {sources.map((source) => {
            const cited = citedIds.includes(source.incident_id);

            return (
              <li
                className={`source${cited ? " source--cited" : ""}`}
                key={source.incident_id}
              >
                <span className="source__icon" aria-hidden="true">
                  <DocumentIcon />
                </span>
                <span className="source__body">
                  <span className="source__id">{source.incident_id}</span>
                  <span className="source__machine">
                    Machine: {source.machine}
                  </span>
                </span>
                <span className="source__value">
                  <span className="source__distance">
                    {source.distance.toFixed(3)}
                  </span>
                  {cited && <span className="source__cited">cited</span>}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
