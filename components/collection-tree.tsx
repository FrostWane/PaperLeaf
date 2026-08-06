"use client";

import { ChevronDown, ChevronRight, FileQuestion, Folder, FolderOpen, Library } from "lucide-react";
import { useMemo, useState } from "react";
import { collectionForest } from "@/lib/collections";
import type { PaperCollection } from "@/lib/types";

interface CollectionTreeProps {
  collections: PaperCollection[];
  selectedId: string;
  onSelect: (collectionId: string) => void;
  allCount: number;
  unorganizedCount?: number;
  disabled?: boolean;
  label?: string;
  includeAll?: boolean;
  includeUnorganized?: boolean;
}

export function CollectionTree({
  collections,
  selectedId,
  onSelect,
  allCount,
  unorganizedCount = 0,
  disabled = false,
  label = "集合树",
  includeAll = true,
  includeUnorganized = false,
}: CollectionTreeProps) {
  const forest = useMemo(() => collectionForest(collections), [collections]);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [rootOpen, setRootOpen] = useState(true);
  const selectedPath = useMemo(() => {
    const ancestors = new Set<string>();
    function findPath(items: PaperCollection[], path: string[]): boolean {
      for (const item of items) {
        if (item.id === selectedId) {
          for (const id of path) ancestors.add(id);
          return true;
        }
        if (findPath(item.children, [...path, item.id])) return true;
      }
      return false;
    }
    return { ancestors, found: findPath(forest, []) };
  }, [forest, selectedId]);
  const visibleRootOpen = rootOpen || (includeAll && selectedPath.found);

  function setOpen(id: string, open: boolean) {
    setExpanded((current) => {
      const next = new Set(current);
      if (open) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  function renderNode(collection: PaperCollection, depth: number) {
    const hasChildren = collection.children.length > 0;
    const open = expanded.has(collection.id) || selectedPath.ancestors.has(collection.id);
    const count = collection.recursivePaperCount;
    return (
      <div className="collection-tree-branch" role="none" key={collection.id}>
        <div className="collection-tree-row" style={{ "--collection-depth": depth } as React.CSSProperties}>
          {hasChildren ? (
            <button type="button" className="collection-tree-toggle" data-testid={`collection-toggle-${collection.id}`} aria-hidden="true" tabIndex={-1} disabled={disabled} onClick={() => setOpen(collection.id, !open)}>
              {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
            </button>
          ) : <span className="collection-tree-toggle-placeholder" aria-hidden="true" />}
          <button
            type="button"
            role="treeitem"
            aria-level={depth}
            aria-expanded={hasChildren ? open : undefined}
            aria-selected={selectedId === collection.id}
            className={selectedId === collection.id ? "collection-tree-item active" : "collection-tree-item"}
            disabled={disabled}
            onClick={() => onSelect(collection.id)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(collection.id); return; }
              if (hasChildren && event.key === "ArrowRight") { event.preventDefault(); setOpen(collection.id, true); }
              if (hasChildren && event.key === "ArrowLeft") { event.preventDefault(); setOpen(collection.id, false); }
            }}
          >
            {open ? <FolderOpen size={15} /> : <Folder size={15} />}
            <span>{collection.name}</span><small>{count}</small>
          </button>
        </div>
        {hasChildren && open && <div role="group">{collection.children.map((child) => renderNode(child, depth + 1))}</div>}
      </div>
    );
  }

  return (
    <div className="collection-tree" role="tree" aria-label={label}>
      {includeAll ? <>
        <div className="collection-tree-row collection-tree-all-row">
          <button type="button" className="collection-tree-toggle" data-testid="collection-toggle-all" aria-hidden="true" tabIndex={-1} disabled={disabled} onClick={() => setRootOpen(!visibleRootOpen)}>{visibleRootOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</button>
          <button type="button" role="treeitem" aria-level={1} aria-expanded={visibleRootOpen} aria-selected={selectedId === "all"} className={selectedId === "all" ? "collection-tree-root active" : "collection-tree-root"} disabled={disabled} onClick={() => onSelect("all")} onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect("all"); }
            if (event.key === "ArrowRight") { event.preventDefault(); setRootOpen(true); }
            if (event.key === "ArrowLeft") { event.preventDefault(); setRootOpen(false); }
          }}><Library size={15} /><span>全部文献</span><small>{allCount}</small></button>
        </div>
        {visibleRootOpen && <div role="group">{forest.map((collection) => renderNode(collection, 2))}{includeUnorganized && <div className="collection-tree-row" style={{ "--collection-depth": 2 } as React.CSSProperties}><span className="collection-tree-toggle-placeholder" aria-hidden="true" /><button type="button" role="treeitem" aria-level={2} aria-selected={selectedId === "unorganized"} className={selectedId === "unorganized" ? "collection-tree-item active" : "collection-tree-item"} disabled={disabled || unorganizedCount === 0} onClick={() => onSelect("unorganized")} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect("unorganized"); } }}><FileQuestion size={15} /><span>待整理</span><small>{unorganizedCount}</small></button></div>}</div>}
      </> : forest.map((collection) => renderNode(collection, 1))}
    </div>
  );
}
