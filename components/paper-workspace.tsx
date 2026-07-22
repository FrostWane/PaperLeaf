"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { ArrowLeft, ChevronLeft, ChevronRight, FileText, Info, MessageSquare, Quote, Search, Send } from "lucide-react";
import dynamic from "next/dynamic";
import { useEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { Group, Panel, Separator } from "react-resizable-panels";
import { z } from "zod";
import { groundedAnswer, papers } from "@/lib/fixtures";
import { getDataSource } from "@/lib/data-source";
import { useWorkspaceStore, type MobilePane } from "@/lib/store";

const RealPdfDocument = dynamic(
  () => import("./real-pdf-document").then((module) => module.RealPdfDocument),
  { ssr: false, loading: () => <p role="status">正在准备 PDF 阅读器…</p> },
);

const questionSchema = z.object({ question: z.string().trim().min(3, "问题至少需要 3 个字符").max(500) });
type QuestionInput = z.infer<typeof questionSchema>;

const tabItems: { id: MobilePane; label: string; icon: typeof FileText }[] = [
  { id: "pdf", label: "论文", icon: FileText },
  { id: "ask", label: "提问", icon: MessageSquare },
  { id: "info", label: "信息", icon: Info },
];

function ResizeSeparator({ label }: { label: string }) {
  return <Separator className="resize-handle" aria-label={label} />;
}

export function PaperWorkspace({ paperId = "attention", demo = false }: { paperId?: string; demo?: boolean }) {
  const [paper, setPaper] = useState(papers.find((item) => item.id === paperId) ?? papers[0]);
  const [currentPage, setCurrentPage] = useState(2);
  const [mobilePane, setMobilePane] = useState<MobilePane>("pdf");
  const workspaceRef = useRef<HTMLDivElement>(null);
  const setSelectedPaperId = useWorkspaceStore((state) => state.setSelectedPaperId);
  const [answer, setAnswer] = useState(groundedAnswer);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const isReal = !demo && process.env.NEXT_PUBLIC_DATA_MODE === "real";
  const { register, handleSubmit, reset, formState: { errors } } = useForm<QuestionInput>({ resolver: zodResolver(questionSchema), defaultValues: { question: "" } });

  useEffect(() => {
    if (!isReal) return;
    getDataSource().getPaper(paperId).then(setPaper).catch((error: unknown) => setMessage(error instanceof Error ? error.message : "文献信息读取失败"));
  }, [isReal, paperId]);

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

  function openCitation(page: number) {
    setCurrentPage(page);
    setMobilePane("pdf");
  }

  async function submit(values: QuestionInput) {
    setBusy(true);
    setMessage("");
    try {
      setAnswer(await getDataSource().ask(values.question, [paperId]));
      reset();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "提问失败");
    } finally {
      setBusy(false);
    }
  }

  const infoPane = <aside className={`workspace-info pane-view ${mobilePane === "info" ? "mobile-active" : ""}`} aria-label="论文信息"><div className="pane-heading"><a href="/library" className="back-link"><ArrowLeft size={14} />返回文献库</a></div><div className="paper-summary"><span className="paper-index">PL–001</span><h2>{paper.title}</h2><p>{paper.authors} · {paper.year}</p><span className="status-pill ready"><span>✓</span>已建立索引</span></div><dl className="metadata"><div><dt>期刊 / 会议</dt><dd>{paper.venue}</dd></div><div><dt>页数</dt><dd>{paper.pages} 页</dd></div><div><dt>arXiv</dt><dd className="mono">{paper.arxivId ?? "—"}</dd></div></dl><div className="outline-list"><span className="eyebrow">论文目录</span>{["摘要", "1. Introduction", "2. Background", "3. Model Architecture", "4. Why Self-Attention", "5. Training", "6. Results"].map((item, index) => <button key={item} className={index === 1 ? "active" : ""} onClick={() => setCurrentPage(index + 1)}><span>{String(index + 1).padStart(2, "0")}</span>{item}</button>)}</div></aside>;

  const readerPane = <section className={`workspace-reader pane-view ${mobilePane === "pdf" ? "mobile-active" : ""}`} aria-label="PDF 阅读器"><div className="reader-toolbar"><div><button className="icon-button" aria-label="在文内搜索"><Search size={15} /></button></div><div className="page-control"><button className="icon-button" aria-label="上一页" onClick={() => setCurrentPage(Math.max(1, currentPage - 1))}><ChevronLeft size={16} /></button><span className="mono">{String(currentPage).padStart(2, "0")} / {paper.pages || "—"}</span><button className="icon-button" aria-label="下一页" onClick={() => setCurrentPage(Math.min(paper.pages || currentPage + 1, currentPage + 1))}><ChevronRight size={16} /></button></div><span className="mono muted">92%</span></div><div className="document-stage">{isReal ? <RealPdfDocument url={getDataSource().fileUrl(paperId)} page={currentPage} onPageCount={(count) => { setPaper((item) => ({ ...item, pages: count })); if (currentPage > count) setCurrentPage(count); }} /> : <article className="mock-paper" aria-label={`模拟 PDF 第 ${currentPage} 页`}><div className="pdf-running"><span>NEURIPS 2017</span><span>ARXIV:{paper.arxivId}</span></div><h1>{paper.title}</h1><p className="pdf-authors">Ashish Vaswani · Noam Shazeer · Niki Parmar · Jakob Uszkoreit · Llion Jones · Aidan N. Gomez</p><h2>{currentPage === 2 ? "1 · Introduction" : `Section · Page ${currentPage}`}</h2><p>The dominant sequence transduction models are based on complex recurrent or convolutional neural networks that include an encoder and a decoder.</p><p className={currentPage === 2 ? "pdf-highlight" : ""}>We propose a new simple network architecture, the Transformer, based solely on attention mechanisms, dispensing with recurrence and convolutions entirely.</p><p>Recurrent models typically factor computation along the symbol positions of the input and output sequences. This inherently sequential nature precludes parallelization within training examples.</p><div className="formula">Attention(Q, K, V) = softmax(QKᵀ / √dₖ)V</div><p>Self-attention connects all positions with a constant number of sequentially executed operations.</p><span className="pdf-page-number">{currentPage}</span></article>}<div className="citation-rail" aria-hidden="true">{answer.citations.map((citation) => <span key={citation.id} className={citation.page === currentPage ? "active" : ""} />)}</div></div><div className="reader-status"><strong>{answer.citations.some((citation) => citation.page === currentPage) ? "引用已定位" : "论文页面"}</strong><span>第 {currentPage} 页</span><span>{isReal ? "原始 PDF" : "文本层已载入"}</span></div></section>;

  const askPane = <aside className={`workspace-assistant pane-view ${mobilePane === "ask" ? "mobile-active" : ""}`} aria-label="论文问答"><div className="assistant-head"><span><Quote size={14} />论文助手</span><span className="scope-chip">当前论文</span></div><div className="conversation"><div className="run-note">已保留 {answer.citations.length} 条可验证证据</div><span className="eyebrow">你的问题</span><p className="question-text">{answer.question}</p><span className="eyebrow">基于原文回答</span><p className="answer-text">{answer.answer} {answer.citations.map((citation, index) => <button key={citation.id} className="inline-citation" onClick={() => openCitation(citation.page)} aria-label={`查看第 ${citation.page} 页引用`}>[{index + 1}]</button>)}</p><div className="citation-list" aria-label="回答引用">{answer.citations.map((citation, index) => <button className="citation-row" key={citation.id} onClick={() => openCitation(citation.page)}><span className="citation-no">{String(index + 1).padStart(2, "0")}</span><q>{citation.quote}</q><span className="citation-page">PDF {String(citation.page).padStart(2, "0")}</span></button>)}</div>{message && <p className="field-error" role="alert">{message}</p>}</div><form className="composer" onSubmit={handleSubmit(submit)}><label className="composer-box"><span className="sr-only">向论文提问</span><textarea {...register("question")} placeholder="继续追问这篇论文…" rows={2} /><button className="send-button" disabled={busy} aria-label="发送问题"><Send size={15} /></button></label>{errors.question && <span className="field-error">{errors.question.message}</span>}<div className="composer-meta"><span>仅依据当前论文回答</span><span>{busy ? "正在检索…" : "Ctrl + Enter"}</span></div></form></aside>;

  return (
    <div ref={workspaceRef} className="paper-workspace" data-client-ready="false">
      <nav className="mobile-workspace-tabs" aria-label="移动端工作区">{tabItems.map(({ id, label, icon: Icon }) => <button key={id} onClick={() => setMobilePane(id)} className={mobilePane === id ? "active" : ""} aria-current={mobilePane === id ? "page" : undefined}><Icon size={15} />{label}<span className="sr-only">{mobilePane === id ? "，当前视图" : ""}</span></button>)}</nav>
      <div className="workspace-desktop"><Group orientation="horizontal" id={demo ? "demo-workspace" : "paper-workspace"}><Panel id="info" defaultSize="20%" minSize="16%" maxSize="28%">{infoPane}</Panel><ResizeSeparator label="调整论文信息栏宽度" /><Panel id="reader" defaultSize="49%" minSize="34%">{readerPane}</Panel><ResizeSeparator label="调整论文助手栏宽度" /><Panel id="assistant" defaultSize="31%" minSize="25%" maxSize="39%">{askPane}</Panel></Group></div>
      <div className="workspace-mobile">{infoPane}{readerPane}{askPane}</div>
    </div>
  );
}
