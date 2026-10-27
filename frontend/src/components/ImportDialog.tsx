import { useCallback, useEffect, useRef } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";

import {
  useIncidentImport,
  type ImportCommitFailure,
  type ImportPreviewFailure,
  type IncidentImport,
} from "../hooks/useIncidentImport";
import { formatFileSize } from "../lib/files";
import { IMPORT_ACCEPT_ATTRIBUTE, IMPORT_LIMIT_LABEL } from "../lib/importFile";
import {
  IMPORT_ACCEPTED_FORMATS_LABEL,
  MAX_IMPORT_ROWS,
  type ImportCommitResponse,
  type ImportIndexingInfo,
  type ImportPreviewResponse,
  type ImportRowError,
} from "../types";

/**
 * The incident import modal.
 *
 * The dialog owns its whole flow through useIncidentImport, so closing it
 * discards the selected file, the preview and the result; nothing about an
 * import leaks into the chat state. Closing is refused while a commit is in
 * flight, because the server may already be writing.
 *
 * Everything typed here uses the import endpoints only. Chat attachments and
 * the ask flow are untouched.
 */

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  'input:not([disabled]):not([tabindex="-1"])',
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

const MAX_VISIBLE_ERRORS = 50;
const MAX_VISIBLE_IDS = 8;

function CloseIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M7 7l10 10M17 7 7 17"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

function Spinner() {
  return <span className="btn__spinner" aria-hidden="true" />;
}

