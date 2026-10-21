export type SafeInsets = {
  top: number;
  right: number;
  bottom: number;
  left: number;
};

export type PerformanceClass = "LOW" | "AVERAGE" | "HIGH" | "UNKNOWN";

export type TelegramSnapshot = {
  isTelegram: boolean;
  isDevFallback: boolean;
  platform: string;
  colorScheme: "dark" | "light";
  performanceClass: PerformanceClass;
  startParam: string | null;
  fallbackLabel: string;
};

type ThemeParams = Record<string, string | undefined>;

type TelegramBackButton = {
  show: () => void;
  hide: () => void;
  onClick: (handler: () => void) => void;
  offClick: (handler: () => void) => void;
};

type TelegramWebApp = {
  ready: () => void;
  expand?: () => void;
  initData?: string;
  platform?: string;
  colorScheme?: "dark" | "light";
  themeParams?: ThemeParams;
  initDataUnsafe?: { start_param?: string };
  safeAreaInset?: Partial<SafeInsets>;
  contentSafeAreaInset?: Partial<SafeInsets>;
  performanceClass?: string;
  BackButton?: TelegramBackButton;
  HapticFeedback?: {
    impactOccurred?: (style: "light" | "medium" | "heavy") => void;
    notificationOccurred?: (type: "error" | "success" | "warning") => void;
    selectionChanged?: () => void;
  };
  setHeaderColor?: (color: string) => void;
  setBackgroundColor?: (color: string) => void;
  onEvent?: (event: string, handler: () => void) => void;
  offEvent?: (event: string, handler: () => void) => void;
};

type TelegramHost = Window & {
  Telegram?: { WebApp?: TelegramWebApp };
};

const DEV_FALLBACK_LABEL = "Browser preview — non-production";

function readWebApp(): TelegramWebApp | null {
  const webApp = (window as TelegramHost).Telegram?.WebApp;
  if (!webApp || typeof webApp.ready !== "function") return null;
  return webApp;
}

function readInsets(value: Partial<SafeInsets> | undefined): SafeInsets {
  return {
    top: typeof value?.top === "number" ? value.top : 0,
    right: typeof value?.right === "number" ? value.right : 0,
    bottom: typeof value?.bottom === "number" ? value.bottom : 0,
    left: typeof value?.left === "number" ? value.left : 0,
  };
}

function readPerformance(value: string | undefined): PerformanceClass {
  if (value === "LOW" || value === "AVERAGE" || value === "HIGH") return value;
  return "UNKNOWN";
}

function browserStartParam(): string | null {
  const param = new URLSearchParams(window.location.search).get("startapp");
  return param && param.trim() ? param.trim() : null;
}

function applyInsets(safeArea: SafeInsets, contentSafeArea: SafeInsets, performanceClass: PerformanceClass): void {
  const root = document.documentElement;
  root.style.setProperty("--tg-safe-top", `${safeArea.top}px`);
  root.style.setProperty("--tg-safe-right", `${safeArea.right}px`);
  root.style.setProperty("--tg-safe-bottom", `${safeArea.bottom}px`);
  root.style.setProperty("--tg-safe-left", `${safeArea.left}px`);
  root.style.setProperty("--tg-content-safe-top", `${contentSafeArea.top}px`);
  root.style.setProperty("--tg-content-safe-bottom", `${contentSafeArea.bottom}px`);
  root.dataset.motion = performanceClass === "LOW" ? "off" : "on";
}

export class TelegramBridge {
  snapshot: TelegramSnapshot;
  private webApp: TelegramWebApp | null;
  private listeners = new Set<(snapshot: TelegramSnapshot) => void>();
  private refresh = (): void => {
    this.webApp = readWebApp();
    this.snapshot = this.webApp ? this.readTelegram(this.webApp) : this.readBrowser();
    const safeArea = readInsets(this.webApp?.safeAreaInset);
    const contentSafeArea = readInsets(this.webApp?.contentSafeAreaInset);
    applyInsets(safeArea, contentSafeArea, this.snapshot.performanceClass);
    this.listeners.forEach((listener) => listener(this.snapshot));
  };

  constructor() {
    this.webApp = readWebApp();
    this.snapshot = this.webApp ? this.readTelegram(this.webApp) : this.readBrowser();
  }

  mount(): void {
    this.refresh();
    const webApp = this.webApp;
    if (!webApp) return;
    webApp.ready();
    webApp.expand?.();
    webApp.setHeaderColor?.("#0B0D10");
    webApp.setBackgroundColor?.("#0B0D10");
    webApp.onEvent?.("themeChanged", this.refresh);
    webApp.onEvent?.("viewportChanged", this.refresh);
    webApp.onEvent?.("safeAreaChanged", this.refresh);
    webApp.onEvent?.("contentSafeAreaChanged", this.refresh);
  }

  subscribe(listener: (snapshot: TelegramSnapshot) => void): () => void {
    this.listeners.add(listener);
    listener(this.snapshot);
    return () => this.listeners.delete(listener);
  }

  initData(): string {
    return this.webApp?.initData?.trim() ?? "";
  }

  impact(style: "light" | "medium" | "heavy" = "light"): void {
    this.webApp?.HapticFeedback?.impactOccurred?.(style);
  }

  selection(): void {
    this.webApp?.HapticFeedback?.selectionChanged?.();
  }

  bindBack(handler: () => void): () => void {
    const button = this.webApp?.BackButton;
    if (!button) return () => undefined;
    button.onClick(handler);
    button.show();
    return () => {
      button.offClick(handler);
      button.hide();
    };
  }

  private readTelegram(webApp: TelegramWebApp): TelegramSnapshot {
    const insideTelegram = (webApp.initData ?? "").length > 0;
    if (!insideTelegram) return this.readBrowser();
    return {
      isTelegram: true,
      isDevFallback: false,
      platform: webApp.platform ?? "telegram",
      colorScheme: webApp.colorScheme === "light" ? "light" : "dark",
      performanceClass: readPerformance(webApp.performanceClass),
      startParam: webApp.initDataUnsafe?.start_param ?? null,
      fallbackLabel: "",
    };
  }

  private readBrowser(): TelegramSnapshot {
    return {
      isTelegram: false,
      isDevFallback: true,
      platform: "browser",
      colorScheme: "dark",
      performanceClass: "UNKNOWN",
      startParam: browserStartParam(),
      fallbackLabel: DEV_FALLBACK_LABEL,
    };
  }
}

export const telegramBridge = new TelegramBridge();

export function showTrainingBadge(): boolean {
  const configured = import.meta.env.VITE_PACK_ENV;
  const env = configured ?? (import.meta.env.DEV ? "dev" : "staging");
  return env === "dev" || env === "staging" || env === "mock";
}
