"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { AlertTriangle, RotateCcw, Save, Trash2, X } from "lucide-react";
import { useState } from "react";
import type { Paper, PaperUpdateInput } from "@/lib/types";

interface PaperDetailsDialogProps {
  paper: Paper;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSave: (input: PaperUpdateInput) => Promise<void>;
  onDelete: () => Promise<void>;
  onRetry: () => Promise<void>;
}

export function PaperDetailsDialog(props: PaperDetailsDialogProps) {
  // 关闭后卸载表单状态；再次打开时始终以 Worker/服务端最新回填值初始化。
  // 编辑期间组件保持挂载，不会因后台状态刷新覆盖用户尚未保存的输入。
  if (!props.open) return null;
  return <OpenPaperDetailsDialog {...props} />;
}

function OpenPaperDetailsDialog({ paper, open, onOpenChange, onSave, onDelete, onRetry }: PaperDetailsDialogProps) {
  const [title, setTitle] = useState(paper.title);
  const [authors, setAuthors] = useState(paper.authors);
  const [year, setYear] = useState(paper.year > 0 ? String(paper.year) : "");
  const [doi, setDoi] = useState(paper.doi ?? "");
  const [publication, setPublication] = useState(paper.publication);
  const [abstract, setAbstract] = useState(paper.abstract);
  const [busy, setBusy] = useState<"save" | "delete" | "retry" | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [message, setMessage] = useState("");

  function changeOpen(next: boolean) {
    if (busy) return;
    if (!next) { setConfirmDelete(false); setMessage(""); }
    onOpenChange(next);
  }

  async function save() {
    if (!title.trim()) { setMessage("论文标题不能为空。"); return; }
    const parsedYear = year ? Number(year) : undefined;
    if (parsedYear !== undefined && (!Number.isInteger(parsedYear) || parsedYear < 1400 || parsedYear > 2200)) {
      setMessage("年份应为 1400 到 2200 之间的整数。");
      return;
    }
    setBusy("save");
    setMessage("");
    try {
      await onSave({
        title: title.trim(),
        authors: authors.split(/[、,，;；\n]/).map((item) => item.trim()).filter(Boolean),
        year: parsedYear,
        doi: doi.trim() || undefined,
        publication: publication.trim() || undefined,
        abstract: abstract.trim() || undefined,
      });
      setMessage("文献信息已保存。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "保存失败");
    } finally { setBusy(null); }
  }

  async function retry() {
    setBusy("retry");
    setMessage("");
    try { await onRetry(); setMessage("已重新加入解析队列。"); }
    catch (error) { setMessage(error instanceof Error ? error.message : "重新处理失败"); }
    finally { setBusy(null); }
  }

  async function remove() {
    if (!confirmDelete) { setConfirmDelete(true); return; }
    setBusy("delete");
    setMessage("");
    try { await onDelete(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "删除失败"); }
    finally { setBusy(null); }
  }

  return (
    <Dialog.Root open={open} onOpenChange={changeOpen}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content paper-details-dialog" aria-describedby="paper-details-description">
          <div className="dialog-head">
            <div><Dialog.Title>文献设置</Dialog.Title><Dialog.Description id="paper-details-description">修改可检索的元数据，或管理这篇论文的处理状态。</Dialog.Description></div>
            <Dialog.Close className="icon-button" aria-label="关闭"><X size={17} /></Dialog.Close>
          </div>
          <div className="paper-form-grid">
            <label className="paper-form-wide"><span>标题</span><input value={title} onChange={(event) => setTitle(event.target.value)} /></label>
            <label><span>作者</span><input value={authors} onChange={(event) => setAuthors(event.target.value)} placeholder="多位作者用逗号分隔" /></label>
            <label><span>年份</span><input inputMode="numeric" value={year} onChange={(event) => setYear(event.target.value)} placeholder="待识别" /></label>
            <label className="paper-form-wide"><span>出版物</span><input value={publication} onChange={(event) => setPublication(event.target.value)} placeholder="期刊、会议或预印本平台" /></label>
            <label className="paper-form-wide"><span>DOI</span><input className="mono" value={doi} onChange={(event) => setDoi(event.target.value)} placeholder="10.xxxx/xxxxx" /></label>
            <label className="paper-form-wide"><span>摘要</span><textarea rows={5} value={abstract} onChange={(event) => setAbstract(event.target.value)} /></label>
          </div>
          {message && <p className="form-note" role="status">{message}</p>}
          <div className="paper-management">
            <div><strong>处理与删除</strong><p>重新识别会保留 PDF 原件，并重建文本索引和缺失的内置元数据；删除会进入后台幂等清理队列。</p></div>
            {paper.status !== "indexing" && paper.status !== "deleting" && <button className="secondary-button" disabled={Boolean(busy)} onClick={retry}><RotateCcw size={14} />{busy === "retry" ? "正在加入" : "重新识别并索引"}</button>}
            <button className={confirmDelete ? "danger-button confirmed" : "danger-button"} disabled={Boolean(busy)} onClick={remove}>{confirmDelete ? <AlertTriangle size={14} /> : <Trash2 size={14} />}{busy === "delete" ? "正在删除" : confirmDelete ? "确认删除" : "删除文献"}</button>
          </div>
          {confirmDelete && <p className="delete-warning" role="alert">再次点击“确认删除”后，论文、索引、引用和 Agent 产物将进入清理流程。</p>}
          <div className="dialog-actions"><Dialog.Close asChild><button className="secondary-button" disabled={Boolean(busy)}>取消</button></Dialog.Close><button className="primary-button" disabled={Boolean(busy)} onClick={save}><Save size={14} />{busy === "save" ? "正在保存" : "保存修改"}</button></div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