function Count({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone?: "go" | "bad" | "muted";
}) {
  return (
    <div className={`import-count${tone ? ` import-count--${tone}` : ""}`}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function RowErrors({ errors }: { errors: ImportRowError[] }) {
  const visible = errors.slice(0, MAX_VISIBLE_ERRORS);

  return (
    <div className="import-errors">
      <table className="import-errors__table">
        <caption className="sr-only">Row-level problems in the file</caption>
        <thead>
          <tr>
            <th scope="col">Row</th>
            <th scope="col">Field</th>
            <th scope="col">Problem</th>
          </tr>
        </thead>
        <tbody>
          {visible.map((error, index) => (
            <tr key={`${error.row}-${error.field}-${error.code}-${index}`}>
              <td className="import-errors__row">{error.row}</td>
              <td className="import-errors__field">{error.field}</td>
              <td className="import-errors__message">
                {error.message}
                <span className="import-errors__code">{error.code}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {errors.length > visible.length && (
        <p className="import-errors__more">
          {errors.length - visible.length} more problems are in the file. Fixing
          these and checking the file again shows the rest.
        </p>
      )}
    </div>
  );
}

function PreviewSummary({
  preview,
  state,
}: {
  preview: ImportPreviewResponse;
  state: IncidentImport;
}) {
  return (
    <div className="import-preview">
      <dl className="import-counts">
        <Count label="Data rows" value={preview.total_rows} />
        <Count
          label="Invalid"
          value={preview.invalid_rows}
          tone={preview.invalid_rows > 0 ? "bad" : "muted"}
        />
        <Count
          label="Skipped · already exist"
          value={preview.skipped_existing_rows}
          tone="muted"
        />
        <Count
          label="Eligible to import"
          value={preview.eligible_rows}
          tone={preview.eligible_rows > 0 ? "go" : "muted"}
        />
      </dl>

      <p className="import-preview__meta">
        <span className="import-preview__file">{preview.file_name}</span>
        <span
          className="import-preview__digest"
          title={`SHA-256 of the checked file: ${preview.file_sha256}`}
        >
          SHA-256 {preview.file_sha256.slice(0, 12)}…
        </span>
        <span>skip_existing {preview.skip_existing ? "on" : "off"}</span>
      </p>

      {preview.errors.length > 0 && (
        <div className="import-alert import-alert--error">
          <p className="import-alert__title">
            Fix these rows before importing
          </p>
          <p>
            Invalid rows block the whole import: while any row has a problem,
            nothing is written.
          </p>
          <RowErrors errors={preview.errors} />
        </div>
      )}

      {preview.skipped_existing_rows > 0 && (
        <p className="import-note">
          {preview.skipped_existing_rows}{" "}
          {preview.skipped_existing_rows === 1
            ? "row already exists and is"
            : "rows already exist and are"}{" "}
          left out of the import. Existing incidents are never updated or
          overwritten.
        </p>
      )}

      {state.blockers.length > 0 && (
        <div className="import-alert import-alert--warn">
          {state.blockers.map((blocker) => (
            <p key={blocker}>{blocker}</p>
          ))}
        </div>
      )}
    </div>
  );
}

function IndexingPanel({
  indexing,
  state,
}: {
  indexing: ImportIndexingInfo;
  state: IncidentImport;
}) {
  const { reindex } = state;

  if (indexing.status === "failed") {
    if (reindex.status === "done") {
      return (
        <div className="import-index import-index--indexed">
          <div className="import-index__head">
            <h4 className="import-index__title">Search indexing</h4>
            <span className="import-badge import-badge--ok">Indexed</span>
          </div>
          <p>{reindex.message}</p>
          {reindex.collectionTotal !== null && (
            <p className="import-index__total">
              Collection total: <strong>{reindex.collectionTotal}</strong>{" "}
              documents. That is the size of the whole vector collection, not
              the number of rows re-indexed.
            </p>
          )}
        </div>
      );
    }

    return (
      <div className="import-index import-index--failed">
        <div className="import-index__head">
          <h4 className="import-index__title">Search indexing</h4>
          <span className="import-badge import-badge--bad">Needs attention</span>
        </div>

        <p>
          <strong>
            The rows were imported successfully, but indexing needs attention.
          </strong>{" "}
          {indexing.message ??
            "Refresh the search index before these incidents can be retrieved as evidence."}
        </p>

        {reindex.status === "error" && (
          <p className="import-index__error" role="alert">
            {reindex.message}
          </p>
        )}

        <p className="import-index__hint">
          Retrying re-indexes every Closed incident, not only the rows just
          imported. It upserts into ChromaDB and never changes the PostgreSQL
          records.
        </p>

        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => void state.retryIndex()}
          disabled={reindex.status === "running"}
        >
          {reindex.status === "running" ? (
            <>
              <Spinner />
              Reindexing…
            </>
          ) : (
            "Retry indexing"
          )}
        </button>
      </div>
    );
  }

  if (indexing.status === "skipped") {
    return (
      <div className="import-index import-index--skipped">
        <div className="import-index__head">
          <h4 className="import-index__title">Search indexing</h4>
          <span className="import-badge import-badge--muted">Skipped</span>
        </div>
        <p>
          {indexing.message ??
            "No row was inserted, so there was nothing to index."}
        </p>
      </div>
    );
  }

  return (
    <div className="import-index import-index--indexed">
      <div className="import-index__head">
        <h4 className="import-index__title">Search indexing</h4>
        <span className="import-badge import-badge--ok">Indexed</span>
      </div>
      <p>The search index was refreshed after the commit.</p>
      {indexing.collection_total !== null && (
        <p className="import-index__total">
          Collection total: <strong>{indexing.collection_total}</strong>{" "}
          documents. That is the size of the whole vector collection, not the
          number of rows this import added.
        </p>
      )}
    </div>
  );
}

function ImportOutcome({
  result,
  state,
}: {
  result: ImportCommitResponse;
  state: IncidentImport;
}) {
  const ids = result.imported_incident_ids;
  const shownIds = ids.slice(0, MAX_VISIBLE_IDS);

  return (
    <section className="import-outcome" aria-labelledby="import-outcome-title">
      <div
        className={`import-outcome__banner import-outcome__banner--${
          result.database_committed ? "ok" : "bad"
        }`}
      >
        <h3 id="import-outcome-title">
          {result.database_committed
            ? "Database committed"
            : "Database not committed"}
        </h3>
        <p>
          {result.database_committed
            ? "The eligible incidents and their resolutions were inserted in one transaction."
            : "The rows were not committed. Check the file and import it again."}
        </p>
      </div>

      <dl className="import-counts">
        <Count label="Rows imported" value={result.imported_rows} tone="go" />
        <Count
          label="Skipped · already exist"
          value={result.skipped_existing_rows}
          tone="muted"
        />
      </dl>

      {ids.length > 0 && (
        <p className="import-outcome__ids">
          <span className="import-outcome__ids-label">
            Imported incident IDs
          </span>
          {shownIds.map((incidentId) => (
            <code key={incidentId}>{incidentId}</code>
          ))}
          {ids.length > shownIds.length && (
            <span>and {ids.length - shownIds.length} more</span>
          )}
        </p>
      )}

      <IndexingPanel indexing={result.indexing} state={state} />
    </section>
  );
}

function PreviewFailureAlert({
  failure,
  state,
}: {
  failure: ImportPreviewFailure;
  state: IncidentImport;
}) {
  return (
    <div className="import-alert import-alert--error" role="alert">
      <p className="import-alert__title">The file could not be checked</p>
      <p>{failure.message}</p>
      {failure.rowErrors.length > 0 && <RowErrors errors={failure.rowErrors} />}
      {state.file !== null && (
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => void state.previewFile()}
          disabled={state.isPreviewing}
        >
          Check the file again
        </button>
      )}
    </div>
  );
}

function CommitFailureAlert({
  failure,
  state,
}: {
  failure: ImportCommitFailure;
  state: IncidentImport;
}) {
  return (
    <div className="import-alert import-alert--error" role="alert">
      <p className="import-alert__title">The import was not confirmed</p>
      <p>{failure.message}</p>

      {failure.ambiguous && (
        <p className="import-alert__strong">
          The request ended without a definite answer, so the server may already
          have committed these rows. Check the incident records and the search
          index before importing the file again.
        </p>
      )}

      {failure.requiresPreview && (
        <>
          <p>
            The preview was cleared because it no longer describes what an
            import would do.
          </p>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => void state.previewFile()}
            disabled={state.isPreviewing || state.file === null}
          >
            Check the file again
          </button>
        </>
      )}

      {failure.rowErrors.length > 0 && <RowErrors errors={failure.rowErrors} />}
    </div>
  );
}

export function ImportDialog({ onClose }: { onClose: () => void }) {
  const state = useIncidentImport();
  const dialogRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { isCommitting } = state;

  const close = useCallback(() => {
    // Never abandon a commit: the transaction may already be writing.
    if (isCommitting) {
      return;
    }

    onClose();
  }, [isCommitting, onClose]);

  // Move focus into the dialog, and give it back to the trigger on close.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();

    return () => previous?.focus?.();
  }, []);

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      close();
      return;
    }

    if (event.key !== "Tab" || !dialogRef.current) {
      return;
    }

    // Cycle Tab inside the dialog so the page behind it is out of reach.
    const focusable = Array.from(
      dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
    ).filter((element) => element.offsetParent !== null);

    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }

    const first = focusable[0];
    const last = focusable[focusable.length - 1];

    if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    }
  };

  const { preview } = state;
  const commitLabel = preview
    ? `Import ${preview.eligible_rows} ${
        preview.eligible_rows === 1 ? "row" : "rows"
      }`
    : "Import rows";

  const liveText = state.commitResult
    ? `Import finished. ${state.commitResult.imported_rows} rows imported, ${state.commitResult.skipped_existing_rows} skipped. Indexing ${state.commitResult.indexing.status}.`
    : state.isCommitting
      ? "Importing the checked rows."
      : state.isPreviewing
        ? "Checking the file against the incident database."
        : preview
          ? `${preview.total_rows} data rows: ${preview.invalid_rows} invalid, ${preview.skipped_existing_rows} skipped, ${preview.eligible_rows} eligible to import.`
          : "";

  return (
    <div
      className="dialog-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          close();
        }
      }}
    >
      <div
        className="import-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="import-title"
        aria-describedby="import-summary"
        aria-busy={isCommitting}
        ref={dialogRef}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <header className="import-dialog__head">
          <div className="import-dialog__heading">
            <span className="eyebrow">Incident import</span>
            <h2 id="import-title">Import closed downtime incidents</h2>
            <p id="import-summary">
              Choose a {IMPORT_ACCEPTED_FORMATS_LABEL} file, check it against the
              database, then import the rows that pass.
            </p>
          </div>

          <button
            type="button"
            className="import-dialog__close"
            onClick={close}
            disabled={isCommitting}
            aria-label="Close the import dialog"
            title={isCommitting ? "The import is committing" : "Close"}
          >
            <CloseIcon />
          </button>
        </header>

        <div className="import-dialog__body">
          <p className="sr-only" role="status">
            {liveText}
          </p>

          <p className="import-notice">
            <strong>Local development only.</strong> The import endpoints have no
            authentication or authorisation, so anyone who can reach the API can
            write to the incident database. Do not expose this app on a shared or
            public network.
          </p>

          {state.commitResult ? (
            <ImportOutcome result={state.commitResult} state={state} />
          ) : (
            <>
              <section className="import-step" aria-labelledby="import-step-file">
                <div className="import-step__head">
                  <h3 id="import-step-file">1 · Choose the file</h3>
                  <span className="import-step__meta">
                    {IMPORT_LIMIT_LABEL} · up to{" "}
                    {MAX_IMPORT_ROWS.toLocaleString()} data rows
                  </span>
                </div>

                <div className="import-file">
                  <input
                    ref={fileInputRef}
                    className="sr-only"
                    type="file"
                    accept={IMPORT_ACCEPT_ATTRIBUTE}
                    tabIndex={-1}
                    aria-hidden="true"
                    disabled={isCommitting}
                    onChange={(event) => {
                      const chosen = event.target.files?.[0] ?? null;
                      // Reset so choosing the same file again still fires.
                      event.target.value = "";
                      state.selectFile(chosen);
                    }}
                  />

                  <button
                    type="button"
                    className="btn btn--ghost"
                    onClick={() => fileInputRef.current?.click()}
                    disabled={isCommitting}
                    aria-describedby="import-file-name"
                  >
                    {state.file ? "Choose a different file" : "Choose file"}
                  </button>

                  <span
                    className="import-file__name"
                    id="import-file-name"
                    aria-live="polite"
                  >
                    {state.file ? (
                      <>
                        <span className="import-file__label">
                          {state.file.name}
                        </span>
                        <span className="import-file__size">
                          {formatFileSize(state.file.size)}
                        </span>
                      </>
                    ) : (
                      <span className="import-file__empty">
                        No file selected
                      </span>
                    )}
                  </span>
                </div>

                {state.fileError && (
                  <p className="import-alert import-alert--error" role="alert">
                    {state.fileError}
                  </p>
                )}

                <p className="import-step__hint">
                  Start from the template:{" "}
                  <button
                    type="button"
                    className="btn btn--link"
                    onClick={() => void state.downloadTemplate("xlsx")}
                    disabled={state.templateBusy !== null}
                  >
                    {state.templateBusy === "xlsx"
                      ? "Downloading…"
                      : "XLSX template"}
                  </button>
                  <span aria-hidden="true"> · </span>
                  <button
                    type="button"
                    className="btn btn--link"
                    onClick={() => void state.downloadTemplate("csv")}
                    disabled={state.templateBusy !== null}
                  >
                    {state.templateBusy === "csv"
                      ? "Downloading…"
                      : "CSV template"}
                  </button>
                </p>

                {state.templateError && (
                  <p className="import-alert import-alert--error" role="alert">
                    {state.templateError}
                  </p>
                )}
              </section>

              <section
                className="import-step"
                aria-labelledby="import-step-options"
              >
                <h3 id="import-step-options">2 · Options</h3>

                <label className="import-check">
                  <input
                    type="checkbox"
                    checked={state.skipExisting}
                    onChange={(event) =>
                      state.changeSkipExisting(event.target.checked)
                    }
                    disabled={isCommitting || state.isPreviewing}
                  />
                  <span>
                    <span className="import-check__title">
                      Skip rows whose incident ID already exists
                    </span>
                    <span className="import-check__hint">
                      Off by default, which makes an existing ID a row error.
                      Either way, existing incidents are never updated or
                      overwritten.
                    </span>
                  </span>
                </label>
              </section>

              <section className="import-step" aria-labelledby="import-step-check">
                <div className="import-step__head">
                  <h3 id="import-step-check">3 · Check the file</h3>
                  <button
                    type="button"
                    className="btn btn--ghost btn--sm"
                    onClick={() => void state.previewFile()}
                    disabled={
                      state.file === null ||
                      state.fileError !== null ||
                      state.isPreviewing ||
                      isCommitting
                    }
                  >
                    {state.isPreviewing ? (
                      <>
                        <Spinner />
                        Checking…
                      </>
                    ) : preview ? (
                      "Check again"
                    ) : (
                      "Preview"
                    )}
                  </button>
                </div>

                {state.isPreviewing && (
                  <p className="import-step__hint">
                    Reading {state.file?.name} and comparing it with the incident
                    database. Nothing is written.
                  </p>
                )}

                {!state.isPreviewing && !preview && !state.previewFailure && (
                  <p className="import-step__hint">
                    Checking a file is read-only: it parses the rows, validates
                    them and compares the incident IDs and machine codes with the
                    database.
                  </p>
                )}

                {state.previewFailure && (
                  <PreviewFailureAlert
                    failure={state.previewFailure}
                    state={state}
                  />
                )}

                {preview && <PreviewSummary preview={preview} state={state} />}
              </section>

              {preview && (
                <section
                  className="import-step import-step--confirm"
                  aria-labelledby="import-step-confirm"
                >
                  <h3 id="import-step-confirm">4 · Import</h3>
                  <p className="import-step__hint">
                    The import re-checks the file against the database as it is
                    now, then inserts the eligible rows and their resolutions in a
                    single transaction. If any row fails, nothing is written.
                  </p>
                </section>
              )}

              {state.commitFailure && (
                <CommitFailureAlert failure={state.commitFailure} state={state} />
              )}
            </>
          )}
        </div>

        <footer className="import-dialog__foot">
          {state.commitResult ? (
            <>
              <span className="import-dialog__hint">
                The database stage is finished. Indexing can be retried above.
              </span>
              <div className="import-dialog__actions">
                <button type="button" className="btn btn--ghost" onClick={close}>
                  Close
                </button>
                <button
                  type="button"
                  className="btn btn--primary"
                  onClick={state.reset}
                >
                  Import another file
                </button>
              </div>
            </>
          ) : (
            <>
              <span className="import-dialog__hint">
                {isCommitting
                  ? "Importing in one transaction. Keep this window open."
                  : "Rows are inserted in one transaction. Existing records are never modified."}
              </span>
              <div className="import-dialog__actions">
                <button
                  type="button"
                  className="btn btn--ghost"
                  onClick={close}
                  disabled={isCommitting}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn btn--primary"
                  onClick={() => void state.commit()}
                  disabled={!state.canCommit}
                >
                  {isCommitting ? (
                    <>
                      <Spinner />
                      Importing…
                    </>
                  ) : (
                    commitLabel
                  )}
                </button>
              </div>
            </>
          )}
        </footer>
      </div>
    </div>
  );
}
