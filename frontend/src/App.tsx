import {
  BrowserRouter,
  Link,
  NavLink,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { useEffect, useRef, useState } from "react";
import { getAuthToken, setAuthToken, API, cn } from "@/lib/utils";
import { I18nProvider, useI18n } from "@/lib/i18n-context";
import type { TranslationKey } from "@/lib/i18n";
import Dashboard from "@/pages/Dashboard";
import Accounts from "@/pages/Accounts";
import SettingsPage from "@/pages/SettingsPage";
import UpdateBanner from "@/components/UpdateBanner";
import {
  LayoutDashboard,
  Moon,
  Settings as SettingsIcon,
  Sun,
  Monitor,
  Languages,
  Users,
  PanelLeftClose,
  PanelLeft,
  Mail,
  Plus,
  ChevronRight,
  Menu,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

/* ------------------------------------------------------------------ */
/*  Sidebar                                                            */
/* ------------------------------------------------------------------ */

type NavItem = {
  path: string;
  labelKey?: TranslationKey;
  label?: string;
  icon: LucideIcon;
  exact?: boolean;
};

const SETTINGS_NAV_ITEMS: { labelKey: TranslationKey; hash: string }[] = [
  { labelKey: "nav.settings.general", hash: "general" },
  { labelKey: "nav.settings.mailbox", hash: "mailbox" },
];

const NAV_ITEMS: NavItem[] = [
  { path: "/", labelKey: "nav.dashboard", icon: LayoutDashboard, exact: true },
  { path: "/accounts/chatgpt", label: "chatgpt free", icon: Users },
  { path: "/settings", labelKey: "nav.settings", icon: SettingsIcon },
];

function Sidebar({
  theme,
  toggleTheme,
  collapsed,
  setCollapsed,
  mobileOpen,
  closeMobile,
}: {
  theme: string;
  toggleTheme: () => void;
  collapsed: boolean;
  setCollapsed: (v: boolean) => void;
  mobileOpen: boolean;
  closeMobile: () => void;
}) {
  const { t, toggleLanguage } = useI18n();
  const location = useLocation();
  const isSettings = location.pathname === "/settings";
  const currentTab = new URLSearchParams(location.search).get("tab") || "general";
  const sidebarRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!mobileOpen) return;
    const sidebar = sidebarRef.current;
    const previousFocus = document.activeElement as HTMLElement | null;
    const focusable = () => Array.from(sidebar?.querySelectorAll<HTMLElement>('a, button:not(:disabled)') || [])
      .filter(element => element.getClientRects().length > 0);
    focusable()[0]?.focus();
    const trapFocus = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return;
      const items = focusable();
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    sidebar?.addEventListener('keydown', trapFocus);
    return () => { sidebar?.removeEventListener('keydown', trapFocus); previousFocus?.focus(); };
  }, [mobileOpen]);

  const navLinkClass = (active: boolean) =>
    cn(
      "group flex items-center gap-3 rounded-lg px-3 py-2 text-[13px] font-medium transition-colors",
      active
        ? "bg-[var(--accent-soft)] text-[var(--text-primary)]"
        : "text-[var(--text-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]",
      collapsed && "justify-center px-0",
    );

  const iconClass = (active: boolean) =>
    cn(
      "h-[18px] w-[18px] shrink-0",
      active
        ? "text-[var(--accent)]"
        : "text-[var(--text-muted)] group-hover:text-[var(--text-secondary)]",
    );

  return (
    <aside
      ref={sidebarRef}
      className={cn(
        "app-sidebar flex shrink-0 flex-col",
        collapsed && "is-collapsed",
        mobileOpen && "is-mobile-open",
      )}
      id="app-navigation"
    >
      {/* Header */}
      <div
        className={cn(
          "sidebar-brand flex shrink-0 items-center gap-2 border-b border-[var(--border)] px-4",
          collapsed && "justify-center",
        )}
      >
        {(!collapsed || mobileOpen) && (
          <div className="flex items-center gap-2.5 min-w-0 flex-1">
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-[var(--accent)] text-[11px] font-bold text-white">
              B
            </div>
            <span className="truncate text-sm font-semibold text-[var(--text-primary)]">
              BangBang
            </span>
          </div>
        )}
        {collapsed && !mobileOpen && (
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-[var(--accent)] text-[11px] font-bold text-white">
            B
          </div>
        )}
        <button className="mobile-only icon-control" onClick={closeMobile} aria-label={t("common.close")}><X size={18} /></button>
      </div>

      {/* Nav */}
      <nav className="flex-1 overflow-y-auto px-3 py-5 space-y-1" onClick={closeMobile}>
        {NAV_ITEMS.map(({ path, labelKey, label: itemLabel, icon: Icon, exact }) => {
          const active = path === "/settings" ? isSettings && currentTab !== "mailbox" : exact
            ? location.pathname === path
            : location.pathname.startsWith(path);
          const label = itemLabel || (labelKey ? t(labelKey) : path);
          return (
            <NavLink
              key={path}
              to={path}
              end={exact}
              className={navLinkClass(active)}
              title={collapsed ? label : undefined}
              aria-label={label}
              aria-current={active ? "page" : false}
            >
              <Icon className={iconClass(active)} />
              {(!collapsed || mobileOpen) && <span>{label}</span>}
            </NavLink>
          );
        })}
        {(!collapsed || mobileOpen) && isSettings && (
          <div className="ml-[21px] mt-0.5 space-y-px border-l border-[var(--border)] pl-3">
            {SETTINGS_NAV_ITEMS.map((item) => {
              const active = currentTab === item.hash;
              return (
                <NavLink
                  key={item.hash}
                  to={`/settings?tab=${item.hash}`}
                  className={cn(
                    "relative block rounded-md px-2.5 py-1.5 text-[13px] transition-colors",
                    active
                      ? "text-[var(--accent)] font-medium bg-[var(--accent-soft)]"
                      : "text-[var(--text-muted)] hover:text-[var(--text-secondary)] hover:bg-[var(--bg-hover)]",
                  )}
                >
                  {active && (
                    <span className="absolute -left-[13.5px] top-1/2 h-4 w-[2px] -translate-y-1/2 rounded-full bg-[var(--accent)]" />
                  )}
                  {t(item.labelKey)}
                </NavLink>
              );
            })}
          </div>
        )}
        <div className="!my-4 border-t border-[var(--border)]" />
        <Link to="/accounts/chatgpt?register=1" className={navLinkClass(false)} title={t("accounts.autoRegister")}>
          <Plus className={iconClass(false)} />{(!collapsed || mobileOpen) && <span>{t("accounts.autoRegister")}</span>}
        </Link>
        <Link to="/settings?tab=mailbox" className={navLinkClass(isSettings && currentTab === "mailbox")} aria-current={isSettings && currentTab === "mailbox" ? "page" : undefined} title={t("nav.settings.mailbox")}>
          <Mail className={iconClass(isSettings && currentTab === "mailbox")} />{(!collapsed || mobileOpen) && <span>{t("nav.settings.mailbox")}</span>}
        </Link>
      </nav>

      {/* Footer */}
      <div
        className={cn(
          "sidebar-footer shrink-0 border-t border-[var(--border)] px-3 py-3 flex items-center gap-1",
          collapsed && !mobileOpen && "flex-col px-1.5 py-2",
        )}
      >
        <button
          onClick={toggleTheme}
          className={cn(
            "flex items-center justify-center rounded-md p-2 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-secondary)]",
          )}
          title={
            theme === "light"
              ? t("sidebar.theme.toDark")
              : theme === "dark"
                ? t("sidebar.theme.toLight")
                : t("sidebar.theme.followSystem")
          }
        >
          {theme === "light" ? (
            <Moon className="h-4 w-4" />
          ) : theme === "system" ? (
            <Monitor className="h-4 w-4" />
          ) : (
            <Sun className="h-4 w-4" />
          )}
        </button>
        {!collapsed && (
          <span className="flex-1 text-[12px] text-[var(--text-muted)]">
            {theme === "light"
              ? t("sidebar.theme.light")
              : theme === "dark"
                ? t("sidebar.theme.dark")
                : t("sidebar.theme.system")}
          </span>
        )}
        <button
          onClick={toggleLanguage}
          className="flex items-center justify-center rounded-md p-2 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-secondary)]"
          title={t("sidebar.languageToggle")}
        >
          <Languages className="h-4 w-4" />
        </button>
        <button
          onClick={() => setCollapsed(!collapsed)}
          className="flex items-center justify-center rounded-md p-2 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-secondary)]"
          title={collapsed ? t("sidebar.expand") : t("sidebar.collapse")}
        >
          {collapsed ? (
            <PanelLeft className="h-4 w-4" />
          ) : (
            <PanelLeftClose className="h-4 w-4" />
          )}
        </button>
      </div>
    </aside>
  );
}

