import { useCallback, useMemo, useRef, useState } from "react";

import { ChatThread } from "./components/ChatThread";
import { Composer } from "./components/Composer";
import { Header } from "./components/Header";
import { Hero } from "./components/Hero";
import { SourcesPanel } from "./components/SourcesPanel";
import { useApiHealth } from "./hooks/useApiHealth";
import { useAsk } from "./hooks/useAsk";
import { useAttachments } from "./hooks/useAttachments";

export default function App() {
  const health = useApiHealth();
  const chat = useAsk({ onNetworkFailure: health.markOffline });
  const selection = useAttachments();

  const [draft, setDraft] = useState("");
  const [machine, setMachine] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);

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

  const handlePick = useCallback((question: string) => {
    setDraft(question);
    inputRef.current?.focus();
    inputRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, []);

  const handleSubmit = useCallback(() => {
    const question = draft.trim();

    if (!question || chat.isPending) {
      return;
    }

    chat.ask(
      question,
      machine.trim() || null,
      selection.attachments.map((attachment) => attachment.file),
    );
    setDraft("");
    selection.clear();
  }, [chat, draft, machine, selection]);

  return (
    <div className="shell">
      <div className="shell__glow" aria-hidden="true" />

      <Header status={health.status} />

      <Hero compact={chat.turns.length > 0} onPick={handlePick} />

      <main className="workspace">
        <section className="panel" aria-labelledby="panel-title">
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
  );
}
