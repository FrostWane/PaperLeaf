"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { Check, ExternalLink, Plus, Search, X } from "lucide-react";
import { useState } from "react";
import { getDataSource } from "@/lib/data-source";
import { arxivResults } from "@/lib/fixtures";
import type { ArxivResult } from "@/lib/types";

export function DiscoverView() {
  const [query, setQuery] = useState("retrieval augmented generation");
  const [selected, setSelected] = useState<ArxivResult | null>(null);
  const [imported, setImported] = useState<string[]>([]);
  const [results, setResults] = useState(arxivResults);
  const [message, setMessage] = useState("");

  async function search() {
    setMessage("正在搜索 arXiv…");
    try { setResults(await getDataSource().searchArxiv(query)); setMessage(""); }
    catch (error) { setMessage(error instanceof Error ? error.message : "搜索失败"); }
  }

  async function confirmImport() {
    if (!selected) return;
    try { await getDataSource().importArxiv(selected.id); setImported((items) => [...items, selected.id]); setSelected(null); }
    catch (error) { setMessage(error instanceof Error ? error.message : "导入失败"); setSelected(null); }
  }

  return (
    <div className="discover-layout">
      <section>
        <div className="discover-search"><Search size={18} /><input aria-label="搜索 arXiv" value={query} onChange={(event) => setQuery(event.target.value)} /><button className="primary-button" onClick={search}>搜索 arXiv</button></div>
        <div className="source-note" role="status"><span>来源</span>{message || "仅搜索 arXiv 开放论文；导入前会再次确认。"}</div>
        <div className="result-list">{results.map((paper) => <article key={paper.id}><div className="result-index">ARXIV:{paper.id}</div><h2>{paper.title}</h2><p className="result-meta">{paper.authors} · {paper.year}</p><p>{paper.summary}</p><div className="result-actions"><a href={`https://arxiv.org/abs/${paper.id}`}>查看来源 <ExternalLink size={14} /></a><button className={imported.includes(paper.id) ? "secondary-button imported" : "primary-button"} disabled={imported.includes(paper.id)} onClick={() => setSelected(paper)}>{imported.includes(paper.id) ? <><Check size={15} />已加入队列</> : <><Plus size={15} />导入文献库</>}</button></div></article>)}</div>
      </section>
      <aside className="discover-side"><span className="eyebrow">Search boundary</span><h3>联网不是无边界浏览</h3><p>PaperLeaf 只访问允许的 arXiv 域名，并在重定向后重新校验 PDF 类型。</p><dl><div><dt>候选结果</dt><dd>{results.length}</dd></div><div><dt>自动导入</dt><dd>关闭</dd></div><div><dt>重复检查</dt><dd>arXiv ID + 哈希</dd></div></dl></aside>
      <Dialog.Root open={Boolean(selected)} onOpenChange={(open) => !open && setSelected(null)}><Dialog.Portal><Dialog.Overlay className="dialog-overlay" /><Dialog.Content className="dialog-content"><div className="dialog-head"><div><Dialog.Title>确认导入</Dialog.Title><Dialog.Description>PaperLeaf 将从 arXiv 下载开放 PDF，并加入你的私有文献库。</Dialog.Description></div><Dialog.Close className="icon-button" aria-label="关闭"><X size={17} /></Dialog.Close></div><div className="confirm-paper"><span className="mono">ARXIV:{selected?.id}</span><strong>{selected?.title}</strong><p>{selected?.authors}</p></div><div className="dialog-actions"><Dialog.Close asChild><button className="secondary-button">取消</button></Dialog.Close><button className="primary-button" onClick={confirmImport}>确认导入</button></div></Dialog.Content></Dialog.Portal></Dialog.Root>
    </div>
  );
}
