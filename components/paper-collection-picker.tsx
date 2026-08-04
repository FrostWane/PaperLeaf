"use client";

import { Folder } from "lucide-react";
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

  function toggle(collectionId: string, checked: boolean) {
    const next = new Set(selected);
    if (checked) next.add(collectionId);
    else next.delete(collectionId);
    onChange(collections.map((collection) => collection.id).filter((id) => next.has(id)));
  }

  return (
    <fieldset className="paper-collection-picker" disabled={disabled}>
      <legend>{label}</legend>
      {collections.length === 0 ? (
        <p className="collection-picker-empty">{emptyMessage}</p>
      ) : (
        <div className="collection-picker-list">
          {collections.map((collection) => (
            <label className="collection-picker-item" key={collection.id}>
              <input
                type="checkbox"
                checked={selected.has(collection.id)}
                onChange={(event) => toggle(collection.id, event.target.checked)}
              />
              <Folder size={14} aria-hidden="true" />
              <span>
                <strong>{collection.name}</strong>
                <small>{collection.description || `${collection.paperIds.length} 篇文献`}</small>
              </span>
            </label>
          ))}
        </div>
      )}
      <small className="collection-picker-count">已选择 {selectedIds.length} 个集合；一篇论文可以属于多个集合。</small>
    </fieldset>
  );
}
