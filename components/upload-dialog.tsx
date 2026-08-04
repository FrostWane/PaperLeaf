"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { FileUp, Upload, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { demoDataSource, getDataSource } from "@/lib/data-source";
import type { PaperCollection } from "@/lib/types";
import { PaperCollectionPicker } from "./paper-collection-picker";

export function UploadDialog({ onUploaded, demo = false }: { onUploaded?: () => void; demo?: boolean }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState(0);
  const [open, setOpen] = useState(false);
  const [message, setMessage] = useState("");
  const [collections, setCollections] = useState<PaperCollection[]>([]);
  const [selectedCollectionIds, setSelectedCollectionIds] = useState<string[]>([]);
  const [collectionsLoading, setCollectionsLoading] = useState(false);
  const [collectionsError, setCollectionsError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    let active = true;
    const source = demo ? demoDataSource : getDataSource();
    void source.listCollections()
      .then((items) => { if (active) setCollections(items); })
      .catch(() => { if (active) { setCollections([]); setCollectionsError("集合读取失败，可先上传后再整理。"); } })
      .finally(() => { if (active) setCollectionsLoading(false); });
    return () => { active = false; };
  }, [demo, open]);

  function resetDialog() {
    setFile(null);
    setProgress(0);
    setMessage("");
    setSelectedCollectionIds([]);
    setCollections([]);
    setCollectionsError("");
  }

  function changeOpen(next: boolean) {
    if (busy && !next) return;
    if (next) {
      setCollectionsLoading(true);
      setCollectionsError("");
    }
    setOpen(next);
    if (!next) resetDialog();
  }

  async function upload() {
    if (!file) return;
    setBusy(true);
    setMessage("正在安全上传…");
    const source = demo ? demoDataSource : getDataSource();
    try {
      const paper = await source.upload(file, setProgress);
      let closeDelay = 800;
      if (selectedCollectionIds.length > 0) {
        const results = await Promise.allSettled(selectedCollectionIds.map((targetId) => source.bulkPapers({ paperIds: [paper.id], action: "add_collection", targetId })));
        const failedNames = results.flatMap((result, index) => result.status === "rejected" ? [collections.find((item) => item.id === selectedCollectionIds[index])?.name ?? "未知集合"] : []);
        if (failedNames.length === 0) {
          setMessage(`上传完成，已加入 ${selectedCollectionIds.length} 个集合和解析队列`);
        } else {
          setMessage(`PDF 已上传，但以下集合归类失败：${failedNames.join("、")}。可稍后重新整理。`);
          closeDelay = 1800;
        }
      } else {
        setMessage("上传完成，已加入解析队列");
      }
      window.dispatchEvent(new Event("paperleaf:papers-changed"));
      onUploaded?.();
      window.setTimeout(() => { setOpen(false); resetDialog(); setBusy(false); }, closeDelay);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "上传失败");
      setBusy(false);
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={changeOpen}>
      <Dialog.Trigger asChild><button className="primary-button"><Upload size={16} />上传 PDF</button></Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content" aria-describedby="upload-help">
          <div className="dialog-head"><div><Dialog.Title>上传一篇论文</Dialog.Title><Dialog.Description id="upload-help">支持 PDF，演示模式不会将文件发送到服务器。</Dialog.Description></div><Dialog.Close className="icon-button" aria-label="关闭" disabled={busy}><X size={17} /></Dialog.Close></div>
          <button type="button" className="dropzone" disabled={busy} onClick={() => inputRef.current?.click()}><FileUp size={25} /><strong>{file ? file.name : "选择 PDF 文件"}</strong><span>{file ? `${(file.size / 1024 / 1024).toFixed(2)} MB` : "最大 50 MB · 文件将保持私有"}</span></button>
          <input ref={inputRef} className="sr-only" type="file" accept="application/pdf,.pdf" disabled={busy} onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
          {collectionsLoading ? <p className="collection-picker-loading" role="status">正在读取集合…</p> : collectionsError ? <p className="field-error" role="alert">{collectionsError}</p> : <PaperCollectionPicker collections={collections} selectedIds={selectedCollectionIds} onChange={setSelectedCollectionIds} disabled={busy} label="目标集合（可选）" emptyMessage="还没有集合，可先上传，之后再从文献库中整理。" />}
          {progress > 0 && <div className="upload-progress" aria-label={`上传进度 ${progress}%`}><span style={{ width: `${progress}%` }} /></div>}
          {message && <p className="form-note" role="status">{message}</p>}
          <div className="dialog-actions"><Dialog.Close asChild><button className="secondary-button" disabled={busy}>取消</button></Dialog.Close><button className="primary-button" disabled={!file || busy} onClick={upload}>{busy ? "正在上传" : "开始上传"}</button></div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
