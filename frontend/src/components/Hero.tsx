import { IndustrialIllustration } from "./IndustrialIllustration";

const SUGGESTIONS = [
  "AOI camera inspection is inaccurate",
  "Why did the conveyor belt slip during production?",
  "How was camera drift after extended operation resolved?",
  "What prevents repeat camera calibration failures?",
];

interface HeroProps {
  onPick: (question: string) => void;
  /** Collapses to a slim banner once a conversation has started. */
  compact: boolean;
  /** Suggested prompts only belong to the state before the first question. */
  showSuggestions: boolean;
}

export function Hero({ onPick, compact, showSuggestions }: HeroProps) {
  return (
    <section
      className={`hero${compact ? " hero--compact" : ""}${
        showSuggestions ? "" : " hero--no-suggestions"
      }`}
      aria-labelledby="hero-title"
    >
      <div className="hero__art" aria-hidden="true">
        <IndustrialIllustration />
      </div>

      <div className="hero__copy">
        <span className="eyebrow">Grounded maintenance intelligence</span>
        <h1 id="hero-title">
          Ask the downtime records. <span>Get evidence.</span>
        </h1>
        <p className="hero__lead">
          MachineCare AI searches your closed downtime incidents, then answers
          with the records that supported each claim. No invented procedures.
        </p>
      </div>

      {showSuggestions && (
        <div className="hero__suggestions" aria-label="Suggested questions">
          {SUGGESTIONS.map((question) => (
            <button
              key={question}
              type="button"
              className="chip"
              onClick={() => onPick(question)}
            >
              {question}
            </button>
          ))}
        </div>
      )}
    </section>
  );
}
