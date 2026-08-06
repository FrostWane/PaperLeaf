"use client";

import { flattenCollections } from "@/lib/collections";
import type { PaperCollection } from "@/lib/types";

interface CollectionSelectProps {
  collections: PaperCollection[];
  value: string;
  onChange: (value: string) => void;
  label: string;
  placeholder?: string;
  allowRoot?: boolean;
  rootLabel?: string;
  disabledIds?: Set<string>;
  disabled?: boolean;
}

export function CollectionSelect({ collections, value, onChange, label, placeholder = "选择集合…", allowRoot = false, rootLabel = "全部文献（顶层）", disabledIds = new Set(), disabled = false }: CollectionSelectProps) {
  const entries = flattenCollections(collections);
  return (
    <label className="collection-select-field">
      <span>{label}</span>
      <select aria-label={label} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
        {allowRoot ? <option value="">{rootLabel}</option> : <option value="">{placeholder}</option>}
        {entries.map(({ collection, depth }) => <option key={collection.id} value={collection.id} disabled={disabledIds.has(collection.id)}>{`${"　".repeat(Math.max(0, depth - 1))}${depth > 1 ? "└ " : ""}${collection.name}（${collection.recursivePaperCount}）`}</option>)}
      </select>
    </label>
  );
}
