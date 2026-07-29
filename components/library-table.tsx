"use client";

import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { createColumnHelper, flexRender, getCoreRowModel, getFilteredRowModel, getSortedRowModel, type SortingState, useReactTable } from "@tanstack/react-table";
import { ArrowUpDown, ChevronRight, FileText, Search, SlidersHorizontal } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { getDataSource } from "@/lib/data-source";
import type { Paper } from "@/lib/types";

const helper = createColumnHelper<Paper>();

function PaperState({ paper }: { paper: Paper }) {
  if (paper.status === "ready") return <span className="status-pill ready"><span>✓</span>可提问</span>;
  if (paper.status === "indexing") return <span className="status-pill indexing"><span className="spinner" />{paper.progress === undefined ? "正在索引" : `索引 ${paper.progress}%`}</span>;
  if (paper.status === "partial") return <span className="status-pill partial"><span>!</span>部分可用</span>;
  if (paper.status === "deleting") return <span className="status-pill neutral"><span>…</span>正在删除</span>;
  return <span className="status-pill failed"><span>×</span>处理失败</span>;
}

const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, retry: 1 } } });

function LibraryTableContent() {
  const [query, setQuery] = useState("");
  const [sorting, setSorting] = useState<SortingState>([{ id: "year", desc: true }]);
  const { data = [], isPending, isError, refetch } = useQuery({
    queryKey: ["papers"],
    queryFn: () => getDataSource().listPapers(),
    refetchInterval: (queryState) => queryState.state.data?.some((paper) => paper.status === "indexing" || paper.status === "deleting") ? 3_000 : false,
  });
  useEffect(() => {
    const refresh = () => { void refetch(); };
    window.addEventListener("paperleaf:papers-changed", refresh);
    return () => window.removeEventListener("paperleaf:papers-changed", refresh);
  }, [refetch]);
  const columns = useMemo(() => [
    helper.accessor("title", { header: "论文", cell: ({ row }) => <a className="paper-cell" href={`/library/${row.original.id}`}><span className="paper-icon"><FileText size={16} /></span><span><strong>{row.original.title}</strong><small>{row.original.authors} · {row.original.venue}</small></span></a> }),
    helper.accessor("year", { header: "年份", cell: (info) => <span className="mono">{info.getValue()}</span> }),
    helper.accessor("tags", { header: "标签", enableSorting: false, cell: (info) => <div className="tag-list">{info.getValue().slice(0, 2).map((tag) => <span key={tag}>{tag}</span>)}</div> }),
    helper.accessor("status", { header: "状态", enableSorting: false, cell: ({ row }) => <PaperState paper={row.original} /> }),
    helper.display({ id: "open", header: "", cell: ({ row }) => <a className="row-open" aria-label={`打开 ${row.original.title}`} href={`/library/${row.original.id}`}><ChevronRight size={17} /></a> }),
  ], []);
  const table = useReactTable({ data, columns, state: { globalFilter: query, sorting }, onSortingChange: setSorting, onGlobalFilterChange: setQuery, getCoreRowModel: getCoreRowModel(), getFilteredRowModel: getFilteredRowModel(), getSortedRowModel: getSortedRowModel() });

  return (
    <section className="library-surface" aria-label="文献列表">
      <div className="library-tools"><label className="search-field"><Search size={16} /><span className="sr-only">搜索文献</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题、作者或标签" /></label><button className="secondary-button"><SlidersHorizontal size={15} />筛选</button><span className="result-count">{table.getFilteredRowModel().rows.length} 篇文献</span></div>
      {isPending && <div className="table-message" role="status">正在整理文献…</div>}
      {isError && <div className="table-message error" role="alert">暂时无法读取文献，请稍后重试。</div>}
      {!isPending && !isError && table.getRowModel().rows.length === 0 && <div className="table-message"><strong>{data.length ? "没有匹配的论文" : "文献库还是空的"}</strong><span>{data.length ? "换一个关键词，或清除搜索条件。" : "上传第一篇 PDF，解析完成后就可以按原文提问。"}</span></div>}
      {!isPending && !isError && table.getRowModel().rows.length > 0 && <div className="table-scroll"><table className="data-table"><thead>{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => <th key={header.id}><button className={header.column.getCanSort() ? "sortable" : "plain-head"} onClick={header.column.getToggleSortingHandler()} disabled={!header.column.getCanSort()}>{flexRender(header.column.columnDef.header, header.getContext())}{header.column.getCanSort() && <ArrowUpDown size={12} />}</button></th>)}</tr>)}</thead><tbody>{table.getRowModel().rows.map((row) => <tr key={row.id}>{row.getVisibleCells().map((cell) => <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody></table></div>}
    </section>
  );
}

export function LibraryTable() {
  return <QueryClientProvider client={queryClient}><LibraryTableContent /></QueryClientProvider>;
}
