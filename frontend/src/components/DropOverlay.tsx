import {
  ACCEPTED_FORMATS_LABEL,
  MAX_ATTACHMENTS,
  MAX_ATTACHMENT_BYTES,
} from "../types";

const MAX_FILE_MB = MAX_ATTACHMENT_BYTES / (1024 * 1024);

/**
 * Drop hint shown while files hover the chat panel. It never intercepts
 * pointer or drag events (CSS: pointer-events none), so the panel keeps
 * receiving them, and it is hidden from assistive tech because the same
 * limits and formats are announced by the composer's Attach button.
 */
export function DropOverlay() {
  return (
    <div className="drop-zone" aria-hidden="true">
      <div className="drop-zone__card">
        <span className="drop-zone__icon">
          <svg viewBox="0 0 24 24" fill="none">
            <path
              d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5"
              stroke="currentColor"
              strokeWidth="1.7"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <path
              d="M4 15v3.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V15"
              stroke="currentColor"
              strokeWidth="1.7"
              strokeLinecap="round"
            />
          </svg>
        </span>
        <p className="drop-zone__title">Drop files to attach</p>
        <p className="drop-zone__hint">
          {ACCEPTED_FORMATS_LABEL} · up to {MAX_ATTACHMENTS} files ·{" "}
          {MAX_FILE_MB} MB each
        </p>
      </div>
    </div>
  );
}
