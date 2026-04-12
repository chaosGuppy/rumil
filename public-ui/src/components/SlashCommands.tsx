"use client";

import { useState, useEffect, useCallback, useRef } from "react";

export interface SlashCommand {
  name: string;
  description: string;
  args?: string;
  example?: string;
}

export const COMMANDS: SlashCommand[] = [
  {
    name: "search",
    description: "Search the workspace for relevant research",
    args: "<query>",
    example: "/search compute governance effectiveness",
  },
  {
    name: "inspect",
    description: "Look up a specific page by its short ID",
    args: "<page_id>",
    example: "/inspect f8a1b2c3",
  },
  {
    name: "ask",
    description: "Add a new research question to the workspace",
    args: "<question>",
    example: "/ask Are export controls sustainable long-term?",
  },
  {
    name: "dispatch",
    description: "Fire a research call (costs money, takes time)",
    args: "<call_type> <question_id>",
    example: "/dispatch find-considerations f8a1b2c3",
  },
  {
    name: "investigate",
    description: "Run the full orchestrator on a question",
    args: "<question_id> [--budget N]",
    example: "/investigate f8a1b2c3 --budget 5",
  },
  {
    name: "ingest",
    description: "Ingest a URL as a source for a question",
    args: "<url> --for <question_id>",
    example: "/ingest https://arxiv.org/... --for f8a1b2c3",
  },
];

interface SlashCommandDropdownProps {
  input: string;
  cursorPosition: number;
  onSelect: (command: SlashCommand) => void;
  visible: boolean;
  onDismiss: () => void;
}

export function SlashCommandDropdown({
  input,
  onSelect,
  visible,
  onDismiss,
}: SlashCommandDropdownProps) {
  const [selectedIndex, setSelectedIndex] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);

  const slashPrefix = visible && input.startsWith("/")
    ? input.slice(1).split(" ")[0].toLowerCase()
    : "";

  const filtered = visible
    ? COMMANDS.filter((cmd) => cmd.name.startsWith(slashPrefix))
    : [];

  useEffect(() => {
    setSelectedIndex(0);
  }, [slashPrefix]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (!visible || filtered.length === 0) return;

      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex((i) => (i + 1) % filtered.length);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex((i) => (i - 1 + filtered.length) % filtered.length);
      } else if (e.key === "Tab") {
        e.preventDefault();
        onSelect(filtered[selectedIndex]);
      } else if (e.key === "Escape") {
        e.preventDefault();
        onDismiss();
      }
    },
    [visible, filtered, selectedIndex, onSelect, onDismiss],
  );

  useEffect(() => {
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  if (!visible || filtered.length === 0) return null;

  return (
    <div ref={containerRef} className="slash-dropdown">
      {filtered.map((cmd, i) => (
        <button
          key={cmd.name}
          className={`slash-item ${i === selectedIndex ? "slash-item-active" : ""}`}
          onClick={() => onSelect(cmd)}
          onMouseEnter={() => setSelectedIndex(i)}
        >
          <div className="slash-item-header">
            <span className="slash-item-name">/{cmd.name}</span>
            {cmd.args && <span className="slash-item-args">{cmd.args}</span>}
          </div>
          <div className="slash-item-desc">{cmd.description}</div>
        </button>
      ))}
      <div className="slash-hint">
        tab to complete · esc to dismiss
      </div>
    </div>
  );
}

export function useSlashCommands(
  input: string,
  setInput: (v: string) => void,
  textareaRef: React.RefObject<HTMLTextAreaElement | null>,
) {
  const [showDropdown, setShowDropdown] = useState(false);

  useEffect(() => {
    if (input === "/") {
      setShowDropdown(true);
    } else if (input.startsWith("/") && !input.includes(" ")) {
      setShowDropdown(true);
    } else {
      setShowDropdown(false);
    }
  }, [input]);

  const handleSelect = useCallback(
    (cmd: SlashCommand) => {
      setInput(`/${cmd.name} `);
      setShowDropdown(false);
      textareaRef.current?.focus();
    },
    [setInput, textareaRef],
  );

  const handleDismiss = useCallback(() => {
    setShowDropdown(false);
  }, []);

  return { showDropdown, handleSelect, handleDismiss };
}
