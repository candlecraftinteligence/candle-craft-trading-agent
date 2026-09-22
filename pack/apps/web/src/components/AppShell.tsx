import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useEffect, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { BottomNav } from "./BottomNav";
import { Wordmark } from "./Wordmark";
import {
  showTrainingBadge,
  telegramBridge,
  type TelegramSnapshot,
} from "../telegram/TelegramBridge";

type AppShellProps = {
  children: ReactNode;
};

export function AppShell({ children }: AppShellProps) {
  const location = useLocation();
  const reducedPref = useReducedMotion();
  const snapshot = useTelegramSnapshot();
  const reduced = Boolean(reducedPref) || snapshot.performanceClass === "LOW";

  return (
    <div className="stage">
      <div className="shell">
        <header className="topbar">
          <div className="brand">
            <Wordmark />
          </div>
          {showTrainingBadge() ? (
            <span className="training-badge">MOCK / TRAINING</span>
          ) : null}
        </header>
        {snapshot.isDevFallback ? (
          <p className="dev-banner" role="status">
            <span>{snapshot.fallbackLabel}</span>
            <span className="dev-banner-sep">·</span>
            <span>Telegram is not connected</span>
          </p>
        ) : null}
        <main className="content">
          <AnimatePresence mode="wait">
            <motion.div
              key={location.pathname}
              initial={reduced ? false : { opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduced ? undefined : { opacity: 0, y: -6 }}
              transition={{ duration: reduced ? 0 : 0.22, ease: [0.22, 1, 0.36, 1] }}
            >
              {children}
            </motion.div>
          </AnimatePresence>
        </main>
        <BottomNav />
      </div>
    </div>
  );
}

function useTelegramSnapshot(): TelegramSnapshot {
  const [snapshot, setSnapshot] = useState(telegramBridge.snapshot);
  useEffect(() => telegramBridge.subscribe(setSnapshot), []);
  return snapshot;
}
