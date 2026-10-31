import type { Mission } from "../api/types";

type IllustrativeChartProps = {
  mission: Mission;
  compact?: boolean;
};

const BARS = [18, 28, 22, 36, 30, 44, 26, 48, 34, 52, 40, 58];

export function evidenceMentionsSweep(mission: Pick<Mission, "title" | "thesis_summary" | "evidence">): boolean {
  const blob = [mission.title, mission.thesis_summary, ...mission.evidence.map((block) => `${block.type} ${block.label} ${block.detail ?? ""}`)]
    .join(" ")
    .toLowerCase();
  return blob.includes("sweep") || blob.includes("liquidity");
}

export function IllustrativeChart({ mission, compact = false }: IllustrativeChartProps) {
  const sweep = evidenceMentionsSweep(mission);
  const seed = mission.cci_setup_id.length + mission.symbol.length;
  return (
    <figure className={compact ? "chart chart-compact" : "chart"} aria-label="Illustrative structure">
      <svg viewBox="0 0 280 120" role="img">
        <title>Illustrative structure. Not a live chart.</title>
        {BARS.map((height, index) => {
          const h = height + ((seed + index * 3) % 12);
          const x = 12 + index * 22;
          const up = (index + seed) % 3 !== 0;
          const y = 100 - h;
          return (
            <g key={index}>
              <line x1={x + 5} y1={y - 6} x2={x + 5} y2={y + h + 6} stroke={up ? "#e08a2c" : "#8d5a22"} strokeWidth="1" />
              <rect x={x} y={y} width="10" height={h} rx="1" fill={up ? "#e08a2c" : "#3a2414"} stroke="#ffb25a" strokeOpacity="0.35" />
            </g>
          );
        })}
        {sweep ? (
          <g>
            <path d="M18 78 H250" stroke="#ffb25a" strokeDasharray="3 3" strokeOpacity="0.7" />
            <text x="148" y="72" fill="#ffb25a" fontSize="9" fontFamily="IBM Plex Mono, monospace">
              LIQUIDITY SWEEP
            </text>
          </g>
        ) : null}
      </svg>
      <figcaption className="fine">Illustrative structure. Not a live chart.</figcaption>
    </figure>
  );
}
