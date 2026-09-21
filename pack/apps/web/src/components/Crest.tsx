type CrestProps = {
  size?: number;
};

export function Crest({ size = 36 }: CrestProps) {
  return (
    <svg className="crest" width={size} height={size} viewBox="0 0 64 64" aria-hidden="true">
      <rect x="1.5" y="1.5" width="61" height="61" fill="#14181E" stroke="#2A3038" />
      <path
        d="M16 46V28L25 14h7l4 6 5-6h7l-3 10v22H16z"
        fill="none"
        stroke="#C7923E"
        strokeWidth="1.4"
      />
      <path d="M26 31h12" stroke="#E8EAED" strokeWidth="1.3" />
      <circle cx="31" cy="30" r="1.3" fill="#C7923E" />
    </svg>
  );
}
