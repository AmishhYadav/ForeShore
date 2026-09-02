/**
 * Voice-first input with an always-visible text fallback (`activeVoiceAdapter.available`
 * is false in plenty of browsers/environments — this must never be the only way in).
 * Submitting either path calls the same `onSubmit(text)`; BoatApp decides what to do
 * with it (including refusing while offline).
 */
import { useState } from "react";
import { activeVoiceAdapter } from "@shared/voice";

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
      .listen("ta-IN")
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
            <span className="voice-input__mic-icon" aria-hidden="true" />
            {listening ? "Listening…" : "Speak"}
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
