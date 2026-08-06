import type { PaperCollection } from "./types";

export interface CollectionTreeEntry {
  collection: PaperCollection;
  depth: number;
}

function cloneCollection(collection: PaperCollection, parentId: string | null): PaperCollection {
  return {
    ...collection,
    parentId: collection.parentId ?? parentId,
    paperIds: [...collection.paperIds],
    children: collection.children.map((child) => cloneCollection(child, collection.id)),
  };
}

/** 同时兼容后端返回平铺集合或嵌套 children，并清除重复节点。 */
export function collectionForest(collections: PaperCollection[]): PaperCollection[] {
  const records = new Map<string, PaperCollection>();
  const parentHints = new Map<string, string | null>();

  function visit(collection: PaperCollection, nestedParent: string | null) {
    if (!records.has(collection.id)) records.set(collection.id, cloneCollection(collection, nestedParent));
    const effectiveParent = collection.parentId ?? nestedParent;
    parentHints.set(collection.id, effectiveParent);
    for (const child of collection.children) visit(child, collection.id);
  }

  for (const collection of collections) visit(collection, null);
  const childrenByParent = new Map<string | null, PaperCollection[]>();
  for (const [id, record] of records) {
    const parentId = parentHints.get(id) ?? null;
    const normalized = { ...record, parentId, children: [] };
    const bucket = childrenByParent.get(parentId) ?? [];
    bucket.push(normalized);
    childrenByParent.set(parentId, bucket);
  }

  function attach(collection: PaperCollection, ancestors: Set<string>): PaperCollection {
    if (ancestors.has(collection.id)) return { ...collection, parentId: null, children: [] };
    const nextAncestors = new Set(ancestors).add(collection.id);
    const children = (childrenByParent.get(collection.id) ?? [])
      .sort((left, right) => left.name.localeCompare(right.name, "zh-CN"))
      .map((child) => attach(child, nextAncestors));
    const recursiveIds = new Set(collection.paperIds);
    for (const child of children) {
      for (const paperId of recursivePaperIds(child)) recursiveIds.add(paperId);
    }
    return {
      ...collection,
      children,
      recursivePaperCount: collection.recursivePaperCount || recursiveIds.size,
    };
  }

  const roots = [...(childrenByParent.get(null) ?? [])];
  // 数据异常时仍让孤立节点可见，避免管理界面静默丢失集合。
  for (const collection of records.values()) {
    const parentId = parentHints.get(collection.id);
    if (parentId && !records.has(parentId)) roots.push({ ...collection, parentId: null, children: [] });
  }
  return roots
    .sort((left, right) => left.name.localeCompare(right.name, "zh-CN"))
    .map((root) => attach(root, new Set()));
}

export function flattenCollections(collections: PaperCollection[]): CollectionTreeEntry[] {
  const entries: CollectionTreeEntry[] = [];
  function visit(items: PaperCollection[], depth: number) {
    for (const collection of items) {
      entries.push({ collection, depth });
      visit(collection.children, depth + 1);
    }
  }
  visit(collectionForest(collections), 1);
  return entries;
}

export function recursivePaperIds(collection: PaperCollection): string[] {
  const ids = new Set(collection.paperIds);
  for (const child of collection.children) for (const paperId of recursivePaperIds(child)) ids.add(paperId);
  return [...ids];
}

export function collectionDescendantIds(collection: PaperCollection): string[] {
  return collection.children.flatMap((child) => [child.id, ...collectionDescendantIds(child)]);
}

export function findCollection(collections: PaperCollection[], id: string): PaperCollection | undefined {
  return flattenCollections(collections).find((entry) => entry.collection.id === id)?.collection;
}

export function formatIsoDate(value?: string): string {
  if (!value) return "";
  const isoDate = value.match(/^\d{4}-\d{2}-\d{2}/)?.[0];
  if (isoDate) return isoDate;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return "";
  return parsed.toISOString().slice(0, 10);
}
