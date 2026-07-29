"use client";

import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { createColumnHelper, flexRender, getCoreRowModel, getSortedRowModel, type SortingState, useReactTable } from "@tanstack/react-table";
import { Archive, ArchiveRestore, ArrowUpDown, Check, ChevronRight, FileText, Folder, FolderCog, FolderPlus, Hash, Search, SlidersHorizontal, Tags, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { demoDataSource, getDataSource } from "@/lib/data-source";
import type { BulkPaperAction, CollectionInput, Paper, TagInput } from "@/lib/types";
import { LibraryOrganizerDialog } from "./library-organizer-dialog";

type LibraryScope = "all" | "recent" | "unorganized" | "archived";
type StatusFilter = "all" | "ready" | "processing" | "attention";
const helper = createColumnHelper<Paper>();
const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 15_000, retry: 1 } } });

function PaperState({ paper }: { paper: Paper }) {
  if (paper.archivedAt) return <span className="status-pill neutral"><Archive size={11} />已归档</span>;
  if (paper.status === "ready") return <span className="status-pill ready"><span>✓</span>可提问</span>;
  if (paper.status === "indexing") return <span className="status-pill indexing"><span className="spinner" />{paper.progress === undefined ? "正在索引" : `索引 ${paper.progress}%`}</span>;
  if (paper.status === "partial") return <span className="status-pill partial"><span>!</span>部分可用</span>;
  if (paper.status === "deleting") return <span className="status-pill neutral"><span>…</span>正在删除</span>;
  return <span className="status-pill failed"><span>×</span>处理失败</span>;
}

function SelectionBox({ checked, indeterminate = false, label, onChange }: { checked: boolean; indeterminate?: boolean; label: string; onChange: (checked: boolean) => void }) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { if (ref.current) ref.current.indeterminate = indeterminate; }, [indeterminate]);
  return <input ref={ref} className="row-checkbox" type="checkbox" checked={checked} aria-label={label} onChange={(event) => onChange(event.target.checked)} />;
}

