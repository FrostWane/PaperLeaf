"use client";

import { BookOpen, Compass, LogOut, MessageSquareText, Settings, ShieldCheck, UserRound } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Brand } from "./brand";
import { applyFontScale, demoCurrentUser, getCurrentUser, logout, type CurrentUser } from "@/lib/preferences-api";

const nav = [
  { href: "/library", label: "文献库", icon: BookOpen },
  { href: "/ask", label: "跨文献提问", icon: MessageSquareText },
  { href: "/discover", label: "发现", icon: Compass },
  { href: "/admin", label: "管理", icon: ShieldCheck, adminOnly: true },
  { href: "/settings", label: "设置", icon: Settings },
];

interface AppShellProps {
  active: string;
  title: string;
  eyebrow?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
  flush?: boolean;
  demo?: boolean;
  onLoggedOut?: () => void;
}

export function AppShell({ active, title, eyebrow, actions, children, flush = false, demo = false, onLoggedOut }: AppShellProps) {
  const usesDemoData = demo || process.env.NEXT_PUBLIC_DATA_MODE !== "real";
  const [user, setUser] = useState<CurrentUser | null>(usesDemoData ? demoCurrentUser : null);
  const [accountError, setAccountError] = useState("");
  const [isLoggingOut, setIsLoggingOut] = useState(false);
  const visibleNav = useMemo(() => nav.filter((item) => !item.adminOnly || user?.role === "admin"), [user?.role]);

  useEffect(() => {
    let activeRequest = true;
    if (!usesDemoData) {
      void getCurrentUser()
        .then((nextUser) => {
          if (!activeRequest) return;
          setUser(nextUser);
          applyFontScale(nextUser.preferences.fontScale);
        })
        .catch((error) => {
          if (activeRequest) setAccountError(error instanceof Error ? error.message : "账户信息读取失败");
        });
    } else {
      applyFontScale(demoCurrentUser.preferences.fontScale);
    }

    function syncProfile(event: Event) {
      const detail = (event as CustomEvent<{ displayName?: string; preferences?: CurrentUser["preferences"] }>).detail;
      if (!detail) return;
      setUser((current) => current ? {
        ...current,
        ...(detail.displayName ? { displayName: detail.displayName } : {}),
        ...(detail.preferences ? { preferences: detail.preferences } : {}),
      } : current);
      if (detail.preferences) applyFontScale(detail.preferences.fontScale);
    }

    window.addEventListener("paperleaf:profile-updated", syncProfile);
    return () => {
      activeRequest = false;
      window.removeEventListener("paperleaf:profile-updated", syncProfile);
    };
  }, [usesDemoData]);

  async function handleLogout() {
    if (isLoggingOut) return;
    setAccountError("");
    setIsLoggingOut(true);
    try {
      if (!usesDemoData) await logout();
      if (onLoggedOut) onLoggedOut();
      else window.location.replace("/login");
    } catch (error) {
      setAccountError(error instanceof Error ? error.message : "退出登录失败");
      setIsLoggingOut(false);
    }
  }

  const displayName = user?.displayName ?? "正在载入账户";
  const avatarText = Array.from(displayName)[0] ?? "研";

  return (
    <div className="app-shell">
      <aside className="global-sidebar">
        <a className="global-brand" href="/" aria-label="PaperLeaf 首页"><Brand /></a>
        <nav aria-label="主导航" className={user?.role === "admin" ? "global-nav" : "global-nav non-admin"}>
          {visibleNav.map(({ href, label, icon: Icon }) => (
            <a key={href} href={href} className={active === href ? "nav-item active" : "nav-item"} aria-current={active === href ? "page" : undefined}>
              <Icon size={18} />
              <span>{label}</span>
              {active === href && <span className="nav-current">当前</span>}
            </a>
          ))}
        </nav>
        <details className="account-menu">
          <summary aria-label={`打开 ${displayName} 的账户菜单`}>
            <span className="avatar" aria-hidden="true">{avatarText}</span>
            <span className="account-summary"><strong>{displayName}</strong><small>{user?.email ?? "正在验证身份"}</small></span>
          </summary>
          <div className="account-popover">
            <a href="/settings"><UserRound size={17} /><span><strong>个人设置</strong><small>资料、阅读与 AI 偏好</small></span></a>
            <button type="button" onClick={handleLogout} disabled={isLoggingOut}><LogOut size={17} /><span>{isLoggingOut ? "正在退出…" : "退出登录"}</span></button>
          </div>
          {accountError && <p className="account-error" role="alert">{accountError}</p>}
        </details>
      </aside>
      <div className="app-main">
        <header className="app-header">
          <div><span className="eyebrow">{eyebrow ?? "个人研究文献库"}</span><h1>{title}</h1></div>
          <div className="header-actions">
            {accountError && <span className="header-account-error" role="alert">{accountError}</span>}
            {usesDemoData && <span className="demo-badge">固定演示数据</span>}
            {actions}
            <a className="mobile-account-link" href="/settings" aria-label={`打开 ${displayName} 的账户与退出设置`}><UserRound size={18} /><span>账户</span></a>
          </div>
        </header>
        <main className={flush ? "page-content flush" : "page-content"}>{children}</main>
      </div>
    </div>
  );
}
