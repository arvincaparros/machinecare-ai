import { useEffect, useRef } from "react";

import { formatFileSize } from "../lib/files";
import type { AssistantTurn, AttachmentMeta, ChatTurn } from "../types";
import { AnswerCard } from "./AnswerCard";

interface ChatThreadProps {
  turns: ChatTurn[];
  pending: boolean;
  onRetry: (turn: AssistantTurn) => void;
}

function UserBubble({
  content,
  machine,
  attachments,
}: {
  content: string;
  machine: string | null;
  attachments: AttachmentMeta[];
}) {
  return (
    <div className="bubble">
      <span className="bubble__label">You</span>
      <p className="bubble__text">{content}</p>
      {attachments.length > 0 && (
        <ul className="bubble__files" aria-label="Attached documents">
          {attachments.map((attachment) => (
            <li className="bubble__file" key={attachment.name}>
              <span className="bubble__file-name">{attachment.name}</span>
              <span className="bubble__file-size">
                {formatFileSize(attachment.size)}
              </span>
            </li>
          ))}
        </ul>
      )}
      {machine && <span className="bubble__machine">Machine filter · {machine}</span>}
    </div>
  );
}

function LoadingBubble({ withDocuments }: { withDocuments: boolean }) {
  return (
    <div className="answer answer--loading" aria-live="polite">
      <span className="answer__badge">MachineCare AI</span>
      <div className="typing" aria-label="Searching incident records">
        <span />
        <span />
        <span />
        <span className="typing__text">
          {withDocuments
            ? "Reading documents and retrieving evidence…"
            : "Retrieving evidence…"}
        </span>
      </div>
    </div>
  );
}

function ErrorBubble({
  turn,
  pending,
  onRetry,
}: {
  turn: AssistantTurn;
  pending: boolean;
  onRetry: (turn: AssistantTurn) => void;
}) {
  return (
    <div className="answer answer--error" role="alert">
      <span className="answer__badge answer__badge--error">Request failed</span>
      <p className="error__message">{turn.message}</p>
      <button
        type="button"
        className="error__retry"
        onClick={() => onRetry(turn)}
        disabled={pending}
      >
        Try again
      </button>
    </div>
  );
}

export function ChatThread({ turns, pending, onRetry }: ChatThreadProps) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [turns]);

  if (turns.length === 0) {
    return (
      <div className="thread thread--empty">
        <div className="empty">
          <span className="empty__icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none">
              <path
                d="M21 12a9 9 0 1 1-3.2-6.9"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
              />
              <path
                d="M21 4v5h-5"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
              <path
                d="M8.5 11.5h7M8.5 15h4.5"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
              />
            </svg>
          </span>
          <h3>Ask about a downtime incident</h3>
          <p>
            Describe the symptom, the machine, or the fault you are seeing. The
            answer will cite the closed records it used — pick a suggestion above
            to start.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="thread" aria-live="polite" aria-busy={pending}>
      {turns.map((turn) => {
        if (turn.role === "user") {
          return (
            <UserBubble
              key={turn.id}
              content={turn.content}
              machine={turn.machine}
              attachments={turn.attachments}
            />
          );
        }

        if (turn.state === "pending") {
          return (
            <LoadingBubble
              key={turn.id}
              withDocuments={(turn.files ?? []).length > 0}
            />
          );
        }

        if (turn.state === "error") {
          return (
            <ErrorBubble
              key={turn.id}
              turn={turn}
              pending={pending}
              onRetry={onRetry}
            />
          );
        }

        return turn.response ? (
          <AnswerCard key={turn.id} response={turn.response} />
        ) : null;
      })}

      <div ref={endRef} />
    </div>
  );
}
