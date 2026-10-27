/**
 * Client-side attachment validation.
 *
 * These checks only decide what the composer lets the user select; the
 * backend re-validates extension, declared type, size and file signature.
 * Nothing here reads file contents.
 */

import {
  ACCEPTED_EXTENSIONS,
  ACCEPTED_FORMATS_LABEL,
  MAX_ATTACHMENT_BYTES,
  MAX_ATTACHMENTS,
} from "../types";

export interface AttachmentFile {
  id: string;
  file: File;
  name: string;
  size: number;
  extension: string;
}

export interface AttachmentSelection {
  attachments: AttachmentFile[];
  errors: string[];
}

type AcceptedExtension = (typeof ACCEPTED_EXTENSIONS)[number];

function createId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function fileExtension(name: string): string {
  const index = name.lastIndexOf(".");
  return index <= 0 ? "" : name.slice(index).toLowerCase();
}

/** Human-readable size for chips and error messages. */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }

  if (bytes < 1024 * 1024) {
    return `${Math.round(bytes / 1024)} KB`;
  }

  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Validate a new selection against the files already attached. */
export function validateSelection(
  current: AttachmentFile[],
  incoming: File[],
): AttachmentSelection {
  const attachments = [...current];
  const names = new Set(attachments.map((item) => item.name.toLowerCase()));
  const errors: string[] = [];

  for (const file of incoming) {
    const label = `"${file.name}"`;
    const extension = fileExtension(file.name);

    if (!ACCEPTED_EXTENSIONS.includes(extension as AcceptedExtension)) {
      errors.push(
        `${label} is not a supported file type. Attach a ${ACCEPTED_FORMATS_LABEL} file.`,
      );
      continue;
    }

    if (file.size === 0) {
      errors.push(`${label} is empty. Attach a document that contains text.`);
      continue;
    }

    if (file.size > MAX_ATTACHMENT_BYTES) {
      errors.push(
        `${label} is ${formatFileSize(file.size)}. The limit is 10 MB per file.`,
      );
      continue;
    }

    if (names.has(file.name.toLowerCase())) {
      errors.push(`${label} is already attached.`);
      continue;
    }

    if (attachments.length >= MAX_ATTACHMENTS) {
      errors.push(
        `You can attach up to ${MAX_ATTACHMENTS} documents. Remove one before adding ${label}.`,
      );
      break;
    }

    names.add(file.name.toLowerCase());
    attachments.push({
      id: createId(),
      file,
      name: file.name,
      size: file.size,
      extension,
    });
  }

  return { attachments, errors };
}
