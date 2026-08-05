"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { BookOpen, Check, ChevronRight, Search, Send } from "lucide-react";
import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { getDataSource } from "@/lib/data-source";
import type { AgentActivity, AgentAnswer, Paper, PaperCollection } from "@/lib/types";
import { AgentRunProgress } from "./agent-run-progress";
import { EvidenceQualityStrip } from "./evidence-quality-strip";

const schema = z.object({ question: z.string().trim().min(3, "请输入更具体的问题") });
const subscribeToClient = () => () => undefined;
type ScopeKey = "all" | "recent" | `collection:${string}`;

interface AskScope {
  key: ScopeKey;
  label: string;
  paperIds: string[];
}

export function AskView() {
  const clientReady = useSyncExternalStore(subscribeToClient, () => true, () => false);
  const [scopeKey, setScopeKey] = useState<ScopeKey>("all");
  const [papers, setPapers] = useState<Paper[]>([]);
  const [collections, setCollections] = useState<PaperCollection[]>([]);
  const [scopesLoading, setScopesLoading] = useState(true);
  const [scopesError, setScopesError] = useState("");
  const [answer, setAnswer] = useState<AgentAnswer | null>(null);
  const [activities, setActivities] = useState<AgentActivity[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const { register, handleSubmit, setValue, formState: { errors } } = useForm<{ question: string }>({ resolver: zodResolver(schema) });

  useEffect(() => {
    let active = true;
    const source = getDataSource();
    void Promise.all([source.listPapers(), source.listCollections()])
      .then(([nextPapers, nextCollections]) => {
        if (!active) return;
        setPapers(nextPapers);
        setCollections(nextCollections);
      })
      .catch(() => {
        if (active) setScopesError("文献范围读取失败；仍可使用全部文献提问。");
      })
      .finally(() => {
        if (active) setScopesLoading(false);
      });
    return () => { active = false; };
  }, []);

  const scopes = useMemo<AskScope[]>(() => {
    const readyPapers = papers.filter((paper) => paper.status === "ready" && !paper.archivedAt);
    const readyIds = new Set(readyPapers.map((paper) => paper.id));
    const recentIds = readyPapers
      .filter((paper) => paper.lastOpenedAt)
      .sort((left, right) => Date.parse(right.lastOpenedAt ?? "") - Date.parse(left.lastOpenedAt ?? ""))
      .slice(0, 10)
      .map((paper) => paper.id);
    return [
      { key: "all", label: "全部文献", paperIds: [] },
      { key: "recent", label: "最近阅读", paperIds: recentIds },
      ...collections.map((collection) => ({
        key: `collection:${collection.id}` as const,
        label: collection.name,
        paperIds: collection.paperIds.filter((paperId) => readyIds.has(paperId)),
      })),
    ];
  }, [collections, papers]);
  const selectedScope = scopes.find((item) => item.key === scopeKey) ?? scopes[0];
  const allReadyCount = papers.filter((paper) => paper.status === "ready" && !paper.archivedAt).length;

  async function submit(values: { question: string }) {
    const question = values.question;
    setBusy(true);
    setMessage("");
    setActivities([]);
    setAnswer({ question, answer: "", citations: [], activities: [] });
    try {
      const next = await getDataSource().ask(question, selectedScope.paperIds, {
        onActivity: (activity) => {
          setActivities((items) => {
            const exists = items.some((item) => item.key === activity.key);
            return exists ? items.map((item) => item.key === activity.key ? activity : item) : [...items, activity];
          });
        },
        onAnswerUpdate: (nextAnswer) => {
          setAnswer((current) => current?.question === question ? { ...current, answer: nextAnswer } : current);
        },
        onCitationsUpdate: (citations) => {
          setAnswer((current) => current?.question === question ? { ...current, citations } : current);
        },
        onEvidenceQualityUpdate: (evidenceQuality) => {
          setAnswer((current) => current?.question === question ? { ...current, evidenceQuality } : current);
        },
      });
      setAnswer(next);
      setActivities(next.activities ?? []);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "提问失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ask-layout" data-client-ready={clientReady}>
      <aside className="scope-panel">
        <span className="eyebrow">回答范围</span><h2>选择要检索的内容</h2>
        {scopes.map((item) => {
          const count = item.key === "all" ? allReadyCount : item.paperIds.length;
          const disabled = busy || (item.key !== "all" && count === 0);
          return <button key={item.key} type="button" disabled={disabled} onClick={() => setScopeKey(item.key)} className={scopeKey === item.key ? "active" : ""} aria-pressed={scopeKey === item.key}><span className="scope-check">{scopeKey === item.key && <Check size={13} />}</span>{item.label}<small>{scopesLoading ? "读取中" : `${count} 篇`}</small></button>;
        })}
        {scopesError && <p className="field-error" role="alert">{scopesError}</p>}
        <div className="scope-note"><BookOpen size={17} /><p>回答只使用你有权访问且完成索引的文献。</p></div>
      </aside>
      <section className="ask-stage">
        <div className="ask-intro"><span className="eyebrow">Ask your library</span><h2>把问题交给文献，不交给记忆。</h2><p>当前范围：{selectedScope.label}。每条结论都会附上可以回读的物理页码。</p></div>
        <form className="ask-composer" onSubmit={handleSubmit(submit)}><textarea rows={4} placeholder="例如：这些论文如何解释长上下文中的证据位置偏差？" {...register("question")} /><div><span role={message ? "alert" : undefined}>{errors.question?.message ?? (message || "支持方法对比、结论核对与跨论文综合")}</span><button className="primary-button" disabled={busy || !clientReady}><Send size={15} />{busy ? "正在检索" : "开始提问"}</button></div></form>
        <AgentRunProgress activities={activities} />
        {answer && <article className={`ask-answer ${answer.evidenceQuality?.grade === "insufficient" ? "quality-insufficient" : ""}`}><div className="answer-run" role="status"><Search size={14} />{answer.evidenceQuality?.summary ?? (busy ? "问题已提交，正在检索并等待回答事件" : `已在 ${selectedScope.label} 中检索 · ${answer.citations.length} 条证据`)}</div><EvidenceQualityStrip quality={answer.evidenceQuality} /><h3>{answer.question}</h3><p>{answer.answer || (busy ? "正在准备基于文献证据的回答…" : "本次运行未返回回答。")}</p>{answer.citations.length > 0 && <div className="answer-sources">{answer.citations.map((item, index) => <a key={item.id} href={`/library/${item.paperId}?page=${item.page}`}><span>{String(index + 1).padStart(2, "0")}</span><span><strong>{item.paperTitle}</strong><small>{item.quote}</small></span><em>PDF {item.page}</em><ChevronRight size={15} /></a>)}</div>}</article>}
        {!answer && <div className="ask-prompts"><span>可以这样问</span>{["比较 Transformer 与 RNN 的计算路径", "RAG 的检索器在训练中如何更新？", "这些论文有哪些互相矛盾的结论？"].map((item) => <button type="button" key={item} onClick={() => setValue("question", item, { shouldValidate: true })}>{item}<ChevronRight size={14} /></button>)}</div>}
      </section>
    </div>
  );
}