function LibraryTableContent({ demo }: { demo: boolean }) {
  const dataSource = useMemo(() => demo ? demoDataSource : getDataSource(), [demo]);
  const dataMode = demo ? "demo" : "real";
  const [query, setQuery] = useState("");
  const [sorting, setSorting] = useState<SortingState>([{ id: "year", desc: true }]);
  const [scope, setScope] = useState<LibraryScope>("all");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [yearFilter, setYearFilter] = useState("all");
  const [collectionFilter, setCollectionFilter] = useState("all");
  const [tagFilter, setTagFilter] = useState("all");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [organizerOpen, setOrganizerOpen] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkCollectionId, setBulkCollectionId] = useState("");
  const [bulkTagId, setBulkTagId] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const papersQuery = useQuery({ queryKey: ["papers", dataMode], queryFn: () => dataSource.listPapers(), refetchInterval: (state) => state.state.data?.some((paper) => paper.status === "indexing" || paper.status === "deleting") ? 3_000 : false });
  const collectionsQuery = useQuery({ queryKey: ["collections", dataMode], queryFn: () => dataSource.listCollections() });
  const tagsQuery = useQuery({ queryKey: ["tags", dataMode], queryFn: () => dataSource.listTags() });
  const papers = useMemo(() => papersQuery.data ?? [], [papersQuery.data]);
  const collections = useMemo(() => collectionsQuery.data ?? [], [collectionsQuery.data]);
  const tags = useMemo(() => tagsQuery.data ?? [], [tagsQuery.data]);

  useEffect(() => {
    const refresh = () => { void papersQuery.refetch(); };
    window.addEventListener("paperleaf:papers-changed", refresh);
    return () => window.removeEventListener("paperleaf:papers-changed", refresh);
  }, [papersQuery]);

  useEffect(() => { setSelectedIds(new Set()); setMessage(""); }, [scope, statusFilter, yearFilter, collectionFilter, tagFilter]);

  const enrichedPapers = useMemo(() => papers.map((paper) => ({
    ...paper,
    tags: tags.filter((tag) => tag.paperIds.includes(paper.id)).map((tag) => tag.name),
  })), [papers, tags]);

  const collectionIdsByPaper = useMemo(() => {
    const result = new Map<string, string[]>();
    for (const collection of collections) for (const paperId of collection.paperIds) result.set(paperId, [...(result.get(paperId) ?? []), collection.id]);
    return result;
  }, [collections]);
  const tagIdsByPaper = useMemo(() => {
    const result = new Map<string, string[]>();
    for (const tag of tags) for (const paperId of tag.paperIds) result.set(paperId, [...(result.get(paperId) ?? []), tag.id]);
    return result;
  }, [tags]);

  const inScope = useCallback((paper: Paper, target: LibraryScope): boolean => {
    if (target === "archived") return Boolean(paper.archivedAt);
    if (paper.archivedAt) return false;
    if (target === "recent") return Boolean(paper.lastOpenedAt);
    if (target === "unorganized") return !(collectionIdsByPaper.get(paper.id)?.length) || !(tagIdsByPaper.get(paper.id)?.length);
    return true;
  }, [collectionIdsByPaper, tagIdsByPaper]);

  const scopeCounts = useMemo(() => ({
    all: enrichedPapers.filter((paper) => inScope(paper, "all")).length,
    recent: enrichedPapers.filter((paper) => inScope(paper, "recent")).length,
    unorganized: enrichedPapers.filter((paper) => inScope(paper, "unorganized")).length,
    archived: enrichedPapers.filter((paper) => inScope(paper, "archived")).length,
  }), [enrichedPapers, inScope]);

  const filteredPapers = useMemo(() => enrichedPapers.filter((paper) => {
    if (!inScope(paper, scope)) return false;
    const normalized = query.trim().toLocaleLowerCase();
    if (normalized && !`${paper.title} ${paper.authors} ${paper.venue} ${paper.tags.join(" ")}`.toLocaleLowerCase().includes(normalized)) return false;
    if (statusFilter === "ready" && paper.status !== "ready") return false;
    if (statusFilter === "processing" && paper.status !== "indexing") return false;
    if (statusFilter === "attention" && paper.status !== "partial" && paper.status !== "failed") return false;
    if (yearFilter !== "all" && paper.year !== Number(yearFilter)) return false;
    if (collectionFilter !== "all" && !collectionIdsByPaper.get(paper.id)?.includes(collectionFilter)) return false;
    if (tagFilter !== "all" && !tagIdsByPaper.get(paper.id)?.includes(tagFilter)) return false;
    return true;
  }), [collectionFilter, collectionIdsByPaper, enrichedPapers, inScope, query, scope, statusFilter, tagFilter, tagIdsByPaper, yearFilter]);

  const visibleIds = useMemo(() => filteredPapers.map((paper) => paper.id), [filteredPapers]);
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selectedIds.has(id));
  const someVisibleSelected = visibleIds.some((id) => selectedIds.has(id)) && !allVisibleSelected;
  const years = Array.from(new Set(enrichedPapers.map((paper) => paper.year))).sort((a, b) => b - a);

  const toggleSelected = useCallback((id: string, checked: boolean) => {
    setSelectedIds((current) => { const next = new Set(current); if (checked) next.add(id); else next.delete(id); return next; });
  }, []);

  const toggleAll = useCallback((checked: boolean) => {
    setSelectedIds((current) => { const next = new Set(current); for (const id of visibleIds) { if (checked) next.add(id); else next.delete(id); } return next; });
  }, [visibleIds]);

  async function refreshOrganization() {
    await Promise.all([papersQuery.refetch(), collectionsQuery.refetch(), tagsQuery.refetch()]);
  }

  async function runBulk(action: BulkPaperAction, targetId?: string) {
    const ids = Array.from(selectedIds);
    if (!ids.length) return;
    setBusy(true);
    setMessage("");
    try {
      await dataSource.bulkPapers({ paperIds: ids, action, targetId });
      await refreshOrganization();
      setSelectedIds(new Set());
      setMessage(action === "archive" ? `已归档 ${ids.length} 篇文献。` : action === "unarchive" ? `已恢复 ${ids.length} 篇文献。` : `已整理 ${ids.length} 篇文献。`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "批量整理失败");
    } finally { setBusy(false); }
  }

  async function mutateOrganizer(task: () => Promise<unknown>) {
    await task();
    await Promise.all([collectionsQuery.refetch(), tagsQuery.refetch(), papersQuery.refetch()]);
  }

  const columns = useMemo(() => [
    helper.display({ id: "select", header: () => <SelectionBox checked={allVisibleSelected} indeterminate={someVisibleSelected} label="选择当前筛选中的全部文献" onChange={toggleAll} />, cell: ({ row }) => <SelectionBox checked={selectedIds.has(row.original.id)} label={`选择 ${row.original.title}`} onChange={(checked) => toggleSelected(row.original.id, checked)} /> }),
    helper.accessor("title", { header: "论文", cell: ({ row }) => <a className="paper-cell" href={`/library/${row.original.id}${demo ? "?demo=1" : ""}`}><span className="paper-icon"><FileText size={16} /></span><span><strong>{row.original.title}</strong><small>{row.original.authors} · {row.original.venue}{row.original.lastOpenedAt ? ` · 最近阅读 ${new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(new Date(row.original.lastOpenedAt))}` : ""}</small><span className="mobile-paper-state"><PaperState paper={row.original} /></span></span></a> }),
    helper.accessor("year", { header: "年份", cell: (info) => <span className="mono">{info.getValue()}</span> }),
    helper.accessor("tags", { header: "标签", enableSorting: false, cell: (info) => info.getValue().length ? <div className="tag-list">{info.getValue().slice(0, 2).map((tag) => <span key={tag}>{tag}</span>)}</div> : <span className="table-muted">未标记</span> }),
    helper.accessor("status", { header: "状态", enableSorting: false, cell: ({ row }) => <PaperState paper={row.original} /> }),
    helper.display({ id: "open", header: "", cell: ({ row }) => <a className="row-open" aria-label={`打开 ${row.original.title}`} href={`/library/${row.original.id}${demo ? "?demo=1" : ""}`}><ChevronRight size={17} /></a> }),
  ], [allVisibleSelected, demo, selectedIds, someVisibleSelected, toggleAll, toggleSelected]);
  const table = useReactTable({ data: filteredPapers, columns, state: { sorting }, onSortingChange: setSorting, getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel() });
  const loading = papersQuery.isPending || collectionsQuery.isPending || tagsQuery.isPending;
  const error = papersQuery.isError || collectionsQuery.isError || tagsQuery.isError;

  return (
    <>
      <div className="page-lead library-lead">
        <div><h2>你的研究文献</h2><p>用集合建立主题边界，用标签记录阅读语境；所有数量都来自当前文献库。</p></div>
        <div className="collection-tabs" role="tablist" aria-label="文献范围">
          {([ ["all", "全部文献"], ["recent", "最近阅读"], ["unorganized", "待整理"], ["archived", "已归档"] ] as const).map(([id, label]) => <button role="tab" aria-selected={scope === id} className={scope === id ? "active" : ""} key={id} onClick={() => setScope(id)}>{label}<span>{scopeCounts[id]}</span></button>)}
        </div>
      </div>
      <section className="library-frame" aria-label="文献库">
        <aside className="organization-rail" aria-label="集合和标签">
          <div className="organization-head"><span>组织</span><button className="icon-button" aria-label="管理集合和标签" onClick={() => setOrganizerOpen(true)}><FolderCog size={15} /></button></div>
          <div className="organization-section"><span>集合</span><button className={collectionFilter === "all" ? "active" : ""} onClick={() => setCollectionFilter("all")}><Folder size={14} /><span>全部集合</span></button>{collections.map((item) => <button className={collectionFilter === item.id ? "active" : ""} key={item.id} onClick={() => setCollectionFilter(item.id)}><Folder size={14} /><span>{item.name}</span><small>{item.paperIds.length}</small></button>)}</div>
          <div className="organization-section tag-section"><span>标签</span><button className={tagFilter === "all" ? "active" : ""} onClick={() => setTagFilter("all")}><Hash size={14} /><span>全部标签</span></button>{tags.map((item) => <button className={tagFilter === item.id ? "active" : ""} key={item.id} onClick={() => setTagFilter(item.id)}><i style={{ backgroundColor: item.color ?? "#AFC3CE" }} aria-hidden="true" /><span>{item.name}</span><small>{item.paperIds.length}</small></button>)}</div>
        </aside>
        <div className="library-surface organization-table">
          <div className="library-tools">
            <label className="search-field"><Search size={16} /><span className="sr-only">搜索文献</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题、作者或标签" /></label>
            <button className={filtersOpen ? "secondary-button active" : "secondary-button"} aria-expanded={filtersOpen} onClick={() => setFiltersOpen((value) => !value)}><SlidersHorizontal size={15} />筛选</button>
            <button className="secondary-button mobile-organizer-button" onClick={() => setOrganizerOpen(true)}><FolderCog size={15} />组织</button>
            <span className="result-count">{filteredPapers.length} 篇文献</span>
          </div>
          <div className="mobile-organization-filters">
            <label><span>集合</span><select value={collectionFilter} onChange={(event) => setCollectionFilter(event.target.value)}><option value="all">全部集合</option>{collections.map((item) => <option key={item.id} value={item.id}>{item.name}（{item.paperIds.length}）</option>)}</select></label>
            <label><span>标签</span><select value={tagFilter} onChange={(event) => setTagFilter(event.target.value)}><option value="all">全部标签</option>{tags.map((item) => <option key={item.id} value={item.id}>{item.name}（{item.paperIds.length}）</option>)}</select></label>
          </div>
          {filtersOpen && <div className="library-filter-panel"><fieldset><legend>处理状态</legend>{([ ["all", "全部"], ["ready", "可提问"], ["processing", "处理中"], ["attention", "需关注"] ] as const).map(([id, label]) => <button className={statusFilter === id ? "active" : ""} key={id} onClick={() => setStatusFilter(id)}><Check size={12} />{label}</button>)}</fieldset><label><span>年份</span><select value={yearFilter} onChange={(event) => setYearFilter(event.target.value)}><option value="all">全部年份</option>{years.map((year) => <option value={year} key={year}>{year}</option>)}</select></label><button className="text-button" onClick={() => { setStatusFilter("all"); setYearFilter("all"); setCollectionFilter("all"); setTagFilter("all"); }}>清除筛选</button></div>}
          {selectedIds.size > 0 && <div className="bulk-bar" role="region" aria-label="批量整理"><strong>{selectedIds.size} 篇已选</strong><label><span className="sr-only">选择集合</span><select value={bulkCollectionId} onChange={(event) => setBulkCollectionId(event.target.value)}><option value="">选择集合…</option>{collections.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><button className="secondary-button" disabled={!bulkCollectionId || busy} onClick={() => void runBulk("add_collection", bulkCollectionId)}><FolderPlus size={14} />加入集合</button><label><span className="sr-only">选择标签</span><select value={bulkTagId} onChange={(event) => setBulkTagId(event.target.value)}><option value="">选择标签…</option>{tags.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><button className="secondary-button" disabled={!bulkTagId || busy} onClick={() => void runBulk("add_tag", bulkTagId)}><Tags size={14} />添加标签</button>{collectionFilter !== "all" && <button className="text-button" disabled={busy} onClick={() => void runBulk("remove_collection", collectionFilter)}>移出当前集合</button>}{tagFilter !== "all" && <button className="text-button" disabled={busy} onClick={() => void runBulk("remove_tag", tagFilter)}>移除当前标签</button>}<button className="secondary-button archive-action" disabled={busy} onClick={() => void runBulk(scope === "archived" ? "unarchive" : "archive")}>{scope === "archived" ? <ArchiveRestore size={14} /> : <Archive size={14} />}{scope === "archived" ? "恢复" : "归档"}</button><button className="icon-button" aria-label="清除选择" onClick={() => setSelectedIds(new Set())}><X size={15} /></button></div>}
          {message && <p className="library-message" role="status">{message}</p>}
          {loading && <div className="table-message" role="status">正在整理文献…</div>}
          {error && <div className="table-message error" role="alert">文献与组织信息暂时无法读取，请稍后重试。</div>}
          {!loading && !error && table.getRowModel().rows.length === 0 && <div className="table-message"><strong>{papers.length ? "没有匹配的论文" : "文献库还是空的"}</strong><span>{papers.length ? "调整范围、组织或筛选条件后再试。" : "上传第一篇 PDF，解析完成后就可以按原文提问。"}</span></div>}
          {!loading && !error && table.getRowModel().rows.length > 0 && <div className="table-scroll"><table className="data-table library-data-table"><thead>{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => <th key={header.id}>{header.column.getCanSort() ? <button className="sortable" onClick={header.column.getToggleSortingHandler()}>{flexRender(header.column.columnDef.header, header.getContext())}<ArrowUpDown size={12} /></button> : flexRender(header.column.columnDef.header, header.getContext())}</th>)}</tr>)}</thead><tbody>{table.getRowModel().rows.map((row) => <tr className={selectedIds.has(row.original.id) ? "selected" : ""} key={row.id}>{row.getVisibleCells().map((cell) => <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody></table></div>}
        </div>
      </section>
      <LibraryOrganizerDialog open={organizerOpen} onOpenChange={setOrganizerOpen} collections={collections} tags={tags} onCreateCollection={(input: CollectionInput) => mutateOrganizer(() => dataSource.createCollection(input))} onUpdateCollection={(id: string, input: CollectionInput) => mutateOrganizer(() => dataSource.updateCollection(id, input))} onDeleteCollection={(id: string) => mutateOrganizer(() => dataSource.deleteCollection(id))} onCreateTag={(input: TagInput) => mutateOrganizer(() => dataSource.createTag(input))} onUpdateTag={(id: string, input: TagInput) => mutateOrganizer(() => dataSource.updateTag(id, input))} onDeleteTag={(id: string) => mutateOrganizer(() => dataSource.deleteTag(id))} />
    </>
  );
}

export function LibraryTable({ demo = false }: { demo?: boolean }) {
  return <QueryClientProvider client={queryClient}><LibraryTableContent demo={demo} /></QueryClientProvider>;
}
