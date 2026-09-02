/**
 * Voice adapter interface — PLAN.md Phase 5: "Build the interface first, ship Web
 * Speech, add Bhashini when the key lands. Say 'Bhashini' on the deck only once it is
 * actually wired." `WebSpeechVoiceAdapter` is the only one wired for real right now;
 * `BhashiniVoiceAdapter` is a stub that throws until CLAUDE.md's open unknown #1 (IMD/
 * Bhashini registration) resolves — swap `activeVoiceAdapter` below when it does.
 */

export interface VoiceAdapter {
  readonly id: "web-speech" | "bhashini";
  readonly available: boolean;
  /** Resolves with the recognized transcript, or rejects/throws on failure. */
  listen(lang: string): Promise<string>;
  speak(text: string, lang: string): Promise<void>;
  cancel(): void;
}

// -- Web Speech (fast path) -----------------------------------------------------------

type SpeechRecognitionCtor = new () => SpeechRecognition;

function getSpeechRecognitionCtor(): SpeechRecognitionCtor | null {
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export class WebSpeechVoiceAdapter implements VoiceAdapter {
  readonly id = "web-speech" as const;
  private recognition: SpeechRecognition | null = null;

  get available(): boolean {
    return getSpeechRecognitionCtor() !== null && "speechSynthesis" in window;
  }

  listen(lang: string): Promise<string> {
    const Ctor = getSpeechRecognitionCtor();
    if (!Ctor) return Promise.reject(new Error("SpeechRecognition unavailable in this browser"));
    return new Promise((resolve, reject) => {
      const recognition = new Ctor();
      this.recognition = recognition;
      recognition.lang = lang; // e.g. "ta-IN"
      recognition.interimResults = false;
      recognition.maxAlternatives = 1;
      recognition.onresult = (event) => {
        const transcript = event.results[0]?.[0]?.transcript ?? "";
        resolve(transcript);
      };
      recognition.onerror = (event) => reject(new Error(`speech recognition error: ${event.error}`));
      recognition.onend = () => {
        this.recognition = null;
      };
      recognition.start();
    });
  }

  speak(text: string, lang: string): Promise<void> {
    return new Promise((resolve, reject) => {
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = lang;
      utterance.onend = () => resolve();
      utterance.onerror = (event) => reject(new Error(`speech synthesis error: ${event.error}`));
      window.speechSynthesis.speak(utterance);
    });
  }

  cancel(): void {
    this.recognition?.stop();
    window.speechSynthesis.cancel();
  }
}

// -- Bhashini (credible path — stubbed until the ULCA key lands) ----------------------

export class BhashiniVoiceAdapter implements VoiceAdapter {
  readonly id = "bhashini" as const;
  readonly available = false; // flip once dhruva-api.bhashini.gov.in is wired

  listen(): Promise<string> {
    return Promise.reject(new Error("Bhashini adapter not yet wired — see CLAUDE.md open unknowns"));
  }
  speak(): Promise<void> {
    return Promise.reject(new Error("Bhashini adapter not yet wired — see CLAUDE.md open unknowns"));
  }
  cancel(): void {}
}

export const webSpeechAdapter = new WebSpeechVoiceAdapter();
export const bhashiniAdapter = new BhashiniVoiceAdapter();

/** The adapter routes/boat actually uses. Swap to `bhashiniAdapter` once it is real. */
export const activeVoiceAdapter: VoiceAdapter = webSpeechAdapter;
