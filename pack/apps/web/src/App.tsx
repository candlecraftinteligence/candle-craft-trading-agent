import { useEffect, useRef } from "react";
import { Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { HomeScreen } from "./screens/HomeScreen";
import { MissionDetailScreen } from "./screens/MissionDetailScreen";
import { MissionsScreen } from "./screens/MissionsScreen";
import { ProfileScreen } from "./screens/ProfileScreen";
import { ReplayScreen } from "./screens/ReplayScreen";
import { telegramBridge } from "./telegram/TelegramBridge";

export function App() {
  return (
    <AppShell>
      <StartParamRedirect />
      <Routes>
        <Route path="/" element={<HomeScreen />} />
        <Route path="/missions" element={<MissionsScreen />} />
        <Route path="/missions/:id" element={<MissionDetailScreen />} />
        <Route path="/replay" element={<ReplayScreen />} />
        <Route path="/profile" element={<ProfileScreen />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AppShell>
  );
}

function StartParamRedirect() {
  const navigate = useNavigate();
  const handled = useRef(false);

  useEffect(() => {
    if (handled.current) return;
    const startParam = telegramBridge.snapshot.startParam;
    if (!startParam || !/^mock_setup_[a-z0-9_]+$/.test(startParam)) return;
    handled.current = true;
    navigate(`/missions/${startParam}`, { replace: true });
  }, [navigate]);

  return null;
}
