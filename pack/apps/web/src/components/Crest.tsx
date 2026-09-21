type CrestProps = {
  size?: number;
};

export function Crest({ size = 36 }: CrestProps) {
  return (
    <svg className="crest" width={size} height={size} viewBox="0 0 64 64" aria-hidden="true">
      <circle cx="32" cy="32" r="30.5" fill="#0c0e12" stroke="#3a2e22" strokeWidth="1" />
      <circle cx="32" cy="32" r="26.5" fill="none" stroke="#E08A2C" strokeOpacity="0.55" strokeWidth="0.8" />
      <circle
        cx="32"
        cy="32"
        r="22.5"
        fill="none"
        stroke="#E08A2C"
        strokeOpacity="0.28"
        strokeWidth="0.6"
        strokeDasharray="1.4 2.4"
      />
      <path d="M32 4.8v3.2M32 56v3.2M4.8 32h3.2M56 32h3.2" stroke="#E08A2C" strokeOpacity="0.7" strokeWidth="0.9" />
      <path d="M18 24.5 14.5 13.5 25 22.5Z" fill="#b7c0ca" />
      <path d="M25 23.5 27 12.5 33.5 23Z" fill="#e4e8ed" />
      <path
        d="M16.5 36.5c.4-7 6.2-12.5 12.2-12.2 2.2-4.2 7.4-5.6 12.2-2.2 4.6 2.2 8.6 6.4 9.4 9.2 1.2 2.4-.2 4.6-3.4 5.2-2.2 2.8-4.6 3.4-7.2 5.6-3.2 4.6-9.6 7.2-15.4 5.2-6.2-2.2-8.2-6.4-7.8-10.8Z"
        fill="#d5dbe3"
      />
      <path d="M40.5 32.2c3.6.6 8.2 2.2 8.6 4.4-2.6 1.6-6.4.8-9.2-.6-.8-1.4-.4-2.8.6-3.8Z" fill="#7f8b98" />
      <path d="M47.2 35.2h3.2" stroke="#1a140f" strokeWidth="1.4" strokeLinecap="round" />
      <circle cx="31.2" cy="31.2" r="1.35" fill="#E08A2C" />
      <path d="M22 40.5c2.4 2.2 6.2 2.6 9.2 1.4" fill="none" stroke="#8e98a3" strokeWidth="0.7" />
    </svg>
  );
}
