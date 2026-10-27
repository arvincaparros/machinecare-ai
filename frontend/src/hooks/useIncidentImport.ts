/**
 * State for one incident import: file selection, preview, commit and the
 * indexing retry.
 *
 * The rule that keeps the flow safe is structural: any change to the file or
 * to `skip_existing` throws the preview away, and a commit is only ever sent
 * with the digest of the preview that is currently displayed. An import that
 * failed in an ambiguous way also throws its preview away, because the server
 * may already have committed the rows.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/client";
import {
  ImportRequestError,
  commitIncidentImport,
  downloadImportTemplate,
  previewIncidentImport,
  reindexIncidents,
  type ImportTemplateFormat,
} from "../api/importClient";
import { saveDownload, validateImportFile } from "../lib/importFile";
import type {
  ImportCommitResponse,
  ImportPreviewResponse,
  ImportRowError,
} from "../types";

export interface ImportPreviewFailure {
  message: string;
  rowErrors: ImportRowError[];
}

export interface ImportCommitFailure extends ImportPreviewFailure {
  /** The server may already have committed; the outcome is unknown. */
  ambiguous: boolean;
  /** The preview no longer authorises a commit; check the file again. */
  requiresPreview: boolean;
}

export type ReindexState =
  | { status: "idle" }
  | { status: "running" }
  | { status: "done"; collectionTotal: number | null; message: string }
  | { status: "error"; message: string };

function messageFor(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    return error.message;
  }

  return fallback;
}

function describeCommitFailure(error: unknown): ImportCommitFailure {
  if (!(error instanceof ImportRequestError)) {
    return {
      message: "Something went wrong while contacting the RAG API.",
      rowErrors: [],
      ambiguous: true,
      requiresPreview: true,
    };
  }

  const status = error.status;

  // A 500 from this API means the transaction rolled back ("No row was
  // written"). Any other 5xx, a timeout, a dropped connection or a 200 with
  // an unreadable body leaves the outcome unknown.
  const ambiguous =
    error.kind === "timeout" ||
    error.kind === "network" ||
    error.kind === "invalid-response" ||
    (typeof status === "number" && status >= 500 && status !== 500);

  return {
    message: error.message,
    rowErrors: error.rowErrors,
    ambiguous,
    requiresPreview: ambiguous || status === 409 || status === 422,
  };
}

/** Why the confirmation action is unavailable for a preview. */
export function commitBlockers(preview: ImportPreviewResponse | null): string[] {
  if (!preview) {
    return [];
  }

  const blockers: string[] = [];

  if (preview.invalid_rows > 0) {
    blockers.push(
      `${preview.invalid_rows} ${
        preview.invalid_rows === 1 ? "row has" : "rows have"
      } problems. The import refuses the whole file while any row is invalid, so fix the file and check it again.`,
    );
  }

  if (preview.eligible_rows === 0) {
    blockers.push(
      preview.skipped_existing_rows > 0
        ? "Every row that passed the checks already exists, so there is nothing new to import."
        : "No row in this file can be imported.",
    );
  }

  return blockers;
}

