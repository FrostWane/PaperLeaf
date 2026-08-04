"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { Save, X } from "lucide-react";
import { useMemo, useState } from "react";
import type { Paper, PaperCollection } from "@/lib/types";
import { PaperCollectionPicker } from "./paper-collection-picker";

interface PaperCollectionsDialogProps {
  paper: Paper | null;
  collections: PaperCollection[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSave: (paperId: string, collectionIds: string[]) => Promise<{ selectedIds: string[]; error?: string }>;
}

export function PaperCollectionsDialog({ paper, collections, open, onOpenChange, onSave }: PaperCollectionsDialogProps) {
  const currentIds = useMemo(
    () => paper ? collections.filter((collection) => collection.paperIds.includes(paper.id)).map((collection) => collection.id) : [],
    [collections, paper],
  );
  const [selectedIds, setSelectedIds] = useState<string[]>(currentIds);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const changed = currentIds.length !== selectedIds.length || currentIds.some((id) => !selectedIds.includes(id));

  function changeOpen(next: boolean) {
    if (busy && !next) return;
    onOpenChange(next);
  }

  async function save() {
    if (!paper || !changed) return;
    setBusy(true);
    setMessage("");
    try {
      const result = await onSave(paper.id, selectedIds);
      setSelectedIds(result.selectedIds);
      if (result.error) {
        setMessage(result.error);
        return;
      }
      onOpenChange(false);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "集合保存失败，请刷新后重试");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={changeOpen}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content paper-collections-dialog" aria-describedby="paper-collections-description">
          <div className="dialog-head">
            <div>
              <Dialog.Title>管理论文集合</Dialog.Title>
              <Dialog.Description id="paper-collections-description">为《{paper?.title ?? "当前论文"}》勾选或取消集合，保存后立即同步到文献库。</Dialog.Description>
            </div>
            <Dialog.Close className="icon-button" aria-label="关闭" disabled={busy}><X size={17} /></Dialog.Close>
          </div>
          <PaperCollectionPicker collections={collections} selectedIds={selectedIds} onChange={setSelectedIds} disabled={busy} />
          {message && <p className="field-error collection-save-error" role="alert">{message}</p>}
          <div className="dialog-actions">
            <Dialog.Close asChild><button className="secondary-button" disabled={busy}>取消</button></Dialog.Close>
            <button className="primary-button" disabled={busy || !changed || collections.length === 0} onClick={() => void save()}><Save size={14} />{busy ? "正在保存" : "保存集合"}</button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
