/**
 * Voice-first input with an always-visible text fallback (`activeVoiceAdapter.available`
 * is false in plenty of browsers/environments — this must never be the only way in).
 * Submitting either path calls the same `onSubmit(text)`; BoatApp decides what to do
 * with it (including refusing while offline).
 */
import { useState } from "react";
import { activeVoiceAdapter } from "@shared/voice";
import "./ask.css";

/** Inline mic glyph — replaces the old empty `<span>` that had a font-size but no
 * content, which is exactly why the label looked off-centre. */
function MicIcon() {
  return (
    <svg
      className="voice-input__mic-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <line x1="12" y1="19" x2="12" y2="23" />
      <line x1="8" y1="23" x2="16" y2="23" />
    </svg>
  );
}

export function VoiceInput({
  onSubmit,
  disabled,
  disabledReason,
}: {
  onSubmit: (text: string) => void;
  disabled?: boolean;
  disabledReason?: string;
}) {
  const [text, setText] = useState("");
  const [listening, setListening] = useState(false);
  const [voiceError, setVoiceError] = useState<string | null>(null);

  const micAvailable = activeVoiceAdapter.available;

  function handleMic() {
    if (disabled || listening) return;
    setVoiceError(null);
    setListening(true);
    activeVoiceAdapter
      // English-first for now — language selection returns when multilingual input is re-enabled.
      .listen("en-IN")
      .then((transcript) => {
        setListening(false);
        if (!transcript.trim()) return;
        setText(transcript);
        onSubmit(transcript.trim());
      })
      .catch((err: unknown) => {
        setListening(false);
        setVoiceError(err instanceof Error ? err.message : "Voice input failed");
      });
  }

  function handleCancel() {
    activeVoiceAdapter.cancel();
    setListening(false);
  }

  function handleTextSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSubmit(trimmed);
    setText("");
  }

  return (
    <div className="voice-input">
      {disabled && disabledReason ? <div className="voice-input__disabled-note">{disabledReason}</div> : null}
      <div className="voice-input__row">
        {micAvailable ? (
          <button
            type="button"
            className={`voice-input__mic${listening ? " voice-input__mic--listening" : ""}`}
            onClick={listening ? handleCancel : handleMic}
            disabled={disabled}
            aria-label={listening ? "Stop listening" : "Ask by voice"}
          >
            <MicIcon />
            <span className="voice-input__mic-label">{listening ? "Listening…" : "Speak"}</span>
          </button>
        ) : (
          <div className="voice-input__mic-unavailable">Voice input not supported in this browser — use text below.</div>
        )}
      </div>
      {voiceError ? <div className="voice-input__error">{voiceError}</div> : null}
      <form className="voice-input__text-row" onSubmit={handleTextSubmit}>
        <input
          type="text"
          className="voice-input__text"
          placeholder="Type your question — e.g. Is it safe to go out tomorrow morning?"
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={disabled}
        />
        <button type="submit" className="voice-input__send" disabled={disabled || !text.trim()}>
          Ask
        </button>
      </form>
    </div>
  );
}
