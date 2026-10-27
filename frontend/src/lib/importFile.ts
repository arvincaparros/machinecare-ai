/**
 * Client-side import file checks and the template download helper.
 *
 * These checks only keep an obviously unusable file out of the upload; the
 * API re-reads the file, enforces its own 5 MB and 5,000-row limits, and
 * validates every cell. Nothing here parses the file.
 */

import {
  IMPORT_ACCEPTED_EXTENSIONS,
  IMPORT_ACCEPTED_FORMATS_LABEL,
  MAX_IMPORT_BYTES,
} from "../types";
import { fileExtension, formatFileSize } from "./files";

export const IMPORT_ACCEPT_ATTRIBUTE = IMPORT_ACCEPTED_EXTENSIONS.join(",");

const MAX_IMPORT_LABEL = `${MAX_IMPORT_BYTES / (1024 * 1024)} MB`;

type AcceptedImportExtension = (typeof IMPORT_ACCEPTED_EXTENSIONS)[number];

/**
 * Return the reason a file cannot be imported, or null when it can.
 *
 * Called as soon as a file is chosen so the user is not sent through a
 * preview that is certain to fail.
 */
export function validateImportFile(file: File): string | null {
  const label = `"${file.name}"`;
  const extension = fileExtension(file.name);

  if (
    !IMPORT_ACCEPTED_EXTENSIONS.includes(extension as AcceptedImportExtension)
  ) {
    return `${label} is not a supported import file. Choose a ${IMPORT_ACCEPTED_FORMATS_LABEL} file.`;
  }

  if (file.size === 0) {
    return `${label} is empty. Choose a file that contains data rows.`;
  }

  if (file.size > MAX_IMPORT_BYTES) {
    return `${label} is ${formatFileSize(file.size)}. The import limit is ${MAX_IMPORT_LABEL}.`;
  }

  return null;
}

/** Handle a downloaded blob: the browser saves it, the page never navigates. */
export function saveDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");

  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noopener";
  anchor.style.display = "none";

  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();

  // Revoke on the next task so the browser has started reading the blob.
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** Human-readable limit for labels and hints. */
export const IMPORT_LIMIT_LABEL = `${IMPORT_ACCEPTED_FORMATS_LABEL} · max ${MAX_IMPORT_LABEL}`;
