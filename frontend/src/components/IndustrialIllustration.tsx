/** Lightweight line-art illustration: inspection camera over a conveyor. */
export function IndustrialIllustration() {
  return (
    <svg
      className="illustration"
      viewBox="0 0 360 280"
      role="img"
      aria-label="Line drawing of an inspection camera above a conveyor line"
      fill="none"
    >
      <defs>
        <linearGradient id="mc-line" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#ffa04a" />
          <stop offset="100%" stopColor="#f2620a" stopOpacity="0.5" />
        </linearGradient>
        <radialGradient id="mc-glow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#ff7a18" stopOpacity="0.18" />
          <stop offset="100%" stopColor="#ff7a18" stopOpacity="0" />
        </radialGradient>
      </defs>

      <circle cx="180" cy="132" r="118" fill="url(#mc-glow)" />

      <g
        stroke="url(#mc-line)"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {/* conveyor */}
        <path d="M28 238h304" />
        <circle cx="64" cy="238" r="9" />
        <circle cx="124" cy="238" r="9" />
        <circle cx="184" cy="238" r="9" />
        <circle cx="244" cy="238" r="9" />

        {/* part on the belt */}
        <path d="M92 238v-30h38v30" />
        <path d="M105 208v30" />

        {/* camera head */}
        <path d="M146 96h68a10 10 0 0 1 10 10v28a10 10 0 0 1-10 10h-68a10 10 0 0 1-10-10v-28a10 10 0 0 1 10-10Z" />
        <circle cx="180" cy="120" r="13" />
        <circle cx="180" cy="120" r="5" />

        {/* mount */}
        <path d="M180 96V64h46" />
        <path d="M180 64V52" />

        {/* scan cone */}
        <path d="M166 144l-26 62M194 144l26 62" strokeDasharray="5 8" />

        {/* signal trace */}
        <path d="M26 62h30l10-20 12 40 10-20h34" />

        {/* gear */}
        <circle cx="300" cy="74" r="24" />
        <circle cx="300" cy="74" r="9" />
        <path d="M300 40v-9M300 117v9M334 74h9M257 74h-9M324 50l7-7M276 98l-7 7M324 98l7 7M276 50l-7-7" />
      </g>
    </svg>
  );
}
