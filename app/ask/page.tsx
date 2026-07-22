import type { Metadata } from "next";
import { AppShell } from "@/components/app-shell";
import { AskView } from "@/components/ask-view";
export const metadata: Metadata = { title: "跨文献提问" };
export default function AskPage() { return <AppShell active="/ask" title="跨文献提问" eyebrow="Ask / grounded answers"><AskView /></AppShell>; }