export function useIncidentImport() {
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [skipExisting, setSkipExisting] = useState(false);
  const [preview, setPreview] = useState<ImportPreviewResponse | null>(null);
  const [previewFailure, setPreviewFailure] =
    useState<ImportPreviewFailure | null>(null);
  const [isPreviewing, setIsPreviewing] = useState(false);
  const [commitResult, setCommitResult] =
    useState<ImportCommitResponse | null>(null);
  const [commitFailure, setCommitFailure] =
    useState<ImportCommitFailure | null>(null);
  const [isCommitting, setIsCommitting] = useState(false);
  const [reindex, setReindex] = useState<ReindexState>({ status: "idle" });
  const [templateBusy, setTemplateBusy] =
    useState<ImportTemplateFormat | null>(null);
  const [templateError, setTemplateError] = useState<string | null>(null);

  // One import request at a time. A ref, not state: two clicks in the same
  // tick would both read the same stale state value.
  const busy = useRef(false);
  const previewController = useRef<AbortController | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;

    return () => {
      mounted.current = false;
      // Only the preview is safe to abandon. A commit is never aborted: the
      // server may still be committing, so its outcome must stay unknown
      // rather than silent.
      previewController.current?.abort();
    };
  }, []);

  const selectFile = useCallback((next: File | null) => {
    previewController.current?.abort();

    const error = next ? validateImportFile(next) : null;

    // A rejected file never becomes the selection, exactly like the chat
    // composer: the message explains why, and nothing is remembered.
    setFile(error ? null : next);
    setFileError(error);
    setPreview(null);
    setPreviewFailure(null);
    setCommitResult(null);
    setCommitFailure(null);
    setReindex({ status: "idle" });
  }, []);

  const changeSkipExisting = useCallback(
    (value: boolean) => {
      if (value === skipExisting) {
        return;
      }

      // The counts depend on this option, so the old preview no longer
      // describes what a commit would do.
      previewController.current?.abort();
      setSkipExisting(value);
      setPreview(null);
      setPreviewFailure(null);
      setCommitFailure(null);
    },
    [skipExisting],
  );

  const previewFile = useCallback(async () => {
    if (!file || fileError || busy.current) {
      return;
    }

    previewController.current?.abort();
    const controller = new AbortController();
    previewController.current = controller;

    busy.current = true;
    setIsPreviewing(true);
    setPreview(null);
    setPreviewFailure(null);
    setCommitFailure(null);

    try {
      const response = await previewIncidentImport(
        file,
        skipExisting,
        controller.signal,
      );

      if (!mounted.current || controller.signal.aborted) {
        return;
      }

      setPreview(response);
    } catch (error) {
      if (!mounted.current || controller.signal.aborted) {
        return;
      }

      setPreviewFailure({
        message: messageFor(error, "The file could not be checked."),
        rowErrors:
          error instanceof ImportRequestError ? error.rowErrors : [],
      });
    } finally {
      busy.current = false;

      if (mounted.current) {
        setIsPreviewing(false);
      }
    }
  }, [file, fileError, skipExisting]);

  const canCommit =
    preview !== null &&
    preview.invalid_rows === 0 &&
    preview.eligible_rows > 0 &&
    !isPreviewing &&
    !isCommitting;

  const commit = useCallback(async () => {
    if (!file || !preview || busy.current) {
      return;
    }

    if (preview.invalid_rows > 0 || preview.eligible_rows === 0) {
      return;
    }

    // Both the file and the option are cleared into the preview when they
    // change, so this guard should never fire; it exists so a stale preview
    // can never authorise an insert.
    if (preview.skip_existing !== skipExisting) {
      setPreview(null);
      return;
    }

    busy.current = true;
    setIsCommitting(true);
    setCommitFailure(null);

    try {
      const result = await commitIncidentImport({
        file,
        fileSha256: preview.file_sha256,
        skipExisting: preview.skip_existing,
      });

      if (!mounted.current) {
        return;
      }

      setCommitResult(result);
      // The preview has been spent: importing it a second time would be a
      // second, unintended import.
      setPreview(null);
    } catch (error) {
      if (!mounted.current) {
        return;
      }

      const failure = describeCommitFailure(error);
      setCommitFailure(failure);

      if (failure.requiresPreview) {
        setPreview(null);
      }
    } finally {
      busy.current = false;

      if (mounted.current) {
        setIsCommitting(false);
      }
    }
  }, [file, preview, skipExisting]);

  const retryIndex = useCallback(async () => {
    if (busy.current) {
      return;
    }

    busy.current = true;
    setReindex({ status: "running" });

    try {
      const response = await reindexIncidents();

      if (!mounted.current) {
        return;
      }

      setReindex({
        status: "done",
        collectionTotal: response.collection_total,
        message: response.message,
      });
    } catch (error) {
      if (!mounted.current) {
        return;
      }

      setReindex({
        status: "error",
        message: messageFor(error, "The search index could not be refreshed."),
      });
    } finally {
      busy.current = false;
    }
  }, []);

  const downloadTemplate = useCallback(async (format: ImportTemplateFormat) => {
    if (busy.current) {
      return;
    }

    busy.current = true;
    setTemplateBusy(format);
    setTemplateError(null);

    try {
      const template = await downloadImportTemplate(format);

      if (!mounted.current) {
        return;
      }

      saveDownload(template.blob, template.filename);
    } catch (error) {
      if (!mounted.current) {
        return;
      }

      setTemplateError(
        messageFor(error, "The template could not be downloaded."),
      );
    } finally {
      busy.current = false;

      if (mounted.current) {
        setTemplateBusy(null);
      }
    }
  }, []);

  /** Start over: the result screen's "import another file". */
  const reset = useCallback(() => {
    previewController.current?.abort();
    setFile(null);
    setFileError(null);
    setSkipExisting(false);
    setPreview(null);
    setPreviewFailure(null);
    setCommitResult(null);
    setCommitFailure(null);
    setReindex({ status: "idle" });
    setTemplateError(null);
  }, []);

  return {
    file,
    fileError,
    skipExisting,
    preview,
    previewFailure,
    isPreviewing,
    commitResult,
    commitFailure,
    isCommitting,
    reindex,
    templateBusy,
    templateError,
    canCommit,
    blockers: commitBlockers(preview),
    selectFile,
    changeSkipExisting,
    previewFile,
    commit,
    retryIndex,
    downloadTemplate,
    reset,
  };
}

export type IncidentImport = ReturnType<typeof useIncidentImport>;
