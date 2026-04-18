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
    name: "orchestrate",
    description: "Preview and run the orchestrator on a branch",
    args: "[branch_id] [--type explore|evaluate]",
    example: "/orchestrate f8a1b2c3 --type evaluate",
  },
  {
    name: "ingest",
    description: "Ingest a URL as a source for a question",
    args: "<url> --for <question_id>",
    example: "/ingest https://arxiv.org/... --for f8a1b2c3",
  },
  {
    name: "review",
    description: "Show pending suggestions in the review queue",
    example: "/review",
  },
  {
    name: "sonnet",
    description: "Switch to Sonnet 4.6 (faster, cheaper)",
    example: "/sonnet",
  },
  {
    name: "opus",
    description: "Switch to Opus 4.6 (most capable)",
    example: "/opus",
  },
  {
    name: "haiku",
    description: "Switch to Haiku 4.5 (fastest, cheapest)",
    example: "/haiku",
  },
];

const MODEL_COMMAND_NAMES = new Set(["sonnet", "opus", "haiku"]);

const RECENT_STORAGE_KEY = "parma.slash.recent";
const RECENT_MAX = 8;

// Most-recent-first queue of slash command names, persisted in localStorage
// so the ordering survives reloads. The dropdown pins these to the top of
// the filtered list; the full alphabetical list still renders below.
export function loadRecentCommands(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(RECENT_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((x): x is string => typeof x === "string").slice(0, RECENT_MAX);
  } catch {
    return [];
  }
}

export function recordRecentCommand(name: string): void {
  if (typeof window === "undefined") return;
  const normalized = name.replace(/^\//, "").toLowerCase();
  if (!normalized) return;
  try {
    const current = loadRecentCommands().filter((n) => n !== normalized);
    const next = [normalized, ...current].slice(0, RECENT_MAX);
    window.localStorage.setItem(RECENT_STORAGE_KEY, JSON.stringify(next));
    window.dispatchEvent(new CustomEvent("parma:slash-recent-updated"));
  } catch {
    /* localStorage may be unavailable; non-fatal */
  }
}

function useRecentCommands(): string[] {
  const [recent, setRecent] = useState<string[]>([]);
  useEffect(() => {
    setRecent(loadRecentCommands());
    function onUpdate() {
      setRecent(loadRecentCommands());
    }
    window.addEventListener("parma:slash-recent-updated", onUpdate);
    return () => window.removeEventListener("parma:slash-recent-updated", onUpdate);
  }, []);
  return recent;
}

interface SlashCommandDropdownProps {
  input: string;
  cursorPosition: number;
  onSelect: (command: SlashCommand) => void;
  visible: boolean;
  onDismiss: () => void;
  activeModel?: "sonnet" | "opus" | "haiku";
}

export function SlashCommandDropdown({
  input,
  onSelect,
  visible,
  onDismiss,
  activeModel,
}: SlashCommandDropdownProps) {
  const [selectedIndex, setSelectedIndex] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);

  const recent = useRecentCommands();

  const slashPrefix = visible && input.startsWith("/")
    ? input.slice(1).split(" ")[0].toLowerCase()
    : "";

  // Two-section ordering: recently-used commands (most-recent first), then
  // the remaining commands alphabetically. Both halves share the same
  // prefix filter so typing narrows the union.
  const { filtered, recentSet } = visible
    ? (() => {
        const prefix = slashPrefix;
        const matching = COMMANDS.filter((cmd) => cmd.name.startsWith(prefix));
        const recentOrdered: SlashCommand[] = [];
        const seen = new Set<string>();
        for (const name of recent) {
          const cmd = matching.find((c) => c.name === name);
          if (cmd && !seen.has(cmd.name)) {
            recentOrdered.push(cmd);
            seen.add(cmd.name);
          }
        }
        const rest = matching.filter((c) => !seen.has(c.name));
        return {
          filtered: [...recentOrdered, ...rest],
          recentSet: seen,
        };
      })()
    : { filtered: [] as SlashCommand[], recentSet: new Set<string>() };

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

  const recentCount = recentSet.size;
  const hasRecentSection = recentCount > 0 && recentCount < filtered.length;

  return (
    <div ref={containerRef} className="slash-dropdown">
      {hasRecentSection && (
        <div className="slash-section-label">Recently used</div>
      )}
      {filtered.map((cmd, i) => {
        const isActiveModel =
          MODEL_COMMAND_NAMES.has(cmd.name) && cmd.name === activeModel;
        const isRecent = recentSet.has(cmd.name);
        const showAllLabel = hasRecentSection && i === recentCount;
        return (
          <div key={cmd.name}>
            {showAllLabel && <div className="slash-section-label">All commands</div>}
            <button
              className={[
                "slash-item",
                i === selectedIndex ? "slash-item-active" : "",
                isActiveModel ? "slash-item-current" : "",
                isRecent ? "slash-item-recent" : "",
              ]
                .filter(Boolean)
                .join(" ")}
              onClick={() => onSelect(cmd)}
              onMouseEnter={() => setSelectedIndex(i)}
              aria-current={isActiveModel ? "true" : undefined}
            >
              <div className="slash-item-header">
                <span className="slash-item-name">/{cmd.name}</span>
                {cmd.args && <span className="slash-item-args">{cmd.args}</span>}
                {isActiveModel && (
                  <span className="slash-item-current-tag">(current)</span>
                )}
                {isRecent && !isActiveModel && (
                  <span className="slash-item-recent-tag">recent</span>
                )}
              </div>
              <div className="slash-item-desc">{cmd.description}</div>
            </button>
          </div>
        );
      })}
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