/* ------------------------------------------------------------------ */
/*  Shell                                                              */
/* ------------------------------------------------------------------ */

function Shell({
  theme,
  setTheme,
  toggleTheme,
}: {
  theme: string;
  setTheme: (t: string) => void;
  toggleTheme: () => void;
}) {
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("sidebar-collapsed") === "true",
  );
  const [mobileOpen, setMobileOpen] = useState(false);
  const { t } = useI18n();
  const location = useLocation();
  const pageTitle = location.pathname === "/settings"
    ? t("nav.settings")
    : location.pathname.startsWith("/accounts") ? "chatgpt free" : t("nav.dashboard");

  useEffect(() => {
    if (!mobileOpen) return;
    const media = window.matchMedia('(min-width: 768px)');
    const onResize = () => { if (media.matches) setMobileOpen(false); };
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileOpen(false);
    };
    window.addEventListener("keydown", close);
    media.addEventListener('change', onResize);
    return () => { window.removeEventListener("keydown", close); media.removeEventListener('change', onResize); };
  }, [mobileOpen]);

  useEffect(() => {
    localStorage.setItem("sidebar-collapsed", String(collapsed));
  }, [collapsed]);

  return (
    <div className="app-shell app-canvas overflow-hidden">
      {mobileOpen && <button className="sidebar-scrim" onClick={() => setMobileOpen(false)} aria-label={t("common.close")} />}
      <Sidebar
        theme={theme}
        toggleTheme={toggleTheme}
        collapsed={collapsed}
        setCollapsed={setCollapsed}
        mobileOpen={mobileOpen}
        closeMobile={() => setMobileOpen(false)}
      />
      <main id="main-content" className="workspace-main min-w-0 flex-1" inert={mobileOpen}>
        <header className="workspace-topbar">
          <button className="mobile-only icon-control" onClick={() => setMobileOpen(true)} aria-label={t("sidebar.expand")} aria-expanded={mobileOpen} aria-controls="app-navigation"><Menu size={18} /></button>
          <span className="text-[var(--text-muted)]">BangBang</span><ChevronRight size={14} className="text-[var(--text-muted)]" /><span>{pageTitle}</span>
        </header>
        <div className="workspace-content">
          <UpdateBanner />
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/accounts/chatgpt" element={<Accounts />} />
            <Route
              path="/settings"
              element={<SettingsPage theme={theme} setTheme={setTheme} />}
            />
          </Routes>
        </div>
      </main>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Login                                                              */
/* ------------------------------------------------------------------ */

function LoginScreen({ onLogin }: { onLogin: (token: string) => void }) {
  const { t } = useI18n();
  const [pw, setPw] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      const res = await fetch(API + "/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pw }),
      });
      const data = await res.json();
      if (data.ok) {
        setAuthToken(data.token || "");
        onLogin(data.token || "");
      } else {
        setError(data.error || t("login.passwordError"));
      }
    } catch {
      setError(t("login.requestFailed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="app-canvas flex h-dvh items-center justify-center">
      <form
        onSubmit={submit}
        className="panel-surface w-80 space-y-4 p-6"
      >
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-[var(--accent)] text-sm font-bold text-white">
            B
          </div>
          <h1 className="text-base font-semibold text-[var(--text-primary)]">
            BangBang
          </h1>
        </div>
        <p className="text-sm text-[var(--text-muted)]">{t("login.prompt")}</p>
        <input
          type="password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          placeholder={t("login.passwordPlaceholder")}
          autoFocus
          className="control-surface w-full"
        />
        {error && <p className="text-xs text-red-500">{error}</p>}
        <button
          type="submit"
          disabled={loading || !pw}
          className="w-full rounded-lg bg-[var(--accent)] px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-[var(--accent-hover)] disabled:opacity-50"
        >
          {loading ? t("login.checking") : t("login.submit")}
        </button>
      </form>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  App root                                                           */
/* ------------------------------------------------------------------ */

function AppContent() {
  const { t } = useI18n();
  const [theme, setTheme] = useState(
    () => localStorage.getItem("theme") || "dark",
  );
  const [authState, setAuthState] = useState<
    "loading" | "open" | "locked" | "authed"
  >("loading");

  useEffect(() => {
    const applyTheme = () => {
      let effective = theme;
      if (theme === "system") {
        effective = window.matchMedia("(prefers-color-scheme: light)").matches
          ? "light"
          : "dark";
      }
      document.documentElement.classList.toggle("light", effective === "light");
    };
    applyTheme();
    localStorage.setItem("theme", theme);
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const handler = () => {
      if (theme === "system") applyTheme();
    };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, [theme]);

  useEffect(() => {
    fetch(API + "/auth/check")
      .then((r) => r.json())
      .then((data) => {
        if (!data.required) setAuthState("open");
        else if (getAuthToken()) setAuthState("authed");
        else setAuthState("locked");
      })
      .catch(() => setAuthState("open"));
  }, []);

  const toggleTheme = () =>
    setTheme((c) =>
      c === "dark" ? "light" : c === "light" ? "system" : "dark",
    );

  if (authState === "loading") {
    return (
      <div className="app-canvas flex h-dvh items-center justify-center text-[var(--text-muted)] text-sm">
        {t("app.loading")}
      </div>
    );
  }
  if (authState === "locked") {
    return <LoginScreen onLogin={() => setAuthState("authed")} />;
  }

  return (
    <BrowserRouter>
      <Shell theme={theme} setTheme={setTheme} toggleTheme={toggleTheme} />
    </BrowserRouter>
  );
}

export default function App() {
  return (
    <I18nProvider>
      <AppContent />
    </I18nProvider>
  );
}
