"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { createColumnHelper, flexRender, getCoreRowModel, getPaginationRowModel, getSortedRowModel, type PaginationState, type SortingState, useReactTable } from "@tanstack/react-table";
import { Archive, ArchiveRestore, ArrowUpDown, Check, ChevronLeft, ChevronRight, FileText, FolderCog, FolderPlus, RotateCcw, Search, SlidersHorizontal, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { demoDataSource, getDataSource } from "@/lib/data-source";
import { collectionForest, findCollection, flattenCollections, formatIsoDate, recursivePaperIds } from "@/lib/collections";
import type { BulkPaperAction, CollectionInput, Paper } from "@/lib/types";
import { CollectionSelect } from "./collection-select";
import { CollectionTree } from "./collection-tree";
import { LibraryOrganizerDialog } from "./library-organizer-dialog";
import { PaperCollectionsDialog } from "./paper-collections-dialog";

type LibraryScope = "all" | "recent" | "unorganized" | "archived";
type StatusFilter = "all" | "ready" | "processing" | "attention";
const helper = createColumnHelper<Paper>();
const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 15_000, retry: 1 } } });
const LIBRARY_PAGE_SIZE = 20;

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
  const [pagination, setPagination] = useState<PaginationState>({ pageIndex: 0, pageSize: LIBRARY_PAGE_SIZE });
  const [scope, setScope] = useState<LibraryScope>("all");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [yearFilter, setYearFilter] = useState("all");
  const [collectionFilter, setCollectionFilter] = useState("all");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [organizerOpen, setOrganizerOpen] = useState(false);
  const [organizingPaper, setOrganizingPaper] = useState<Paper | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkCollectionId, setBulkCollectionId] = useState("");
  const [reindexConfirmOpen, setReindexConfirmOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const papersQuery = useQuery({ queryKey: ["papers", dataMode, "all"], queryFn: () => dataSource.listPapers(), refetchInterval: (state) => state.state.data?.some((paper) => paper.status === "indexing" || paper.status === "deleting") ? 3_000 : false });
  const collectionsQuery = useQuery({ queryKey: ["collections", dataMode], queryFn: () => dataSource.listCollections() });
  const usesServerCollectionScope = !demo && collectionFilter !== "all" && collectionFilter !== "unorganized";
  const scopedPapersQuery = useQuery({ queryKey: ["papers", dataMode, "collection", collectionFilter], queryFn: () => dataSource.listPapers({ collectionId: collectionFilter }), enabled: usesServerCollectionScope });
  const papers = useMemo(() => papersQuery.data ?? [], [papersQuery.data]);
  const collections = useMemo(() => collectionForest(collectionsQuery.data ?? []), [collectionsQuery.data]);
  const flatCollections = useMemo(() => flattenCollections(collections).map(({ collection }) => collection), [collections]);

  useEffect(() => {
    const refresh = () => {
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["papers", dataMode] }),
        queryClient.invalidateQueries({ queryKey: ["collections", dataMode] }),
      ]);
    };
    window.addEventListener("paperleaf:papers-changed", refresh);
    return () => window.removeEventListener("paperleaf:papers-changed", refresh);
  }, [dataMode]);

  useEffect(() => { setSelectedIds(new Set()); setMessage(""); }, [scope, statusFilter, yearFilter, collectionFilter]);

  const collectionIdsByPaper = useMemo(() => {
    const result = new Map<string, string[]>();
    for (const collection of flatCollections) for (const paperId of collection.paperIds) result.set(paperId, [...(result.get(paperId) ?? []), collection.id]);
    return result;
  }, [flatCollections]);
  const collectionNamesByPaper = useMemo(() => {
    const result = new Map<string, string[]>();
    for (const collection of flatCollections) for (const paperId of collection.paperIds) result.set(paperId, [...(result.get(paperId) ?? []), collection.name]);
    return result;
  }, [flatCollections]);
  const selectedCollectionPaperIds = useMemo(() => {
    if (collectionFilter === "all" || collectionFilter === "unorganized") return null;
    const selected = findCollection(collections, collectionFilter);
    return new Set(selected ? recursivePaperIds(selected) : []);
  }, [collectionFilter, collections]);

  const inScope = useCallback((paper: Paper, target: LibraryScope): boolean => {
    if (target === "archived") return Boolean(paper.archivedAt);
    if (paper.archivedAt) return false;
    if (target === "recent") return Boolean(paper.lastOpenedAt);
    if (target === "unorganized") return !(collectionIdsByPaper.get(paper.id)?.length);
    return true;
  }, [collectionIdsByPaper]);

  const scopeCounts = useMemo(() => ({
    all: papers.filter((paper) => inScope(paper, "all")).length,
    recent: papers.filter((paper) => inScope(paper, "recent")).length,
    unorganized: papers.filter((paper) => inScope(paper, "unorganized")).length,
    archived: papers.filter((paper) => inScope(paper, "archived")).length,
  }), [papers, inScope]);

  const filteredPapers = useMemo(() => {
    const candidatePapers = usesServerCollectionScope ? (scopedPapersQuery.data ?? []) : papers;
    return candidatePapers.filter((paper) => {
    if (!inScope(paper, scope)) return false;
    const normalized = query.trim().toLocaleLowerCase();
    if (normalized && !`${paper.title} ${paper.authors} ${paper.publication}`.toLocaleLowerCase().includes(normalized)) return false;
    if (statusFilter === "ready" && paper.status !== "ready") return false;
    if (statusFilter === "processing" && paper.status !== "indexing") return false;
    if (statusFilter === "attention" && paper.status !== "partial" && paper.status !== "failed") return false;
    if (yearFilter !== "all" && paper.year !== Number(yearFilter)) return false;
    if (collectionFilter === "unorganized" && collectionIdsByPaper.get(paper.id)?.length) return false;
    if (demo && selectedCollectionPaperIds && !selectedCollectionPaperIds.has(paper.id)) return false;
      return true;
    });
  }, [collectionFilter, collectionIdsByPaper, demo, inScope, papers, query, scope, scopedPapersQuery.data, selectedCollectionPaperIds, statusFilter, usesServerCollectionScope, yearFilter]);

  const visibleIds = useMemo(() => filteredPapers.map((paper) => paper.id), [filteredPapers]);
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selectedIds.has(id));
  const someVisibleSelected = visibleIds.some((id) => selectedIds.has(id)) && !allVisibleSelected;
  const years = Array.from(new Set(papers.map((paper) => paper.year))).sort((a, b) => b - a);

  useEffect(() => {
    setPagination((current) => current.pageIndex === 0 ? current : { ...current, pageIndex: 0 });
  }, [collectionFilter, query, scope, sorting, statusFilter, yearFilter]);

  const pageCount = Math.max(1, Math.ceil(filteredPapers.length / LIBRARY_PAGE_SIZE));
  useEffect(() => {
    setPagination((current) => current.pageIndex < pageCount
      ? current
      : { ...current, pageIndex: pageCount - 1 });
  }, [pageCount]);

  const toggleSelected = useCallback((id: string, checked: boolean) => {
    setSelectedIds((current) => { const next = new Set(current); if (checked) next.add(id); else next.delete(id); return next; });
  }, []);

  const toggleAll = useCallback((checked: boolean) => {
    setSelectedIds((current) => { const next = new Set(current); for (const id of visibleIds) { if (checked) next.add(id); else next.delete(id); } return next; });
  }, [visibleIds]);

  async function refreshOrganization() {
    const [, collectionResult] = await Promise.all([papersQuery.refetch(), collectionsQuery.refetch(), usesServerCollectionScope ? scopedPapersQuery.refetch() : Promise.resolve(null)]);
    return collectionResult.data ?? [];
  }

  async function runBulk(action: BulkPaperAction, targetId?: string) {
    const ids = Array.from(selectedIds);
    if (!ids.length) return false;
    setBusy(true);
    setMessage("");
    try {
      const result = await dataSource.bulkPapers({ paperIds: ids, action, targetId });
      await refreshOrganization();
      setSelectedIds(new Set());
      if (action === "reindex") {
        const skipped = ids.length - result.affected;
        setMessage(`已将 ${result.affected} 篇文献加入重新识别与索引队列${skipped > 0 ? `，跳过 ${skipped} 篇正在处理的文献` : ""}。`);
      } else {
        setMessage(action === "archive" ? `已归档 ${ids.length} 篇文献。` : action === "unarchive" ? `已恢复 ${ids.length} 篇文献。` : `已整理 ${ids.length} 篇文献。`);
      }
      return true;
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "批量操作失败");
      return false;
    } finally { setBusy(false); }
  }

  async function confirmBulkReindex() {
    const succeeded = await runBulk("reindex");
    if (succeeded) setReindexConfirmOpen(false);
  }

  async function mutateOrganizer(task: () => Promise<unknown>) {
    await task();
    await Promise.all([collectionsQuery.refetch(), papersQuery.refetch(), usesServerCollectionScope ? scopedPapersQuery.refetch() : Promise.resolve(null)]);
  }

  async function savePaperCollections(paperId: string, nextIds: string[]) {
    const currentIds = collectionIdsByPaper.get(paperId) ?? [];
    const additions = nextIds.filter((id) => !currentIds.includes(id));
    const removals = currentIds.filter((id) => !nextIds.includes(id));
    const operations = [
      ...additions.map((targetId) => ({ action: "add_collection" as const, targetId })),
      ...removals.map((targetId) => ({ action: "remove_collection" as const, targetId })),
    ];
    const results = await Promise.allSettled(operations.map(({ action, targetId }) => dataSource.bulkPapers({ paperIds: [paperId], action, targetId })));
    const refreshedCollections = await refreshOrganization();
    const failed = results.flatMap((result, index) => result.status === "rejected" ? [flatCollections.find((item) => item.id === operations[index].targetId)?.name ?? "未知集合"] : []);
    const selectedIds = flattenCollections(refreshedCollections).filter(({ collection }) => collection.paperIds.includes(paperId)).map(({ collection }) => collection.id);
    if (failed.length > 0) return { selectedIds, error: `部分集合保存失败：${failed.join("、")}。已重新读取服务器中的实际结果。` };
    setMessage(`已更新《${papers.find((paper) => paper.id === paperId)?.title ?? "论文"}》的集合。`);
    return { selectedIds };
  }

  const columns = useMemo(() => [
    helper.display({ id: "select", header: () => <SelectionBox checked={allVisibleSelected} indeterminate={someVisibleSelected} label="选择当前筛选中的全部文献" onChange={toggleAll} />, cell: ({ row }) => <SelectionBox checked={selectedIds.has(row.original.id)} label={`选择 ${row.original.title}`} onChange={(checked) => toggleSelected(row.original.id, checked)} /> }),
    helper.accessor("title", { header: "论文", cell: ({ row }) => <a className="paper-cell" href={`/library/${row.original.id}${demo ? "?demo=1" : ""}`}><span className="paper-icon"><FileText size={16} /></span><span><strong>{row.original.title}</strong><small>{row.original.authors}{row.original.lastOpenedAt ? ` · 最近阅读 ${formatIsoDate(row.original.lastOpenedAt)}` : ""}</small><span className="mobile-paper-state"><PaperState paper={row.original} /></span></span></a> }),
    helper.display({ id: "collections", header: "集合", cell: ({ row }) => { const names = collectionNamesByPaper.get(row.original.id) ?? []; return names.length ? <div className="collection-chip-list">{names.slice(0, 2).map((name) => <span key={name}>{name}</span>)}{names.length > 2 && <small>+{names.length - 2}</small>}</div> : <span className="table-muted">未归类</span>; } }),
    helper.accessor("publication", { header: "出版物", cell: (info) => info.getValue() ? <span>{info.getValue()}</span> : <span className="table-muted">待识别</span> }),
    helper.accessor("year", { header: "年份", cell: (info) => <span className="mono">{info.getValue()}</span> }),
    helper.accessor("status", { header: "状态", enableSorting: false, cell: ({ row }) => <PaperState paper={row.original} /> }),
    helper.display({ id: "open", header: "", cell: ({ row }) => <div className="row-actions"><button className="row-open" aria-label={`管理 ${row.original.title} 的集合`} title="管理集合" onClick={() => setOrganizingPaper(row.original)}><FolderCog size={15} /></button><a className="row-open" aria-label={`打开 ${row.original.title}`} href={`/library/${row.original.id}${demo ? "?demo=1" : ""}`}><ChevronRight size={17} /></a></div> }),
  ], [allVisibleSelected, collectionNamesByPaper, demo, selectedIds, someVisibleSelected, toggleAll, toggleSelected]);
  const table = useReactTable({
    data: filteredPapers,
    columns,
    state: { sorting, pagination },
    onSortingChange: setSorting,
    onPaginationChange: setPagination,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
  });
  const loading = papersQuery.isPending || collectionsQuery.isPending || (usesServerCollectionScope && scopedPapersQuery.isPending);
  const error = papersQuery.isError || collectionsQuery.isError || (usesServerCollectionScope && scopedPapersQuery.isError);

  return (
    <>
      <div className="page-lead library-lead">
        <div><h2>你的研究文献</h2></div>
        <div className="collection-tabs" role="tablist" aria-label="文献范围">
          {([ ["all", "全部文献"], ["recent", "最近阅读"], ["unorganized", "待整理"], ["archived", "已归档"] ] as const).map(([id, label]) => <button role="tab" aria-selected={scope === id} className={scope === id ? "active" : ""} key={id} onClick={() => setScope(id)}>{label}<span>{scopeCounts[id]}</span></button>)}
        </div>
      </div>
      <section className="library-frame" aria-label="文献库">
        <aside className="organization-rail" aria-label="集合">
          <div className="organization-head"><span>集合</span><button className="icon-button" aria-label="管理集合" onClick={() => setOrganizerOpen(true)}><FolderCog size={15} /></button></div>
          <CollectionTree collections={collections} selectedId={collectionFilter} onSelect={setCollectionFilter} allCount={scopeCounts.all} unorganizedCount={scopeCounts.unorganized} includeUnorganized />
        </aside>
        <div className="library-surface organization-table">
          <div className="library-tools">
            <label className="search-field"><Search size={16} /><span className="sr-only">搜索文献</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题、作者或出版物" /></label>
            <button className={filtersOpen ? "secondary-button active" : "secondary-button"} aria-expanded={filtersOpen} onClick={() => setFiltersOpen((value) => !value)}><SlidersHorizontal size={15} />筛选</button>
            <button className="secondary-button mobile-organizer-button" onClick={() => setOrganizerOpen(true)}><FolderCog size={15} />组织</button>
            <span className="result-count">{filteredPapers.length} 篇文献</span>
          </div>
          <div className="mobile-organization-filters"><CollectionSelect collections={collections} value={collectionFilter === "all" || collectionFilter === "unorganized" ? "" : collectionFilter} onChange={(value) => setCollectionFilter(value || "all")} label="集合" placeholder="全部文献" /></div>
          {filtersOpen && <div className="library-filter-panel"><fieldset><legend>处理状态</legend>{([ ["all", "全部"], ["ready", "可提问"], ["processing", "处理中"], ["attention", "需关注"] ] as const).map(([id, label]) => <button className={statusFilter === id ? "active" : ""} key={id} onClick={() => setStatusFilter(id)}><Check size={12} />{label}</button>)}</fieldset><label><span>年份</span><select value={yearFilter} onChange={(event) => setYearFilter(event.target.value)}><option value="all">全部年份</option>{years.map((year) => <option value={year} key={year}>{year}</option>)}</select></label><button className="text-button" onClick={() => { setStatusFilter("all"); setYearFilter("all"); setCollectionFilter("all"); }}>清除筛选</button></div>}
          {selectedIds.size > 0 && <div className="bulk-bar" role="region" aria-label="批量操作"><strong>{selectedIds.size} 篇已选</strong><CollectionSelect collections={collections} value={bulkCollectionId} onChange={setBulkCollectionId} label="选择集合" /><button className="secondary-button" disabled={!bulkCollectionId || busy} onClick={() => void runBulk("add_collection", bulkCollectionId)}><FolderPlus size={14} />加入集合</button>{collectionFilter !== "all" && collectionFilter !== "unorganized" && <button className="text-button" disabled={busy} onClick={() => void runBulk("remove_collection", collectionFilter)}>移出当前集合</button>}<button className="secondary-button" disabled={busy} onClick={() => setReindexConfirmOpen(true)}><RotateCcw size={14} />重新识别与索引</button><button className="secondary-button archive-action" disabled={busy} onClick={() => void runBulk(scope === "archived" ? "unarchive" : "archive")}>{scope === "archived" ? <ArchiveRestore size={14} /> : <Archive size={14} />}{scope === "archived" ? "恢复" : "归档"}</button><button className="icon-button" aria-label="清除选择" onClick={() => setSelectedIds(new Set())}><X size={15} /></button></div>}
          {message && <p className="library-message" role="status">{message}</p>}
          {loading && <div className="table-message" role="status">正在整理文献…</div>}
          {error && <div className="table-message error" role="alert">文献与组织信息暂时无法读取，请稍后重试。</div>}
          {!loading && !error && table.getRowModel().rows.length === 0 && <div className="table-message"><strong>{papers.length ? "没有匹配的论文" : "文献库还是空的"}</strong><span>{papers.length ? "调整范围、组织或筛选条件后再试。" : "上传第一篇 PDF，解析完成后就可以按原文提问。"}</span></div>}
          {!loading && !error && table.getRowModel().rows.length > 0 && <><div className="table-scroll"><table className="data-table library-data-table"><thead>{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => <th key={header.id}>{header.column.getCanSort() ? <button className="sortable" onClick={header.column.getToggleSortingHandler()}>{flexRender(header.column.columnDef.header, header.getContext())}<ArrowUpDown size={12} /></button> : flexRender(header.column.columnDef.header, header.getContext())}</th>)}</tr>)}</thead><tbody>{table.getRowModel().rows.map((row) => <tr className={selectedIds.has(row.original.id) ? "selected" : ""} key={row.id}>{row.getVisibleCells().map((cell) => <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody></table></div>{filteredPapers.length > LIBRARY_PAGE_SIZE && <nav className="library-pagination" aria-label="文献库分页"><button type="button" className="secondary-button" disabled={!table.getCanPreviousPage()} onClick={() => table.previousPage()}><ChevronLeft size={16} />上一页</button><span aria-live="polite">第 {pagination.pageIndex + 1} / {table.getPageCount()} 页 · 共 {filteredPapers.length} 篇</span><button type="button" className="secondary-button" disabled={!table.getCanNextPage()} onClick={() => table.nextPage()}>下一页<ChevronRight size={16} /></button></nav>}</>}
        </div>
      </section>
      <LibraryOrganizerDialog open={organizerOpen} onOpenChange={setOrganizerOpen} collections={collections} onCreateCollection={(input: CollectionInput) => mutateOrganizer(() => dataSource.createCollection(input))} onUpdateCollection={(id: string, input: CollectionInput) => mutateOrganizer(() => dataSource.updateCollection(id, input))} onDeleteCollection={(id: string) => mutateOrganizer(() => dataSource.deleteCollection(id))} />
      {organizingPaper && <PaperCollectionsDialog paper={organizingPaper} collections={collections} open onOpenChange={(next) => { if (!next) setOrganizingPaper(null); }} onSave={savePaperCollections} />}
      <Dialog.Root open={reindexConfirmOpen} onOpenChange={(open) => { if (!busy) setReindexConfirmOpen(open); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay" />
          <Dialog.Content className="dialog-content" aria-describedby="bulk-reindex-description">
            <div className="dialog-head"><div><Dialog.Title>重新处理 {selectedIds.size} 篇文献</Dialog.Title><Dialog.Description id="bulk-reindex-description">将从已保存的 PDF 重新识别文献信息并更新检索内容，不会产生重复文献。</Dialog.Description></div><Dialog.Close className="icon-button" aria-label="关闭" disabled={busy}><X size={17} /></Dialog.Close></div>
            <p className="bulk-reindex-warning">已有译文将失效，概览和研究脑图会在下次使用时重新生成；问答历史不会被删除。</p>
            <div className="dialog-actions"><Dialog.Close asChild><button type="button" className="secondary-button" disabled={busy}>取消</button></Dialog.Close><button type="button" className="primary-button" disabled={busy || selectedIds.size === 0} onClick={() => void confirmBulkReindex()}><RotateCcw size={15} />{busy ? "正在加入队列…" : "确认重新处理"}</button></div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  );
}

export function LibraryTable({ demo = false }: { demo?: boolean }) {
  return <QueryClientProvider client={queryClient}><LibraryTableContent demo={demo} /></QueryClientProvider>;
}
