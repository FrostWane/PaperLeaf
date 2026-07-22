import type { Metadata } from "next";
import { PaperWorkspace } from "@/components/paper-workspace";
import { Brand } from "@/components/brand";

export const metadata: Metadata = { title: "交互演示" };
export default function DemoPage() { return <main className="demo-page" aria-label="PaperLeaf 文献阅读工作台"><header className="demo-bar"><a href="/"><Brand /></a><span>公开演示 · 使用固定文献与模拟 AI</span><a className="secondary-button" href="/library">进入完整界面</a></header><PaperWorkspace demo /></main>; }
