import type { AskResponse } from "../types";
import { Markdown } from "./Markdown";

export function AnswerCard({ response }: { response: AskResponse }) {
  const { answer, sources, cited_incident_ids, unsupported_citations } = response;
  const documents = response.documents ?? [];

  return (
    <article className="answer">
      <header className="answer__head">
        <span className="answer__badge">MachineCare AI</span>
        <span className="answer__meta">
          {sources.length === 0
            ? "No matching records"
            : `${sources.length} source${sources.length === 1 ? "" : "s"} cited`}
        </span>
      </header>

      <Markdown source={answer} />

      {documents.length > 0 && (
        <div className="answer__documents" aria-label="Documents analyzed">
          <span className="answer__documents-label">Documents analyzed</span>
          <ul>
            {documents.map((document) => (
              <li key={document.filename}>
                <span className="answer__document-name">{document.filename}</span>
                <span className="answer__document-meta">
                  {document.characters.toLocaleString()} characters
                  {document.truncated ? " · truncated" : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {(cited_incident_ids.length > 0 || unsupported_citations.length > 0) && (
        <div className="citations" aria-label="Citation check">
          {cited_incident_ids.map((id) => (
            <span className="citation citation--ok" key={`cited-${id}`}>
              <span className="citation__dot" aria-hidden="true" />
              {id}
            </span>
          ))}
          {unsupported_citations.map((id) => (
            <span
              className="citation citation--warn"
              key={`unsupported-${id}`}
              title="Mentioned in the answer but not present in the retrieved records."
            >
              <span className="citation__dot" aria-hidden="true" />
              {id} · unverified
            </span>
          ))}
        </div>
      )}
    </article>
  );
}
