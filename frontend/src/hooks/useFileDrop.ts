import { useCallback, useEffect, useRef, useState } from "react";
import type { DragEvent as ReactDragEvent } from "react";

/**
 * Drag-and-drop wiring for the chat panel.
 *
 * Files dragged from the operating system are handed to the caller's callback,
 * which is the same handler the Attach button uses, so validation, duplicate
 * detection and the three-file limit stay in one place. Nothing here submits a
 * question, and the drop never opens the file in the browser.
 */

function carriesFiles(event: ReactDragEvent<HTMLElement>): boolean {
  const types = event.dataTransfer?.types;

  if (!types) {
    return false;
  }

  return Array.from(types).includes("Files");
}

interface UseFileDropOptions {
  /** Drops are ignored while an answer is in flight, like the Attach button. */
  enabled?: boolean;
}

export interface FileDropHandlers {
  onDragEnter: (event: ReactDragEvent<HTMLElement>) => void;
  onDragOver: (event: ReactDragEvent<HTMLElement>) => void;
  onDragLeave: (event: ReactDragEvent<HTMLElement>) => void;
  onDrop: (event: ReactDragEvent<HTMLElement>) => void;
}

export interface FileDropZone {
  /** True while files hover the zone, so the overlay can be shown. */
  isDragging: boolean;
  dragHandlers: FileDropHandlers;
}

export function useFileDrop(
  onFiles: (files: File[]) => void,
  { enabled = true }: UseFileDropOptions = {},
): FileDropZone {
  const [isDragging, setIsDragging] = useState(false);
  // Nested children fire enter/leave pairs; the element the drag moves to
  // decides whether the overlay should stay, so it never flickers or sticks.
  const hideFrame = useRef<number | null>(null);

  const cancelHide = useCallback(() => {
    if (hideFrame.current !== null) {
      window.cancelAnimationFrame(hideFrame.current);
      hideFrame.current = null;
    }
  }, []);

  const hide = useCallback(() => {
    cancelHide();
    setIsDragging(false);
  }, [cancelHide]);

  const onDragEnter = useCallback(
    (event: ReactDragEvent<HTMLElement>) => {
      if (!enabled || !carriesFiles(event)) {
        return;
      }

      // Any enter inside the zone (bubbled from a child or not) shows it again.
      cancelHide();
      event.preventDefault();
      setIsDragging(true);
    },
    [cancelHide, enabled],
  );

  const onDragOver = useCallback(
    (event: ReactDragEvent<HTMLElement>) => {
      if (!enabled) {
        return;
      }

      // Once a file drag has entered, keep accepting it even if the browser
      // hides the item types during the drag-over phase.
      if (!carriesFiles(event) && !isDragging) {
        return;
      }

      // Required: without this the browser refuses the drop and opens the file.
      event.preventDefault();

      if (event.dataTransfer) {
        event.dataTransfer.dropEffect = "copy";
      }
    },
    [enabled, isDragging],
  );

  const onDragLeave = useCallback(
    (event: ReactDragEvent<HTMLElement>) => {
      const next = event.relatedTarget;

      if (next instanceof Node) {
        // Moving between nested children: still inside the zone.
        if (event.currentTarget.contains(next)) {
          return;
        }

        hide();
        return;
      }

      // No target means the drag may have left the window: hide, but keep one
      // frame of grace so an immediate enter for a child can cancel it.
      cancelHide();
      hideFrame.current = window.requestAnimationFrame(() => {
        hideFrame.current = null;
        setIsDragging(false);
      });
    },
    [cancelHide, hide],
  );

  const onDrop = useCallback(
    (event: ReactDragEvent<HTMLElement>) => {
      // Cancel the default on every file drop, even when it is ignored: the
      // browser must never navigate away to the dropped document.
      if (carriesFiles(event)) {
        event.preventDefault();
      }

      hide();

      if (!enabled) {
        return;
      }

      const dropped = Array.from(event.dataTransfer?.files ?? []);

      if (dropped.length > 0) {
        onFiles(dropped);
      }
    },
    [enabled, hide, onFiles],
  );

  // A drop or a cancelled drag outside the zone never reaches the handlers
  // above, so the overlay would stay up: clear it from the window instead.
  useEffect(() => {
    if (!isDragging) {
      return;
    }

    window.addEventListener("dragend", hide);
    window.addEventListener("drop", hide);

    return () => {
      window.removeEventListener("dragend", hide);
      window.removeEventListener("drop", hide);
    };
  }, [hide, isDragging]);

  // Never leave a frame pending after unmount.
  useEffect(() => cancelHide, [cancelHide]);

  // Drops are disabled while an answer is in flight: no overlay may outlive it.
  useEffect(() => {
    if (!enabled) {
      hide();
    }
  }, [enabled, hide]);

  // Dropping outside the chat panel must not open the file in a new page.
  useEffect(() => {
    const cancelPageDrop = (event: globalThis.DragEvent) => {
      const types = event.dataTransfer?.types;

      if (types && Array.from(types).includes("Files")) {
        event.preventDefault();
      }
    };

    window.addEventListener("dragover", cancelPageDrop);
    window.addEventListener("drop", cancelPageDrop);

    return () => {
      window.removeEventListener("dragover", cancelPageDrop);
      window.removeEventListener("drop", cancelPageDrop);
    };
  }, []);

  return {
    isDragging,
    dragHandlers: { onDragEnter, onDragOver, onDragLeave, onDrop },
  };
}
