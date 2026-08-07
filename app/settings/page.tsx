import type { Metadata } from "next";
import { AppShell } from "@/components/app-shell";
import { SettingsView } from "@/components/settings-view";
export const metadata: Metadata = { title: "设置" };
export default function SettingsPage() { return <AppShell active="/settings" title="设置"><SettingsView /></AppShell>; }
