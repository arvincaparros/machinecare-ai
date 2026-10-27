import { useCallback, useMemo, useRef, useState } from "react";

import { ChatThread } from "./components/ChatThread";
import { Composer } from "./components/Composer";
import { DropOverlay } from "./components/DropOverlay";
import { Header } from "./components/Header";
import { Hero } from "./components/Hero";
import { ImportDialog } from "./components/ImportDialog";
import { SourcesPanel } from "./components/SourcesPanel";
import { useApiHealth } from "./hooks/useApiHealth";
import { useAsk } from "./hooks/useAsk";
import { useAttachments } from "./hooks/useAttachments";
import { useFileDrop } from "./hooks/useFileDrop";

export default function App() {
  const health = useApiHealth();
  const chat = useAsk({ onNetworkFailure: health.markOffline });
  const selection = useAttachments();
  // Dropped documents go through the Attach button's validation, and are
  // ignored while an answer is in flight, exactly like the button itself.
  const dropZone = useFileDrop(selection.add, { enabled: !chat.isPending });

  const [draft, setDraft] = useState("");
  const [machine, setMachine] = useState("");
  // The import dialog is mounted only while open, so closing it discards the
  // selected file, the preview and the result instead of leaving them behind.
  const [importOpen, setImportOpen] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // The transcript is the source of truth for the hero: as soon as a turn
  // exists a conversation has started, and the suggested prompts retire for
  // the rest of it. Typing or attaching files alone does not start one.
  const hasStartedConversation = chat.turns.length > 0;

  // The rail mirrors the most recent completed answer.
  const latestAnswer = useMemo(() => {
    for (let index = chat.turns.length - 1; index >= 0; index -= 1) {
      const turn = chat.turns[index];

      if (turn.role === "assistant" && turn.state === "done" && turn.response) {
        return turn.response;
      }
    }

    return null;
  }, [chat.turns]);

  const submitQuestion = useCallback(
    (question: string) => {
      const trimmed = question.trim();

      if (!trimmed || chat.isPending) {
        return;
      }

      chat.ask(
        trimmed,
        machine.trim() || null,
        selection.attachments.map((attachment) => attachment.file),
      );
      setDraft("");
      selection.clear();
    },
    [chat, machine, selection],
  );

  // A suggested prompt is a real submission, not a draft; the composer keeps
  // focus so follow-up questions are one keystroke away.
  const handlePick = useCallback(
    (question: string) => {
      submitQuestion(question);
      inputRef.current?.focus();
    },
    [submitQuestion],
  );

  const handleSubmit = useCallback(() => {
    submitQuestion(draft);
  }, [draft, submitQuestion]);

  return (
    <>
      <div className="shell">
        <div className="shell__glow" aria-hidden="true" />

        <Header
          status={health.status}
          onOpenImport={() => setImportOpen(true)}
        />

        <Hero
          compact={hasStartedConversation}
          showSuggestions={!hasStartedConversation}
          onPick={handlePick}
        />

        <main className="workspace">
          <section
            className="panel"
            aria-labelledby="panel-title"
            {...dropZone.dragHandlers}
          >
            <div className="panel__head">
              <div>
                <h2 id="panel-title">Incident assistant</h2>
                <p>Closed downtime records · every answer cites its evidence</p>
              </div>
              <span className="panel__badge" aria-hidden="true">
                RAG · grounded
              </span>
            </div>

            <ChatThread
              turns={chat.turns}
              pending={chat.isPending}
              onRetry={chat.retry}
            />

            <Composer
              value={draft}
              onChange={setDraft}
              machine={machine}
              onMachineChange={setMachine}
              attachments={selection.attachments}
              onFilesSelected={selection.add}
              onRemoveAttachment={selection.remove}
              attachmentErrors={selection.errors}
              onDismissAttachmentErrors={selection.dismissErrors}
              onSubmit={handleSubmit}
              pending={chat.isPending}
              inputRef={inputRef}
            />

            {dropZone.isDragging && <DropOverlay />}
          </section>

          <aside className="rail" aria-label="Sources">
            <SourcesPanel
              sources={latestAnswer?.sources ?? []}
              citedIds={latestAnswer?.cited_incident_ids ?? []}
              idle={latestAnswer === null}
            />
          </aside>
        </main>

        <footer className="footer">
          <span>MachineCare AI · retrieval over historical downtime records</span>
          <span>Recommendations are suggestions, not approved procedures.</span>
        </footer>
      </div>

      {/* Outside the shell: the shell is a clipped grid, and the dialog is
          positioned against the viewport. */}
      {importOpen && <ImportDialog onClose={() => setImportOpen(false)} />}
    </>
  );
}
