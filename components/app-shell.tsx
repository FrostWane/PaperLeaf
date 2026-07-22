"use client";

import { Bell, BookOpen, Compass, MessageSquareText, Settings, ShieldCheck } from "lucide-react";
import { Brand } from "./brand";

const nav = [
  { href: "/library", label: "文献库", icon: BookOpen },
  { href: "/ask", label: "跨文献提问", icon: MessageSquareText },
  { href: "/discover", label: "发现", icon: Compass },
  { href: "/admin", label: "管理", icon: ShieldCheck },
  { href: "/settings", label: "设置", icon: Settings },
];

export function AppShell({ active, title, eyebrow, actions, children, flush = false }: { active: string; title: string; eyebrow?: string; actions?: React.ReactNode; children: React.ReactNode; flush?: boolean }) {
  return (
    <div className="app-shell">
      <aside className="global-sidebar">
        <a className="global-brand" href="/" aria-label="PaperLeaf 首页"><Brand /></a>
        <nav aria-label="主导航" className="global-nav">
          {nav.map(({ href, label, icon: Icon }) => <a key={href} href={href} className={active === href ? "nav-item active" : "nav-item"} aria-current={active === href ? "page" : undefined}><Icon size={17} /><span>{label}</span>{active === href && <span className="nav-current">当前</span>}</a>)}
        </nav>
        <div className="sidebar-foot"><span className="avatar">林</span><span><strong>林研究员</strong><small>个人工作区</small></span></div>
      </aside>
      <div className="app-main">
        <header className="app-header">
          <div><span className="eyebrow">{eyebrow ?? "Personal research library"}</span><h1>{title}</h1></div>
          <div className="header-actions">{process.env.NEXT_PUBLIC_DATA_MODE !== "real" && <span className="demo-badge">固定演示数据</span>}<button className="icon-button" aria-label="通知"><Bell size={17} /></button>{actions}</div>
        </header>
        <main className={flush ? "page-content flush" : "page-content"}>{children}</main>
      </div>
    </div>
  );
}
