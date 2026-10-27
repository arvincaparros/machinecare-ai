import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  askQuestion,
  askQuestionWithFiles,
} from "../api/client";
import type { AssistantTurn, ChatTurn } from "../types";

function createId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function messageFor(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  return "Something went wrong while contacting the RAG API.";
}

interface UseAskOptions {
  /** Called when a request fails because the API is unreachable. */
  onNetworkFailure?: () => void;
}

/**
 * Owns the conversation transcript and the single in-flight request.
 * One question is allowed at a time; retry re-runs the failed turn in place.
 */
export function useAsk(options: UseAskOptions = {}) {
  const { onNetworkFailure } = options;
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [isPending, setIsPending] = useState(false);
  const mounted = useRef(true);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      controller.current?.abort();
    };
  }, []);

  const run = useCallback(
    async (
      assistantId: string,
      question: string,
      machine: string | null,
      files: File[],
    ) => {
      controller.current?.abort();
      const current = new AbortController();
      controller.current = current;

      setIsPending(true);
      setTurns((previous) =>
        previous.map((turn) =>
          turn.id === assistantId && turn.role === "assistant"
            ? { ...turn, state: "pending", message: undefined, response: undefined }
            : turn,
        ),
      );

      try {
        const response =
          files.length > 0
            ? await askQuestionWithFiles(
                { question, machine },
                files,
                current.signal,
              )
            : await askQuestion({ question, machine }, current.signal);

        if (!mounted.current || current.signal.aborted) {
          return;
        }

        setTurns((previous) =>
          previous.map((turn) =>
            turn.id === assistantId && turn.role === "assistant"
              ? { ...turn, state: "done", response }
              : turn,
          ),
        );
      } catch (error) {
        if (!mounted.current || current.signal.aborted) {
          return;
        }

        if (error instanceof ApiError && error.kind === "network") {
          onNetworkFailure?.();
        }

        setTurns((previous) =>
          previous.map((turn) =>
            turn.id === assistantId && turn.role === "assistant"
              ? { ...turn, state: "error", message: messageFor(error) }
              : turn,
          ),
        );
      } finally {
        if (mounted.current) {
          setIsPending(false);
        }
      }
    },
    [onNetworkFailure],
  );

  const ask = useCallback(
    (question: string, machine: string | null, files: File[] = []) => {
      const trimmed = question.trim();

      if (!trimmed || isPending) {
        return;
      }

      const assistantId = createId();

      setTurns((previous) => [
        ...previous,
        {
          id: createId(),
          role: "user",
          content: trimmed,
          machine,
          attachments: files.map((file) => ({
            name: file.name,
            size: file.size,
          })),
        },
        {
          id: assistantId,
          role: "assistant",
          state: "pending",
          question: trimmed,
          machine,
          files,
        },
      ]);

      void run(assistantId, trimmed, machine, files);
    },
    [isPending, run],
  );

  const retry = useCallback(
    (turn: AssistantTurn) => {
      if (isPending || turn.state !== "error") {
        return;
      }
      void run(turn.id, turn.question, turn.machine, turn.files ?? []);
    },
    [isPending, run],
  );

  return { turns, isPending, ask, retry };
}
