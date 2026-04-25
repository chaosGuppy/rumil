"use client";

import { useEffect } from "react";

/**
 * Wire keyboard shortcuts to the verdict buttons on the judge page.
 *
 * `1` / `a` / `ArrowLeft`  -> A is better
 * `2` / `b` / `ArrowRight` -> B is better
 * `3` / `t` / `=`           -> Tie
 *
 * Shortcuts are suppressed when focus is inside the note textarea (or any
 * input/textarea/contenteditable), so typing notes still works. We look up
 * the buttons by `name=verdict` + `value` within the component's enclosing
 * form so we don't depend on DOM ordering.
 */
export function JudgeKeyboardShortcuts() {
  useEffect(() => {
    function inTypingField(target: EventTarget | null): boolean {
      if (!(target instanceof HTMLElement)) return false;
      const tag = target.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
      if (target.isContentEditable) return true;
      return false;
    }

    function clickVerdict(value: "A" | "B" | "tie") {
      const btn = document.querySelector<HTMLButtonElement>(
        `button[name="verdict"][value="${value}"]`,
      );
      btn?.click();
    }

    function onKeyDown(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (inTypingField(e.target)) return;
      const k = e.key.toLowerCase();
      if (k === "1" || k === "a" || k === "arrowleft") {
        e.preventDefault();
        clickVerdict("A");
      } else if (k === "2" || k === "b" || k === "arrowright") {
        e.preventDefault();
        clickVerdict("B");
      } else if (k === "3" || k === "t" || k === "=") {
        e.preventDefault();
        clickVerdict("tie");
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return null;
}
