import { NavLink } from "react-router-dom";

const TABS = [
  { to: "/", label: "Home", end: true },
  { to: "/missions", label: "Missions", end: false },
  { to: "/replay", label: "Replay", end: true },
  { to: "/profile", label: "Profile", end: true },
] as const;

export function BottomNav() {
  return (
    <nav className="bottom-nav" aria-label="Primary">
      {TABS.map((tab) => (
        <NavLink
          key={tab.label}
          to={tab.to}
          end={tab.end}
          className="nav-tab"
          aria-label={tab.label}
        >
          <TabIcon label={tab.label} />
          {tab.label}
        </NavLink>
      ))}
    </nav>
  );
}

function TabIcon({ label }: { label: (typeof TABS)[number]["label"] }) {
  if (label === "Home") {
    return (
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
        <path d="M3 8.2 9 3l6 5.2V15H3V8.2Z" stroke="currentColor" strokeWidth="1.2" />
      </svg>
    );
  }
  if (label === "Missions") {
    return (
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
        <rect x="3" y="3.5" width="12" height="11" stroke="currentColor" strokeWidth="1.2" />
        <path d="M6 7.5h6M6 10.5h4" stroke="currentColor" strokeWidth="1.2" />
      </svg>
    );
  }
  if (label === "Replay") {
    return (
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
        <path d="M5 5.5v7l8-3.5-8-3.5Z" stroke="currentColor" strokeWidth="1.2" />
      </svg>
    );
  }
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
      <circle cx="9" cy="6.5" r="2.2" stroke="currentColor" strokeWidth="1.2" />
      <path d="M4.5 14.5c.7-2.2 2.2-3.3 4.5-3.3s3.8 1.1 4.5 3.3" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  );
}
