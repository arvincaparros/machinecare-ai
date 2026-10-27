import { useCallback, useState } from "react";

import {
  validateSelection,
  type AttachmentFile,
  type AttachmentSelection,
} from "../lib/files";

const EMPTY: AttachmentSelection = { attachments: [], errors: [] };

/**
 * Owns the files selected in the composer and the validation messages.
 * Files stay in memory only; they are handed to useAsk on submit.
 */
export function useAttachments() {
  const [state, setState] = useState<AttachmentSelection>(EMPTY);

  const add = useCallback((files: File[]) => {
    if (files.length === 0) {
      return;
    }

    setState((current) => validateSelection(current.attachments, files));
  }, []);

  const remove = useCallback((id: string) => {
    setState((current) => ({
      attachments: current.attachments.filter((item) => item.id !== id),
      errors: [],
    }));
  }, []);

  const clear = useCallback(() => setState(EMPTY), []);

  const dismissErrors = useCallback(
    () => setState((current) => ({ ...current, errors: [] })),
    [],
  );

  return {
    attachments: state.attachments,
    errors: state.errors,
    add,
    remove,
    clear,
    dismissErrors,
  };
}

export type { AttachmentFile };
