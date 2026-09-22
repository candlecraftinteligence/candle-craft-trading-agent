import { useId } from "react";

type WordmarkProps = {
  size?: "md" | "lg" | "xl";
  tone?: "glow" | "paper";
};

export function Wordmark({ size = "md", tone = "glow" }: WordmarkProps) {
  const metal = `cci-metal-${useId().replace(/:/g, "")}`;
  const glow = `cci-glow-${useId().replace(/:/g, "")}`;
  const cls = ["wordmark", size !== "md" ? `wordmark-${size}` : "", tone === "paper" ? "wordmark-paper" : ""]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={cls}>
      <svg className="wordmark-svg" viewBox="0 0 118 36" aria-hidden="true">
        <defs>
          <linearGradient id={metal} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#fff6e4" />
            <stop offset="45%" stopColor="#ffb25a" />
            <stop offset="100%" stopColor="#8a4b12" />
          </linearGradient>
          <filter id={glow} x="-30%" y="-50%" width="160%" height="200%">
            <feDropShadow dx="0" dy="0" stdDeviation="1.5" floodColor="#e08a2c" floodOpacity="0.95" />
          </filter>
        </defs>
        <g filter={tone === "glow" ? `url(#${glow})` : undefined} fill={`url(#${metal})`}>
          <text x="0" y="28" fontFamily="Manrope, sans-serif" fontSize="30" fontWeight="800">
            C
          </text>
          <text x="72" y="28" fontFamily="Manrope, sans-serif" fontSize="30" fontWeight="800">
            CI
          </text>
          <g transform="translate(31 4) scale(0.44)">
            <circle cx="32" cy="32" r="29" fill="#140e08" stroke="#ffb25a" strokeWidth="2.2" />
            <path d="M17 27 13 14 26 23Z" fill="#f7f4ee" />
            <path d="M25 24 28 12 36 24Z" fill="#fff" />
            <path
              d="M15 39c.8-9 8-15 15-14 2-5 9-6 14-2 5 3 9 8 10 11 1 3-1 5-4 6-2 3-5 4-8 6-4 5-11 8-17 5-7-2-11-7-10-12Z"
              fill="#e7edf3"
            />
            <circle cx="35" cy="31" r="2" fill="#e08a2c" />
            <path d="M46 35h5" stroke="#1a140f" strokeWidth="1.8" strokeLinecap="round" />
          </g>
        </g>
      </svg>
      <span className="wordmark-pack">The Pack</span>
    </div>
  );
}
