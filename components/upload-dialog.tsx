"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { FileUp, Upload, X } from "lucide-react";
import { useRef, useState } from "react";
import { getDataSource } from "@/lib/data-source";

export function UploadDialog({ onUploaded }: { onUploaded?: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState(0);
  const [open, setOpen] = useState(false);
  const [message, setMessage] = useState("");

  async function upload() {
    if (!file) return;
    setMessage("正在安全上传…");
    try {
      await getDataSource().upload(file, setProgress);
      setMessage("上传完成，已加入解析队列");
      window.dispatchEvent(new Event("paperleaf:papers-changed"));
      onUploaded?.();
      window.setTimeout(() => { setOpen(false); setFile(null); setProgress(0); setMessage(""); }, 700);
    } catch (error) { setMessage(error instanceof Error ? error.message : "上传失败"); }
  }

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild><button className="primary-button"><Upload size={16} />上传 PDF</button></Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content" aria-describedby="upload-help">
          <div className="dialog-head"><div><Dialog.Title>上传一篇论文</Dialog.Title><Dialog.Description id="upload-help">支持 PDF，演示模式不会将文件发送到服务器。</Dialog.Description></div><Dialog.Close className="icon-button" aria-label="关闭"><X size={17} /></Dialog.Close></div>
          <button type="button" className="dropzone" onClick={() => inputRef.current?.click()}><FileUp size={25} /><strong>{file ? file.name : "选择 PDF 文件"}</strong><span>{file ? `${(file.size / 1024 / 1024).toFixed(2)} MB` : "最大 50 MB · 文件将保持私有"}</span></button>
          <input ref={inputRef} className="sr-only" type="file" accept="application/pdf,.pdf" onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
          {progress > 0 && <div className="upload-progress" aria-label={`上传进度 ${progress}%`}><span style={{ width: `${progress}%` }} /></div>}
          {message && <p className="form-note" role="status">{message}</p>}
          <div className="dialog-actions"><Dialog.Close asChild><button className="secondary-button">取消</button></Dialog.Close><button className="primary-button" disabled={!file || (progress > 0 && progress < 100)} onClick={upload}>开始上传</button></div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
