"use client";

import { BookOpen, Compass, LogOut, MessageSquareText, Settings, ShieldCheck, UserRound } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Brand } from "./brand";
import { clearCurrentUserState, useCurrentUserState } from "./current-user-provider";
import { logout } from "@/lib/preferences-api";

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
  const account = useCurrentUserState(usesDemoData ? "demo" : "real");
  const user = account.user;
  const [logoutError, setLogoutError] = useState("");
  const [isLoggingOut, setIsLoggingOut] = useState(false);
  const visibleNav = useMemo(() => nav.filter((item) => !item.adminOnly || user?.role === "admin"), [user?.role]);

  useEffect(() => {
    if (account.unauthorized) window.location.replace("/login");
  }, [account.unauthorized]);

  async function handleLogout() {
    if (isLoggingOut) return;
    setLogoutError("");
    setIsLoggingOut(true);
    try {
      if (!usesDemoData) await logout();
      clearCurrentUserState();
      if (onLoggedOut) onLoggedOut();
      else window.location.replace("/login");
    } catch (error) {
      setLogoutError(error instanceof Error ? error.message : "退出登录失败");
      setIsLoggingOut(false);
    }
  }

  const displayName = user?.displayName ?? "账户";
  const avatarText = Array.from(displayName)[0] ?? "研";
  const accountError = logoutError || account.error;
  const pendingAccount = !user && account.status !== "error";

  return (
    <div className="app-shell">
      <aside className="global-sidebar">
        <Link className="global-brand" href="/" aria-label="PaperLeaf 首页"><Brand /></Link>
        <nav aria-label="主导航" className={pendingAccount ? "global-nav role-pending" : user?.role === "admin" ? "global-nav" : "global-nav non-admin"}>
          {visibleNav.map(({ href, label, icon: Icon }) => (
            <Link key={href} href={href} className={active === href ? "nav-item active" : "nav-item"} aria-current={active === href ? "page" : undefined}>
              <Icon size={18} />
              <span>{label}</span>
              {active === href && <span className="nav-current">当前</span>}
            </Link>
          ))}
          {pendingAccount && <span className="nav-item nav-role-skeleton" aria-hidden="true" />}
        </nav>
        {pendingAccount ? <div className="account-menu account-loading" role="status" aria-label="正在验证账户"><span className="avatar skeleton" /><span className="account-summary"><strong /><small /></span></div> : <details className="account-menu">
          <summary aria-label={`打开 ${displayName} 的账户菜单`}>
            <span className="avatar" aria-hidden="true">{avatarText}</span>
            <span className="account-summary"><strong>{displayName}</strong><small>{user?.email ?? "正在验证身份"}</small></span>
          </summary>
          <div className="account-popover">
            <Link href="/settings"><UserRound size={17} /><span><strong>个人设置</strong><small>资料、阅读与 AI 偏好</small></span></Link>
            <button type="button" onClick={handleLogout} disabled={isLoggingOut}><LogOut size={17} /><span>{isLoggingOut ? "正在退出…" : "退出登录"}</span></button>
          </div>
          {accountError && <p className="account-error" role="alert">{accountError}</p>}
        </details>}
      </aside>
      <div className="app-main">
        <header className="app-header">
          <div>{eyebrow && <span className="eyebrow">{eyebrow}</span>}<h1>{title}</h1></div>
          <div className="header-actions">
            {accountError && <span className="header-account-error" role="alert">{accountError}</span>}
            {usesDemoData && <span className="demo-badge">固定演示数据</span>}
            {actions}
            <Link className="mobile-account-link" href="/settings" aria-label={`打开 ${displayName} 的账户与退出设置`}><UserRound size={18} /><span>账户</span></Link>
          </div>
        </header>
        <main className={flush ? "page-content flush" : "page-content"}>{children}</main>
      </div>
    </div>
  );
}
