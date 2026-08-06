"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { FolderPlus, Plus, Trash2, X } from "lucide-react";
import { useMemo, useState } from "react";
import { collectionDescendantIds, collectionForest, findCollection, flattenCollections } from "@/lib/collections";
import type { CollectionInput, PaperCollection } from "@/lib/types";
import { CollectionSelect } from "./collection-select";
import { CollectionTree } from "./collection-tree";

interface LibraryOrganizerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  collections: PaperCollection[];
  onCreateCollection: (input: CollectionInput) => Promise<void>;
  onUpdateCollection: (id: string, input: CollectionInput) => Promise<void>;
  onDeleteCollection: (id: string) => Promise<void>;
}

export function LibraryOrganizerDialog(props: LibraryOrganizerDialogProps) {
  const { open, onOpenChange, collections } = props;
  const forest = useMemo(() => collectionForest(collections), [collections]);
  const entries = useMemo(() => flattenCollections(forest), [forest]);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [parentId, setParentId] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [message, setMessage] = useState("");

  const editing = editingId ? findCollection(forest, editingId) : undefined;
  const disabledParentIds = useMemo(() => {
    function subtreeHeight(collection: PaperCollection): number {
      return collection.children.length ? 1 + Math.max(...collection.children.map(subtreeHeight)) : 1;
    }
    const disabledIds = new Set(editing ? [editing.id, ...collectionDescendantIds(editing)] : []);
    const height = editing ? subtreeHeight(editing) : 1;
    for (const entry of entries) if (entry.depth + height > 5) disabledIds.add(entry.collection.id);
    return disabledIds;
  }, [editing, entries]);

  function resetForm(nextParentId = "") {
    setEditingId(null);
    setName("");
    setDescription("");
    setParentId(nextParentId);
    setConfirmDelete(false);
    setMessage("");
  }

  function changeOpen(next: boolean) {
    if (busy) return;
    if (!next) resetForm();
    onOpenChange(next);
  }

  function startEdit(id: string) {
    const item = findCollection(forest, id);
    if (!item) return;
    setEditingId(item.id);
    setName(item.name);
    setDescription(item.description ?? "");
    setParentId(item.parentId ?? "");
    setConfirmDelete(false);
    setMessage("");
  }

  function startChild() {
    if (!editing) return;
    const depth = entries.find((entry) => entry.collection.id === editing.id)?.depth ?? 1;
    if (depth >= 5) { setMessage("集合最多嵌套 5 层，当前集合下不能继续创建子集合。"); return; }
    resetForm(editing.id);
  }

  async function save() {
    if (!name.trim()) { setMessage("集合名称不能为空。"); return; }
    setBusy(true);
    setMessage("");
    try {
      const input: CollectionInput = { name: name.trim(), description: description.trim() || undefined, parentId: parentId || null };
      if (editingId) await props.onUpdateCollection(editingId, input);
      else await props.onCreateCollection(input);
      const createdAsChild = !editingId && Boolean(parentId);
      resetForm(parentId);
      setMessage(editingId ? "集合修改已保存。" : createdAsChild ? "子集合已创建。" : "集合已创建。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "集合保存失败");
    } finally { setBusy(false); }
  }

  async function remove() {
    if (!editingId) return;
    if (!confirmDelete) { setConfirmDelete(true); return; }
    setBusy(true);
    setMessage("");
    try {
      await props.onDeleteCollection(editingId);
      resetForm();
      setMessage("集合已删除；子集合已提升到上一级，文献本身仍保留。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "集合删除失败");
    } finally { setBusy(false); }
  }

  return (
    <Dialog.Root open={open} onOpenChange={changeOpen}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content organizer-dialog" aria-describedby="organizer-description">
          <div className="dialog-head">
            <div><Dialog.Title>管理集合</Dialog.Title><Dialog.Description id="organizer-description">最多嵌套 5 层；删除集合会把子集合提升到上一级，不会删除其中的 PDF。</Dialog.Description></div>
            <Dialog.Close className="icon-button" aria-label="关闭"><X size={17} /></Dialog.Close>
          </div>
          <div className="organizer-toolbar"><button type="button" className="secondary-button" onClick={() => resetForm()} disabled={busy}><Plus size={15} />新建顶层集合</button>{editing && <button type="button" className="secondary-button" onClick={startChild} disabled={busy}><FolderPlus size={15} />在此新建子集合</button>}</div>
          <div className="organizer-workspace">
            <div className="organizer-tree-panel">
              {collections.length ? <CollectionTree collections={forest} selectedId={editingId ?? ""} onSelect={startEdit} allCount={0} includeAll={false} disabled={busy} label="已有集合" /> : <p className="collection-picker-empty">还没有集合。</p>}
            </div>
            <div className="organizer-form">
              <div className="organizer-form-head"><strong>{editingId ? "编辑集合" : parentId ? "新建子集合" : "新建顶层集合"}</strong>{editingId && <button className="text-button" onClick={() => resetForm()} disabled={busy}>取消编辑</button>}</div>
              <label><span>名称</span><input aria-label="名称" value={name} onChange={(event) => setName(event.target.value)} maxLength={200} /></label>
              <label><span>说明（可选）</span><input aria-label="说明（可选）" value={description} onChange={(event) => setDescription(event.target.value)} maxLength={2000} /></label>
              <CollectionSelect collections={forest} value={parentId} onChange={setParentId} label="父集合" allowRoot disabled={busy} disabledIds={disabledParentIds} />
              <div className="organizer-form-actions"><button className="primary-button" onClick={() => void save()} disabled={busy}><Plus size={14} />{busy ? "正在保存" : editingId ? "保存修改" : "创建"}</button>{editingId && <button className={confirmDelete ? "danger-button confirmed" : "danger-button"} onClick={() => void remove()} disabled={busy}><Trash2 size={14} />{confirmDelete ? `确认删除 ${editing?.name ?? "集合"}` : "删除集合"}</button>}</div>
            </div>
          </div>
          {message && <p className={message.includes("不能") || message.includes("失败") || message.includes("为空") ? "form-note organizer-message error" : "form-note organizer-message"} role="status">{message}</p>}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
