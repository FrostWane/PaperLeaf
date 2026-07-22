import type { Metadata } from "next";
import { Brand } from "@/components/brand";
import { LoginForm } from "@/components/login-form";

export const metadata: Metadata = { title: "登录" };
export default function LoginPage() { return <main className="login-page"><section className="login-panel"><a href="/"><Brand /></a><div className="login-copy"><span className="eyebrow">Private research workspace</span><h1>继续你的研究。</h1><p>登录后访问个人文献库、带页码的问答和可恢复的 Agent 任务。</p></div><LoginForm /></section><aside className="login-aside"><div className="login-paper"><span className="mono">CURRENT PAPER · 02 / 15</span><h2>Attention Is All You Need</h2><p>“We propose a new simple network architecture, the Transformer, based solely on attention mechanisms…”</p><div><span>引用已定位</span><strong>PDF 02</strong></div></div><p>你的论文内容不会出现在公共演示中。</p></aside></main>; }
