import type { Metadata } from "next";
import { AppShell } from "@/components/app-shell";
import { LibraryTable } from "@/components/library-table";
import { UploadDialog } from "@/components/upload-dialog";

export const metadata: Metadata = { title: "文献库" };
export default async function LibraryPage({ searchParams }: { searchParams: Promise<{ demo?: string }> }) {
  const demo = (await searchParams).demo === "1";
  return <AppShell active="/library" title="文献库" actions={<UploadDialog demo={demo} />} demo={demo}><LibraryTable demo={demo} /></AppShell>;
}
