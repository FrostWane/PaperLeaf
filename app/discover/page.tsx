import type { Metadata } from "next";
import { AppShell } from "@/components/app-shell";
import { DiscoverView } from "@/components/discover-view";
export const metadata: Metadata = { title: "发现论文" };
export default function DiscoverPage() { return <AppShell active="/discover" title="发现论文"><DiscoverView /></AppShell>; }
