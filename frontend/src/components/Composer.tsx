import { useRef } from "react";
import type { RefObject } from "react";

import { formatFileSize, type AttachmentFile } from "../lib/files";
import {
  ACCEPTED_EXTENSIONS,
  ACCEPTED_FORMATS_LABEL,
  MAX_ATTACHMENTS,
  MAX_MACHINE_LENGTH,
  MAX_QUESTION_LENGTH,
} from "../types";

interface ComposerProps {
  value: string;
  onChange: (value: string) => void;
  machine: string;
  onMachineChange: (value: string) => void;
  attachments: AttachmentFile[];
  onFilesSelected: (files: File[]) => void;
  onRemoveAttachment: (id: string) => void;
  attachmentErrors: string[];
  onDismissAttachmentErrors: () => void;
  onSubmit: () => void;
  pending: boolean;
  inputRef: RefObject<HTMLTextAreaElement | null>;
}

const ACCEPT_ATTRIBUTE = ACCEPTED_EXTENSIONS.join(",");

export function Composer({
  value,
  onChange,
  machine,
  onMachineChange,
  attachments,
  onFilesSelected,
  onRemoveAttachment,
  attachmentErrors,
  onDismissAttachmentErrors,
  onSubmit,
  pending,
  inputRef,
}: ComposerProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);

  const canSubmit = value.trim().length > 0 && !pending;
  const showCounter = value.length > 1700;
  const limitReached = attachments.length >= MAX_ATTACHMENTS;
  const attachDisabled = pending || limitReached;

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        if (canSubmit) {
          onSubmit();
        }
      }}
    >
      <label className="composer__question">
        <span className="sr-only">Your question</span>
        <textarea
          ref={inputRef}
          className="composer__textarea"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              if (canSubmit) {
                onSubmit();
              }
            }
          }}
          placeholder="Describe the fault or ask about a past incident…"
          rows={2}
          maxLength={MAX_QUESTION_LENGTH}
          disabled={pending}
        />
      </label>

      {attachments.length > 0 && (
        <ul className="attach-list" aria-label="Attached documents">
          {attachments.map((attachment) => (
            <li className="attach-chip" key={attachment.id}>
              <span className="attach-chip__icon" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none">
                  <path
                    d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinejoin="round"
                  />
                  <path
                    d="M14 3v5h5"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinejoin="round"
                  />
                </svg>
              </span>
              <span className="attach-chip__name" title={attachment.name}>
                {attachment.name}
              </span>
              <span className="attach-chip__size">
                {formatFileSize(attachment.size)}
              </span>
              <button
                type="button"
                className="attach-chip__remove"
                onClick={() => onRemoveAttachment(attachment.id)}
                disabled={pending}
                aria-label={`Remove ${attachment.name}`}
                title={`Remove ${attachment.name}`}
              >
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path
                    d="M7 7l10 10M17 7 7 17"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                  />
                </svg>
              </button>
            </li>
          ))}
        </ul>
      )}

      {attachmentErrors.length > 0 && (
        <div className="composer__file-error" role="alert">
          <div>
            {attachmentErrors.map((error) => (
              <p key={error}>{error}</p>
            ))}
          </div>
          <button
            type="button"
            className="composer__file-error-dismiss"
            onClick={onDismissAttachmentErrors}
            aria-label="Dismiss attachment errors"
          >
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path
                d="M7 7l10 10M17 7 7 17"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
              />
            </svg>
          </button>
        </div>
      )}

      <div className="composer__row">
        <input
          ref={fileInputRef}
          className="sr-only"
          type="file"
          multiple
          accept={ACCEPT_ATTRIBUTE}
          disabled={pending}
          tabIndex={-1}
          aria-hidden="true"
          onChange={(event) => {
            const files = Array.from(event.target.files ?? []);
            // Reset so selecting the same file again still fires onChange.
            event.target.value = "";
            onFilesSelected(files);
          }}
        />

        <button
          type="button"
          className="composer__attach"
          onClick={() => fileInputRef.current?.click()}
          disabled={attachDisabled}
          title={
            limitReached
              ? `Up to ${MAX_ATTACHMENTS} documents per question`
              : `Attach ${ACCEPTED_FORMATS_LABEL} · max 10 MB each · up to ${MAX_ATTACHMENTS} files`
          }
        >
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path
              d="M20 11.5 12 19.5a4.6 4.6 0 0 1-6.5-6.5l7.7-7.7a3.1 3.1 0 0 1 4.4 4.4l-7.7 7.7a1.6 1.6 0 0 1-2.2-2.2l7-7"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          <span>Attach</span>
          {attachments.length > 0 && (
            <span className="composer__attach-count">
              {attachments.length}/{MAX_ATTACHMENTS}
            </span>
          )}
        </button>

        <label className="composer__machine">
          <span className="composer__machine-label">Machine</span>
          <input
            className="composer__machine-input"
            value={machine}
            onChange={(event) => onMachineChange(event.target.value)}
            placeholder="AOI-01"
            maxLength={MAX_MACHINE_LENGTH}
            disabled={pending}
            spellCheck={false}
          />
        </label>

        <div className="composer__meta">
          {showCounter && (
            <span className="composer__counter">
              {value.length}/{MAX_QUESTION_LENGTH}
            </span>
          )}
          <span className="composer__hint">Enter to ask · Shift + Enter for a new line</span>
        </div>

        <button type="submit" className="ask" disabled={!canSubmit}>
          {pending ? (
            <>
              <span className="ask__spinner" aria-hidden="true" />
              Analyzing
            </>
          ) : (
            <>
              Ask
              <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M5 12h13M13 6.5 18.5 12 13 17.5"
                  stroke="currentColor"
                  strokeWidth="1.9"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </>
          )}
        </button>
      </div>
    </form>
  );
}
