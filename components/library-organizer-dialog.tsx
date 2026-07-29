"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { Pencil, Plus, Trash2, X } from "lucide-react";
import { useState } from "react";
import type { CollectionInput, PaperCollection, PaperTag, TagInput } from "@/lib/types";

type OrganizerKind = "collection" | "tag";

interface LibraryOrganizerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  collections: PaperCollection[];
  tags: PaperTag[];
  onCreateCollection: (input: CollectionInput) => Promise<void>;
  onUpdateCollection: (id: string, input: CollectionInput) => Promise<void>;
  onDeleteCollection: (id: string) => Promise<void>;
  onCreateTag: (input: TagInput) => Promise<void>;
  onUpdateTag: (id: string, input: TagInput) => Promise<void>;
  onDeleteTag: (id: string) => Promise<void>;
}

const tagColors = [
  { value: "#AFC3CE", label: "雾蓝" },
  { value: "#B8C9BC", label: "鼠尾草" },
  { value: "#C9BFAE", label: "暖灰" },
  { value: "#C8AAA5", label: "陶土" },
];

export function LibraryOrganizerDialog(props: LibraryOrganizerDialogProps) {
  const { open, onOpenChange, collections, tags } = props;
  const [kind, setKind] = useState<OrganizerKind>("collection");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [color, setColor] = useState(tagColors[0].value);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [message, setMessage] = useState("");

  function resetDialogState() {
    setEditingId(null);
    setName("");
    setDescription("");
    setConfirmDelete(null);
    setMessage("");
  }

  function changeKind(next: OrganizerKind) {
    resetDialogState();
    setKind(next);
  }

  function changeOpen(next: boolean) {
    if (busy) return;
    if (!next) resetDialogState();
    onOpenChange(next);
  }

  const items = kind === "collection" ? collections : tags;

  function startEdit(id: string) {
    const item = items.find((entry) => entry.id === id);
    if (!item) return;
    setEditingId(item.id);
    setName(item.name);
    setDescription("description" in item ? item.description ?? "" : "");
    setColor("color" in item ? item.color ?? tagColors[0].value : tagColors[0].value);
    setConfirmDelete(null);
    setMessage("");
  }

  function resetForm() {
    setEditingId(null);
    setName("");
    setDescription("");
    setColor(tagColors[0].value);
    setConfirmDelete(null);
  }

  async function save() {
    if (!name.trim()) { setMessage(kind === "collection" ? "集合名称不能为空。" : "标签名称不能为空。"); return; }
    setBusy(true);
    setMessage("");
    try {
      if (kind === "collection") {
        const input = { name: name.trim(), description: description.trim() || undefined };
        if (editingId) await props.onUpdateCollection(editingId, input);
        else await props.onCreateCollection(input);
      } else {
        const input = { name: name.trim(), color };
        if (editingId) await props.onUpdateTag(editingId, input);
        else await props.onCreateTag(input);
      }
      setMessage(editingId ? "修改已保存。" : kind === "collection" ? "集合已创建。" : "标签已创建。");
      resetForm();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "保存失败");
    } finally { setBusy(false); }
  }

  async function remove(id: string) {
    if (confirmDelete !== id) { setConfirmDelete(id); return; }
    setBusy(true);
    setMessage("");
    try {
      if (kind === "collection") await props.onDeleteCollection(id);
      else await props.onDeleteTag(id);
      if (editingId === id) resetForm();
      setConfirmDelete(null);
      setMessage(kind === "collection" ? "集合已删除，文献本身仍保留。" : "标签已删除，文献本身仍保留。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "删除失败");
    } finally { setBusy(false); }
  }

  return (
    <Dialog.Root open={open} onOpenChange={changeOpen}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content organizer-dialog" aria-describedby="organizer-description">
          <div className="dialog-head">
            <div><Dialog.Title>管理文献组织</Dialog.Title><Dialog.Description id="organizer-description">维护集合与标签；删除组织项不会删除其中的 PDF。</Dialog.Description></div>
            <Dialog.Close className="icon-button" aria-label="关闭"><X size={17} /></Dialog.Close>
          </div>
          <div className="organizer-kind-tabs" role="tablist" aria-label="组织类型">
            <button role="tab" aria-selected={kind === "collection"} className={kind === "collection" ? "active" : ""} onClick={() => changeKind("collection")}>集合</button>
            <button role="tab" aria-selected={kind === "tag"} className={kind === "tag" ? "active" : ""} onClick={() => changeKind("tag")}>标签</button>
          </div>
          <div className="organizer-list" aria-label={kind === "collection" ? "已有集合" : "已有标签"}>
            {items.length === 0 && <p>还没有{kind === "collection" ? "集合" : "标签"}。</p>}
            {items.map((item) => (
              <div className="organizer-item" key={item.id}>
                <span className={kind === "tag" ? "tag-color-dot" : "collection-mark"} style={kind === "tag" ? { backgroundColor: (item as PaperTag).color ?? tagColors[0].value } : undefined} aria-hidden="true" />
                <span><strong>{item.name}</strong><small>{item.paperIds.length} 篇文献</small></span>
                <button className="icon-button" aria-label={`编辑 ${item.name}`} onClick={() => startEdit(item.id)} disabled={busy}><Pencil size={14} /></button>
                <button className={confirmDelete === item.id ? "icon-button danger" : "icon-button"} aria-label={confirmDelete === item.id ? `确认删除 ${item.name}` : `删除 ${item.name}`} onClick={() => void remove(item.id)} disabled={busy}><Trash2 size={14} /></button>
              </div>
            ))}
          </div>
          <div className="organizer-form">
            <div className="organizer-form-head"><strong>{editingId ? "编辑" : "新建"}{kind === "collection" ? "集合" : "标签"}</strong>{editingId && <button className="text-button" onClick={resetForm}>取消编辑</button>}</div>
            <label><span>名称</span><input value={name} onChange={(event) => setName(event.target.value)} maxLength={kind === "collection" ? 200 : 100} /></label>
            {kind === "collection" ? <label><span>说明（可选）</span><input value={description} onChange={(event) => setDescription(event.target.value)} maxLength={2000} /></label> : <label><span>标记色</span><select value={color} onChange={(event) => setColor(event.target.value)}>{tagColors.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}</select></label>}
            <button className="primary-button" onClick={() => void save()} disabled={busy}><Plus size={14} />{busy ? "正在保存" : editingId ? "保存修改" : "创建"}</button>
          </div>
          {message && <p className="form-note organizer-message" role="status">{message}</p>}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
