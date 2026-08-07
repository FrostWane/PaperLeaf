import type { Metadata } from "next";
import { AppShell } from "@/components/app-shell";
import { AdminView } from "@/components/admin-view";
export const metadata: Metadata = { title: "管理工作区" };
export default function AdminPage() { return <AppShell active="/admin" title="管理工作区"><AdminView /></AppShell>; }
