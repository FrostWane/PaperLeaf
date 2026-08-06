"use client";

import { ChevronDown, ChevronRight, Folder } from "lucide-react";
import { useMemo, useState } from "react";
import { collectionForest, flattenCollections } from "@/lib/collections";
import type { PaperCollection } from "@/lib/types";

interface PaperCollectionPickerProps {
  collections: PaperCollection[];
  selectedIds: string[];
  onChange: (selectedIds: string[]) => void;
  disabled?: boolean;
  label?: string;
  emptyMessage?: string;
}

export function PaperCollectionPicker({
  collections,
  selectedIds,
  onChange,
  disabled = false,
  label = "选择集合",
  emptyMessage = "还没有集合，可先在文献库中创建集合。",
}: PaperCollectionPickerProps) {
  const selected = new Set(selectedIds);
  const forest = useMemo(() => collectionForest(collections), [collections]);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());

  function toggle(collectionId: string, checked: boolean) {
    const next = new Set(selected);
    if (checked) next.add(collectionId);
    else next.delete(collectionId);
    onChange(flattenCollections(collections).map(({ collection }) => collection.id).filter((id) => next.has(id)));
  }

  function setOpen(id: string, open: boolean) {
    setCollapsed((current) => {
      const next = new Set(current);
      if (open) next.delete(id); else next.add(id);
      return next;
    });
  }

  function renderNode(collection: PaperCollection, depth: number): React.ReactNode {
    const hasChildren = collection.children.length > 0;
    const open = !collapsed.has(collection.id);
    return <div className="collection-picker-branch" key={collection.id}>
      <div className="collection-picker-row" style={{ "--collection-depth": depth } as React.CSSProperties}>
        {hasChildren ? <button type="button" className="collection-picker-toggle" aria-label={`${open ? "收起" : "展开"} ${collection.name}`} aria-expanded={open} disabled={disabled} onClick={() => setOpen(collection.id, !open)}>{open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</button> : <span className="collection-picker-toggle-placeholder" aria-hidden="true" />}
        <label className="collection-picker-item">
          <input type="checkbox" checked={selected.has(collection.id)} onChange={(event) => toggle(collection.id, event.target.checked)} />
          <Folder size={14} aria-hidden="true" />
          <span><strong>{collection.name}</strong><small>{collection.description || `${collection.recursivePaperCount} 篇文献（含子集合）`}</small></span>
        </label>
      </div>
      {hasChildren && open && <div className="collection-picker-group">{collection.children.map((child) => renderNode(child, depth + 1))}</div>}
    </div>;
  }

  return (
    <fieldset className="paper-collection-picker" disabled={disabled}>
      <legend>{label}</legend>
      {collections.length === 0 ? (
        <p className="collection-picker-empty">{emptyMessage}</p>
      ) : (
        <div className="collection-picker-list">{forest.map((collection) => renderNode(collection, 1))}</div>
      )}
      <small className="collection-picker-count">已选择 {selectedIds.length} 个集合；一篇论文可以属于多个集合。</small>
    </fieldset>
  );
}
