"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { AlignLeft, ArrowLeft, ChevronLeft, ChevronRight, FileText, Info, MessageSquare, Network, PencilLine, Quote, Search, Send } from "lucide-react";
import dynamic from "next/dynamic";
import { useEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { Group, Panel, Separator } from "react-resizable-panels";
import { z } from "zod";
import { demoDataSource, getDataSource } from "@/lib/data-source";
import { groundedAnswer, papers } from "@/lib/fixtures";
import { useWorkspaceStore, type MobilePane } from "@/lib/store";
import type { AgentAnswer, Paper, PaperStructureGraph, PaperSummary, PaperUpdateInput } from "@/lib/types";
import { PaperDetailsDialog } from "./paper-details-dialog";
import { StructureDiagram } from "./structure-diagram";

const RealPdfDocument = dynamic(
  () => import("./real-pdf-document").then((module) => module.RealPdfDocument),
  { ssr: false, loading: () => <p role="status">正在准备 PDF 阅读器…</p> },
);

const questionSchema = z.object({ question: z.string().trim().min(3, "问题至少需要 3 个字符").max(500) });
type QuestionInput = z.infer<typeof questionSchema>;
type AssistantView = "ask" | "summary" | "structure";

const tabItems: { id: MobilePane; label: string; icon: typeof FileText }[] = [
  { id: "pdf", label: "论文", icon: FileText },
  { id: "ask", label: "提问", icon: MessageSquare },
  { id: "info", label: "信息", icon: Info },
];

const assistantTabs: { id: AssistantView; label: string; icon: typeof MessageSquare }[] = [
  { id: "ask", label: "问答", icon: MessageSquare },
  { id: "summary", label: "概览", icon: AlignLeft },
  { id: "structure", label: "结构", icon: Network },
];

function ResizeSeparator({ label }: { label: string }) {
  return <Separator className="resize-handle" aria-label={label} />;
}

function PaperState({ paper }: { paper: Paper }) {
  if (paper.status === "ready") return <span className="status-pill ready"><span>✓</span>已建立索引</span>;
  if (paper.status === "partial") return <span className="status-pill partial"><span>!</span>部分可用</span>;
  if (paper.status === "failed") return <span className="status-pill failed"><span>×</span>处理失败</span>;
  if (paper.status === "deleting") return <span className="status-pill neutral"><span>…</span>正在删除</span>;
  return <span className="status-pill indexing"><span className="spinner" />正在建立索引</span>;
}

export function PaperWorkspace({ paperId = "attention", demo = false, initialPage }: { paperId?: string; demo?: boolean; initialPage?: number }) {
  const isReal = !demo && process.env.NEXT_PUBLIC_DATA_MODE === "real";
  const dataSource = demo ? demoDataSource : getDataSource();
  const fallbackPaper = papers.find((item) => item.id === paperId) ?? papers[0];
  const [paper, setPaper] = useState<Paper | null>(isReal ? null : fallbackPaper);
  const [currentPage, setCurrentPage] = useState(Math.max(1, initialPage ?? (demo ? 2 : 1)));
  const [mobilePane, setMobilePane] = useState<MobilePane>("pdf");
  const [assistantView, setAssistantView] = useState<AssistantView>("ask");
  const [answer, setAnswer] = useState<AgentAnswer | null>(isReal ? null : groundedAnswer);
  const [summary, setSummary] = useState<PaperSummary | null>(null);
  const [structure, setStructure] = useState<PaperStructureGraph | null>(null);
  const [busy, setBusy] = useState<"ask" | "summary" | "structure" | null>(null);
  const [loadMessage, setLoadMessage] = useState("");
  const [askMessage, setAskMessage] = useState("");
  const [artifactMessage, setArtifactMessage] = useState("");
  const [manageMessage, setManageMessage] = useState("");
  const [detailsOpen, setDetailsOpen] = useState(false);
  const workspaceRef = useRef<HTMLDivElement>(null);
  const setSelectedPaperId = useWorkspaceStore((state) => state.setSelectedPaperId);
  const { register, handleSubmit, reset, formState: { errors } } = useForm<QuestionInput>({ resolver: zodResolver(questionSchema), defaultValues: { question: "" } });
  const libraryHref = demo ? "/library?demo=1" : "/library";

  useEffect(() => {
    if (!isReal) return;
    let stopped = false;
    async function load() {
      try {
        const next = await dataSource.getPaper(paperId);
        if (stopped) return;
        setPaper(next);
        setLoadMessage("");
      } catch (error) {
        if (!stopped) setLoadMessage(error instanceof Error ? error.message : "文献信息读取失败");
      }
    }
    void load();
    return () => { stopped = true; };
  }, [dataSource, isReal, paperId]);

  useEffect(() => {
    if (!isReal || (paper?.status !== "indexing" && paper?.status !== "deleting")) return;
    let stopped = false;
    const timer = window.setInterval(() => {
      void dataSource.getPaper(paperId).then((next) => {
        if (!stopped) { setPaper(next); setLoadMessage(""); }
      }).catch((error: unknown) => {
        if (!stopped) setLoadMessage(error instanceof Error ? error.message : "处理状态刷新失败");
      });
    }, 3_000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [dataSource, isReal, paper?.status, paperId]);

  useEffect(() => {
    setSelectedPaperId(paperId);
    const group = document.getElementById(demo ? "demo-workspace" : "paper-workspace");
    const separators = group?.querySelectorAll<HTMLElement>("[data-separator]");
    const values = [{ min: 16, max: 28, now: 20 }, { min: 25, max: 39, now: 31 }];
    separators?.forEach((element, index) => {
      const value = values[index];
      if (!value) return;
      element.setAttribute("aria-valuemin", String(value.min));
      element.setAttribute("aria-valuemax", String(value.max));
      element.setAttribute("aria-valuenow", String(value.now));
    });
    workspaceRef.current?.setAttribute("data-client-ready", "true");
  }, [demo, paperId, setSelectedPaperId]);

  useEffect(() => {
    void dataSource.recordPaperOpened(paperId).then(() => {
      window.dispatchEvent(new Event("paperleaf:papers-changed"));
    }).catch(() => {
      // 最近阅读是辅助信息；记录失败不应打断 PDF 阅读与问答主流程。
    });
  }, [dataSource, paperId]);

  function openCitation(page: number) {
    setCurrentPage(Math.max(1, Math.min(paper?.pages || page, page)));
    setMobilePane("pdf");
  }

  async function submit(values: QuestionInput) {
    setBusy("ask");
    setAskMessage("");
    try { setAnswer(await dataSource.ask(values.question, [paperId])); reset(); }
    catch (error) { setAskMessage(error instanceof Error ? error.message : "提问失败"); }
    finally { setBusy(null); }
  }

  async function generateSummary() {
    setBusy("summary");
    setArtifactMessage("");
    try { setSummary(await dataSource.summarizePaper(paperId)); }
    catch (error) { setArtifactMessage(error instanceof Error ? error.message : "论文总结生成失败"); }
    finally { setBusy(null); }
  }

  async function generateStructure() {
    setBusy("structure");
    setArtifactMessage("");
    try { setStructure(await dataSource.buildStructureGraph(paperId)); }
    catch (error) { setArtifactMessage(error instanceof Error ? error.message : "结构图生成失败"); }
    finally { setBusy(null); }
  }

  async function updatePaper(input: PaperUpdateInput) {
    const updated = await dataSource.updatePaper(paperId, input);
    setPaper(updated);
    window.dispatchEvent(new Event("paperleaf:papers-changed"));
  }

  async function retryPaper() {
    const updated = await dataSource.retryPaper(paperId);
    setPaper(updated);
    window.dispatchEvent(new Event("paperleaf:papers-changed"));
  }

  async function deletePaper() {
    await dataSource.deletePaper(paperId);
    window.dispatchEvent(new Event("paperleaf:papers-changed"));
    if (isReal) { window.location.assign("/library"); return; }
    setPaper((current) => current ? { ...current, status: "deleting" } : current);
    setDetailsOpen(false);
    setManageMessage("演示模式已模拟删除；固定文献不会从公开 Demo 中移除。");
  }

  if (!paper) {
    return <div ref={workspaceRef} className="paper-workspace workspace-loading" data-client-ready="true"><FileText size={22} /><strong>{loadMessage ? "无法打开论文" : "正在载入论文工作台"}</strong><p role={loadMessage ? "alert" : "status"}>{loadMessage || "正在读取元数据与私有 PDF…"}</p><a className="secondary-button" href={libraryHref}>返回文献库</a></div>;
  }

  const pageShortcuts = paper.pages ? [...new Set([1, 2, Math.ceil(paper.pages / 2), paper.pages])].filter((page) => page > 0 && page <= paper.pages) : [1];

  const evidencePages = [...new Set([
    ...(answer?.citations.map((item) => item.page) ?? []),
    ...(summary?.citations.map((item) => item.physicalPage) ?? []),
    ...(structure?.nodes.map((item) => item.physicalPage) ?? []),
  ])];
  const readyForArtifacts = paper.status === "ready" || paper.status === "partial";

  const infoPane = <aside className={`workspace-info pane-view ${mobilePane === "info" ? "mobile-active" : ""}`} aria-label="论文信息">
    <div className="pane-heading"><a href={libraryHref} className="back-link"><ArrowLeft size={14} />返回文献库</a><button className="icon-button pane-settings" aria-label="编辑文献信息" onClick={() => setDetailsOpen(true)}><PencilLine size={14} /></button></div>
    <div className="paper-summary"><span className="paper-index">{isReal ? `PL–${paper.id.slice(0, 8).toUpperCase()}` : "PL–001"}</span><h2>{paper.title}</h2><p>{paper.authors || "作者待识别"}{paper.year > 0 ? ` · ${paper.year}` : ""}</p><PaperState paper={paper} /></div>
    <dl className="metadata"><div><dt>来源</dt><dd>{paper.venue}</dd></div><div><dt>页数</dt><dd>{paper.pages ? `${paper.pages} 页` : "待识别"}</dd></div><div><dt>{paper.arxivId ? "arXiv" : "DOI"}</dt><dd className="mono">{paper.arxivId ?? paper.doi ?? "—"}</dd></div></dl>
    <div className="outline-list"><span className="eyebrow">{isReal ? "页码导航" : "论文目录"}</span>{isReal ? pageShortcuts.map((page) => <button key={page} className={currentPage === page ? "active" : ""} onClick={() => setCurrentPage(page)}><span>{String(page).padStart(2, "0")}</span>{page === 1 ? "论文首页" : page === paper.pages ? "最后一页" : `跳到第 ${page} 页`}</button>) : ["摘要", "1. Introduction", "2. Background", "3. Model Architecture", "4. Why Self-Attention", "5. Training", "6. Results"].map((item, index) => <button key={item} className={currentPage === index + 1 ? "active" : ""} onClick={() => setCurrentPage(index + 1)}><span>{String(index + 1).padStart(2, "0")}</span>{item}</button>)}</div>
    <div className="paper-pane-actions"><button className="secondary-button" onClick={() => setDetailsOpen(true)}><PencilLine size={14} />文献设置</button>{manageMessage && <p role="status">{manageMessage}</p>}</div>
  </aside>;

  const readerPane = <section className={`workspace-reader pane-view ${mobilePane === "pdf" ? "mobile-active" : ""}`} aria-label="PDF 阅读器">
    <div className="reader-toolbar"><div><button className="icon-button" aria-label="在文内搜索" disabled><Search size={15} /></button></div><div className="page-control"><button className="icon-button" aria-label="上一页" disabled={currentPage <= 1} onClick={() => setCurrentPage(Math.max(1, currentPage - 1))}><ChevronLeft size={16} /></button><span className="mono">{String(currentPage).padStart(2, "0")} / {paper.pages || "—"}</span><button className="icon-button" aria-label="下一页" disabled={Boolean(paper.pages && currentPage >= paper.pages)} onClick={() => setCurrentPage(Math.min(paper.pages || currentPage + 1, currentPage + 1))}><ChevronRight size={16} /></button></div><span className="mono muted">适合宽度</span></div>
    <div className="document-stage" tabIndex={0} aria-label="PDF 页面，可滚动浏览">{isReal ? <RealPdfDocument url={dataSource.fileUrl(paperId)} page={currentPage} onPageCount={(count) => { setPaper((item) => item ? { ...item, pages: count } : item); if (currentPage > count) setCurrentPage(count); }} /> : <article className="mock-paper" aria-label={`模拟 PDF 第 ${currentPage} 页`}><div className="pdf-running"><span>NEURIPS 2017</span><span>ARXIV:{paper.arxivId}</span></div><h1>{paper.title}</h1><p className="pdf-authors">Ashish Vaswani · Noam Shazeer · Niki Parmar · Jakob Uszkoreit · Llion Jones · Aidan N. Gomez</p><h2>{currentPage === 2 ? "1 · Introduction" : `Section · Page ${currentPage}`}</h2><p>The dominant sequence transduction models are based on complex recurrent or convolutional neural networks that include an encoder and a decoder.</p><p className={currentPage === 2 ? "pdf-highlight" : ""}>We propose a new simple network architecture, the Transformer, based solely on attention mechanisms, dispensing with recurrence and convolutions entirely.</p><p>Recurrent models typically factor computation along the symbol positions of the input and output sequences. This inherently sequential nature precludes parallelization within training examples.</p><div className="formula">Attention(Q, K, V) = softmax(QKᵀ / √dₖ)V</div><p>Self-attention connects all positions with a constant number of sequentially executed operations.</p><span className="pdf-page-number">{currentPage}</span></article>}<div className="citation-rail" aria-hidden="true">{evidencePages.map((page) => <span key={page} className={page === currentPage ? "active" : ""} />)}</div></div>
    <div className="reader-status"><strong>{evidencePages.includes(currentPage) ? "证据页已定位" : "论文页面"}</strong><span>第 {currentPage} 页</span><span>{isReal ? "原始 PDF" : "模拟文本层"}</span></div>
  </section>;

  const askContent = <div className="conversation">
    {answer ? <><div className={`run-note ${answer.evidenceQuality?.grade === "insufficient" ? "quality-insufficient" : ""}`} role="status">{answer.evidenceQuality?.summary ?? `已保留 ${answer.citations.length} 条可验证证据`}</div><span className="eyebrow">你的问题</span><p className="question-text">{answer.question}</p><span className="eyebrow">{answer.evidenceQuality?.grade === "insufficient" ? "证据状态" : "基于原文回答"}</span><p className="answer-text">{answer.answer} {answer.citations.map((citation, index) => <button key={citation.id} className="inline-citation" onClick={() => openCitation(citation.page)} aria-label={`查看第 ${citation.page} 页引用`}>[{index + 1}]</button>)}</p>{answer.citations.length > 0 && <div className="citation-list" aria-label="回答引用">{answer.citations.map((citation, index) => <button className="citation-row" key={citation.id} onClick={() => openCitation(citation.page)}><span className="citation-no">{String(index + 1).padStart(2, "0")}</span><q>{citation.quote}</q><span className="citation-page">PDF {String(citation.page).padStart(2, "0")}</span></button>)}</div>}</> : <div className="assistant-empty"><Quote size={19} /><strong>从原文开始提问</strong><p>回答只使用当前论文中已完成索引的内容，并附上可回读的物理页码。</p></div>}
    {askMessage && <p className="field-error" role="alert">{askMessage}</p>}
  </div>;

  const summaryContent = <div className="artifact-panel">
    <div className="artifact-heading"><div><span className="eyebrow">Grounded overview</span><h3>证据化论文概览</h3><p>围绕研究问题、方法、结果与限制整理；不会补写证据中不存在的内容。</p></div><button className="secondary-button" disabled={!readyForArtifacts || Boolean(busy)} onClick={() => void generateSummary()}>{busy === "summary" ? "正在生成" : summary ? "重新生成" : "生成概览"}</button></div>
    {!summary && <div className="artifact-empty"><AlignLeft size={20} /><strong>{readyForArtifacts ? "尚未生成概览" : "等待论文完成索引"}</strong><p>{readyForArtifacts ? "生成后，点击页码可以回到对应 PDF 证据。" : "PDF 仍可阅读；总结功能会在索引就绪后开放。"}</p></div>}
    {summary && <article className="summary-artifact"><div className="artifact-mode"><span>{summary.mode === "model" ? "模型归纳" : "提取式降级"}</span><em>{summary.citations.length} 个证据页</em></div><p>{summary.content}</p><div className="artifact-citations">{summary.citations.map((citation, index) => <button key={citation.chunkId} onClick={() => openCitation(citation.physicalPage)}><span>{String(index + 1).padStart(2, "0")}</span>PDF {citation.physicalPage}<small className="mono">{citation.chunkId}</small></button>)}</div></article>}
    {artifactMessage && <p className="field-error" role="alert">{artifactMessage}</p>}
  </div>;

  const structureContent = <div className="artifact-panel">
    <div className="artifact-heading"><div><span className="eyebrow">Evidence map</span><h3>论文结构图</h3><p>结构节点保留原始 Chunk 和物理页，图形只表达已抽取证据之间的关系。</p></div><button className="secondary-button" disabled={!readyForArtifacts || Boolean(busy)} onClick={() => void generateStructure()}>{busy === "structure" ? "正在构建" : structure ? "重新构建" : "构建结构"}</button></div>
    {!structure && <div className="artifact-empty"><Network size={20} /><strong>{readyForArtifacts ? "尚未构建结构图" : "等待论文完成索引"}</strong><p>{readyForArtifacts ? "结构图使用 Mermaid strict mode，并提供可键盘访问的证据目录。" : "索引完成后再提取论文结构。"}</p></div>}
    {structure && <StructureDiagram key={structure.mermaid} graph={structure} onOpenPage={openCitation} />}
    {artifactMessage && <p className="field-error" role="alert">{artifactMessage}</p>}
  </div>;

  const askPane = <aside className={`workspace-assistant pane-view ${mobilePane === "ask" ? "mobile-active" : ""}`} aria-label="论文助手">
    <div className="assistant-head"><span className="assistant-title"><Quote size={14} />论文助手</span><nav className="assistant-tabs" aria-label="论文助手视图">{assistantTabs.map(({ id, label, icon: Icon }) => <button key={id} className={assistantView === id ? "active" : ""} aria-current={assistantView === id ? "page" : undefined} onClick={() => { setAssistantView(id); setArtifactMessage(""); }}><Icon size={13} />{label}</button>)}</nav></div>
    {assistantView === "ask" ? askContent : assistantView === "summary" ? summaryContent : structureContent}
    {assistantView === "ask" && <form className="composer" onSubmit={handleSubmit(submit)}><label className="composer-box"><span className="sr-only">向论文提问</span><textarea {...register("question")} onKeyDown={(event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); void handleSubmit(submit)(); } }} placeholder="继续追问这篇论文…" rows={2} /><button className="send-button" disabled={busy === "ask" || !readyForArtifacts} aria-label="发送问题"><Send size={15} /></button></label>{errors.question && <span className="field-error">{errors.question.message}</span>}<div className="composer-meta"><span>{readyForArtifacts ? "仅依据当前论文回答" : "等待索引完成后可提问"}</span><span>{busy === "ask" ? "正在检索…" : "Ctrl + Enter"}</span></div></form>}
  </aside>;

  return (
    <div ref={workspaceRef} className="paper-workspace" data-client-ready="false">
      <nav className="mobile-workspace-tabs" aria-label="移动端工作区">{tabItems.map(({ id, label, icon: Icon }) => <button key={id} onClick={() => setMobilePane(id)} className={mobilePane === id ? "active" : ""} aria-current={mobilePane === id ? "page" : undefined}><Icon size={15} />{label}<span className="sr-only">{mobilePane === id ? "，当前视图" : ""}</span></button>)}</nav>
      <div className="workspace-desktop"><Group orientation="horizontal" id={demo ? "demo-workspace" : "paper-workspace"}><Panel id="info" defaultSize="20%" minSize="16%" maxSize="28%">{infoPane}</Panel><ResizeSeparator label="调整论文信息栏宽度" /><Panel id="reader" defaultSize="49%" minSize="34%">{readerPane}</Panel><ResizeSeparator label="调整论文助手栏宽度" /><Panel id="assistant" defaultSize="31%" minSize="25%" maxSize="39%">{askPane}</Panel></Group></div>
      <div className="workspace-mobile">{infoPane}{readerPane}{askPane}</div>
      <PaperDetailsDialog paper={paper} open={detailsOpen} onOpenChange={setDetailsOpen} onSave={updatePaper} onDelete={deletePaper} onRetry={retryPaper} />
    </div>
  );
}
