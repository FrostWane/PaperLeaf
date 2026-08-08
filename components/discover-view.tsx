"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { Check, ExternalLink, Plus, RefreshCw, Search, ThumbsDown, ThumbsUp, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { getDataSource } from "@/lib/data-source";
import { getUserPreferences } from "@/lib/preferences-api";
import type { ArxivResult, DiscoveryRecommendationPage } from "@/lib/types";

type DiscoverMode = "recommend" | "search";

const emptyRecommendation: DiscoveryRecommendationPage = {
  items: [],
  batch: 0,
  basisPaperCount: 0,
  profileTerms: [],
  strategy: "empty_library",
};

export function DiscoverView() {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<ArxivResult | null>(null);
  const [imported, setImported] = useState<string[]>([]);
  const [results, setResults] = useState<ArxivResult[]>([]);
  const [recommendation, setRecommendation] = useState(emptyRecommendation);
  const [mode, setMode] = useState<DiscoverMode>("recommend");
  const [recommendationAllowed, setRecommendationAllowed] = useState<boolean | null>(process.env.NEXT_PUBLIC_DATA_MODE === "real" ? null : true);
  const [message, setMessage] = useState(process.env.NEXT_PUBLIC_DATA_MODE === "real" ? "正在读取发现偏好…" : "正在根据文献库挑选相关论文…");
  const [loading, setLoading] = useState(true);
  const [importing, setImporting] = useState(false);
  const [feedbackPending, setFeedbackPending] = useState<string[]>([]);
  const requestSequence = useRef(0);
  const initialRequestStarted = useRef(false);

  async function loadRecommendations(refresh = false) {
    const sequence = ++requestSequence.current;
    setLoading(true);
    setMode("recommend");
    setMessage(refresh ? "正在换一批…" : "正在读取上次推荐…");
    try {
      const next = await getDataSource().recommendArxiv({ refresh });
      if (sequence !== requestSequence.current) return;
      setRecommendation(next);
      setResults(next.items);
      setMessage(next.strategy === "empty_library"
        ? "文献库还没有可用于推荐的论文。"
        : next.items.length
          ? next.restored ? "已恢复上次推荐。" : ""
          : "这一批没有新的结果，可以继续换一批。");
    } catch (error) {
      if (sequence === requestSequence.current) setMessage(error instanceof Error ? error.message : "推荐失败");
    } finally {
      if (sequence === requestSequence.current) setLoading(false);
    }
  }

  useEffect(() => {
    if (initialRequestStarted.current) return;
    initialRequestStarted.current = true;
    if (process.env.NEXT_PUBLIC_DATA_MODE !== "real") {
      queueMicrotask(() => void loadRecommendations());
      return;
    }
    void getUserPreferences().then((preferences) => {
      setRecommendationAllowed(preferences.arxivSearchEnabled);
      if (preferences.arxivSearchEnabled) void loadRecommendations();
      else {
        setLoading(false);
        setMessage("联网发现尚未开启。");
      }
    }).catch((error) => {
      setLoading(false);
      setMessage(error instanceof Error ? error.message : "个人设置读取失败");
    });
  }, []);

  function returnToRecommendations() {
    if (recommendationAllowed) void loadRecommendations();
    else {
      setMode("recommend");
      setResults([]);
      setMessage("联网发现尚未开启。");
    }
  }

  async function search() {
    const normalized = query.trim();
    if (!normalized || loading) return;
    const sequence = ++requestSequence.current;
    setLoading(true);
    setMode("search");
    setMessage("正在搜索 arXiv…");
    try {
      const found = await getDataSource().searchArxiv(normalized);
      if (sequence !== requestSequence.current) return;
      setResults(found);
      setMessage(found.length ? "" : "没有找到匹配的开放论文。");
    } catch (error) {
      if (sequence === requestSequence.current) setMessage(error instanceof Error ? error.message : "搜索失败");
    } finally {
      if (sequence === requestSequence.current) setLoading(false);
    }
  }

  async function confirmImport() {
    if (!selected || importing) return;
    setImporting(true);
    try {
      await getDataSource().importArxiv(selected.id, selected.itemId);
      setImported((items) => Array.from(new Set([...items, selected.id])));
      setResults((items) => items.map((item) => item.id === selected.id ? { ...item, imported: true } : item));
      setMessage(`《${selected.title}》已加入导入队列。`);
      setSelected(null);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "导入失败");
      setSelected(null);
    } finally {
      setImporting(false);
    }
  }

  async function recordFeedback(paper: ArxivResult, action: "opened" | "interested" | "not_interested") {
    if (!paper.itemId || feedbackPending.includes(paper.itemId)) return;
    if (action === "opened") {
      setResults((items) => items.map((item) => item.itemId === paper.itemId ? { ...item, opened: true } : item));
    } else {
      setFeedbackPending((items) => [...items, paper.itemId!]);
    }
    try {
      const feedback = await getDataSource().recordDiscoveryFeedback(paper.itemId, action);
      setResults((items) => items.map((item) => item.itemId === paper.itemId ? {
        ...item,
        opened: action === "opened" ? true : item.opened,
        feedback: action === "opened" ? item.feedback : feedback,
      } : item));
    } catch (error) {
      if (action !== "opened") setMessage(error instanceof Error ? error.message : "反馈保存失败");
    } finally {
      if (action !== "opened") setFeedbackPending((items) => items.filter((id) => id !== paper.itemId));
    }
  }

  const strategyLabel = recommendation.strategy === "semantic_keyword" ? "语义与主题" : "主题匹配";

  return (
    <div className="discover-layout">
      <section className="discover-main">
        <div className="discover-heading">
          <div>
            <h2>{mode === "recommend" ? "与你的研究相关" : "arXiv 搜索结果"}</h2>
            {mode === "recommend" && recommendation.basisPaperCount > 0 && <p>根据文献库中的 {recommendation.basisPaperCount} 篇论文挑选</p>}
          </div>
          <div className="discover-heading-actions">
            {mode === "search" && <button type="button" className="secondary-button" disabled={loading} onClick={returnToRecommendations}>返回推荐</button>}
            <button type="button" className="secondary-button discover-refresh" disabled={loading || recommendationAllowed !== true || recommendation.strategy === "empty_library"} onClick={() => void loadRecommendations(true)}><RefreshCw size={15} className={loading && mode === "recommend" ? "spin" : ""} />换一批</button>
          </div>
        </div>

        <div className="discover-search">
          <Search size={18} />
          <input
            aria-label="搜索 arXiv"
            placeholder="也可以按标题、作者或关键词搜索 arXiv"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.nativeEvent.isComposing) void search();
            }}
          />
          <button type="button" className="primary-button" disabled={loading || !query.trim()} onClick={() => void search()}>搜索</button>
        </div>
        <div className="source-note" role="status">{message || (mode === "recommend" ? "已排除文献库中已有论文" : "仅显示 arXiv 开放论文")}</div>

        {recommendationAllowed === false && mode === "recommend"
          ? <div className="discover-consent"><h3>开启个性化论文推荐</h3><p>启用后，PaperLeaf 会从你的文献标题与已索引正文中提取少量主题词，并将主题词发送给 arXiv 检索公开论文。PDF 文件不会上传。</p><Link className="primary-button" href="/settings#agent">前往设置</Link></div>
          : loading && !results.length
          ? <div className="discover-loading" aria-label="正在加载推荐"><span /><span /><span /></div>
          : recommendation.strategy === "empty_library" && mode === "recommend"
            ? <div className="discover-empty"><h3>先添加几篇研究论文</h3><p>PaperLeaf 会从标题和已索引正文中识别研究方向，再为你挑选相关工作。</p></div>
            : <div className="result-list">{results.map((paper) => (
              <article key={paper.id}>
                <div className="result-index">ARXIV:{paper.id}</div>
                <h3>{paper.title}</h3>
                <p className="result-meta">{paper.authors} · {paper.year || "年份未知"}</p>
                {mode === "recommend" && paper.matchedPaperTitle && <div className="result-match"><span>{paper.matchType === "semantic" ? "语义相关" : "主题相关"}</span><p>与《{paper.matchedPaperTitle}》相关{paper.matchedTerms?.length ? ` · ${paper.matchedTerms.join(" / ")}` : ""}</p></div>}
                <p>{paper.summary}</p>
                {mode === "recommend" && paper.itemId && <div className="recommendation-feedback" aria-label="推荐反馈">
                  <span>这篇推荐是否有帮助？</span>
                  <button type="button" aria-pressed={paper.feedback === "interested"} className={paper.feedback === "interested" ? "active" : ""} disabled={feedbackPending.includes(paper.itemId)} onClick={() => void recordFeedback(paper, "interested")}><ThumbsUp size={15} />感兴趣</button>
                  <button type="button" aria-pressed={paper.feedback === "not_interested"} className={paper.feedback === "not_interested" ? "active" : ""} disabled={feedbackPending.includes(paper.itemId)} onClick={() => void recordFeedback(paper, "not_interested")}><ThumbsDown size={15} />不感兴趣</button>
                </div>}
                <div className="result-actions">
                  <a href={`https://arxiv.org/abs/${paper.id}`} target="_blank" rel="noreferrer" onClick={() => void recordFeedback(paper, "opened")}>查看来源 <ExternalLink size={14} /></a>
                  <button type="button" className={paper.imported || imported.includes(paper.id) ? "secondary-button imported" : "primary-button"} disabled={paper.imported || imported.includes(paper.id)} onClick={() => setSelected(paper)}>{paper.imported || imported.includes(paper.id) ? <><Check size={15} />已加入队列</> : <><Plus size={15} />导入文献库</>}</button>
                </div>
              </article>
            ))}</div>}
      </section>

      <aside className="discover-side">
        <h3>研究方向</h3>
        {recommendationAllowed === false
          ? <p>开启联网发现后，这里会显示本轮推荐所依据的研究主题。</p>
          : recommendation.basisPaperCount > 0
          ? <><p>{strategyLabel} · 本批从《{recommendation.seedPaperTitle}》延伸检索</p>{recommendation.generatedAt && <small>生成于 {new Date(recommendation.generatedAt).toLocaleString("zh-CN", { hour12: false })}</small>}{recommendation.feedbackApplied && <small>本批排序已参考你的兴趣反馈</small>}<div className="discover-topics">{recommendation.profileTerms.map((term) => <span key={term}>{term}</span>)}</div></>
          : <p>添加论文后，这里会形成你的个人研究主题。</p>}
        <small>仅向 arXiv 发送主题词，不上传 PDF 文件。</small>
      </aside>

      <Dialog.Root open={Boolean(selected)} onOpenChange={(open) => !open && setSelected(null)}><Dialog.Portal><Dialog.Overlay className="dialog-overlay" /><Dialog.Content className="dialog-content"><div className="dialog-head"><div><Dialog.Title>确认导入</Dialog.Title><Dialog.Description>PaperLeaf 将从 arXiv 下载开放 PDF，并加入你的私有文献库。</Dialog.Description></div><Dialog.Close className="icon-button" aria-label="关闭"><X size={17} /></Dialog.Close></div><div className="confirm-paper"><span className="mono">ARXIV:{selected?.id}</span><strong>{selected?.title}</strong><p>{selected?.authors}</p></div><div className="dialog-actions"><Dialog.Close asChild><button type="button" className="secondary-button">取消</button></Dialog.Close><button type="button" className="primary-button" disabled={importing} onClick={() => void confirmImport()}>{importing ? "正在加入…" : "确认导入"}</button></div></Dialog.Content></Dialog.Portal></Dialog.Root>
    </div>
  );
}
