"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { BookOpen, Check, ChevronRight, Search, Send } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { getDataSource } from "@/lib/data-source";
import type { AgentAnswer } from "@/lib/types";

const schema = z.object({ question: z.string().trim().min(3, "请输入更具体的问题") });

export function AskView() {
  const [scope, setScope] = useState("全部文献");
  const [answer, setAnswer] = useState<AgentAnswer | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const { register, handleSubmit, formState: { errors } } = useForm<{ question: string }>({ resolver: zodResolver(schema) });

  async function submit(values: { question: string }) {
    setBusy(true);
    setMessage("");
    try {
      setAnswer(await getDataSource().ask(values.question));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "提问失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ask-layout">
      <aside className="scope-panel">
        <span className="eyebrow">回答范围</span><h2>选择要检索的内容</h2>
        {["全部文献", "最近阅读", "Transformer 集合"].map((item) => <button key={item} onClick={() => setScope(item)} className={scope === item ? "active" : ""}><span className="scope-check">{scope === item && <Check size={13} />}</span>{item}<small>{item === "全部文献" ? "5 篇" : item === "最近阅读" ? "3 篇" : "2 篇"}</small></button>)}
        <div className="scope-note"><BookOpen size={17} /><p>回答只使用你有权访问且完成索引的文献。</p></div>
      </aside>
      <section className="ask-stage">
        <div className="ask-intro"><span className="eyebrow">Ask your library</span><h2>把问题交给文献，不交给记忆。</h2><p>当前范围：{scope}。每条结论都会附上可以回读的物理页码。</p></div>
        <form className="ask-composer" onSubmit={handleSubmit(submit)}><textarea rows={4} placeholder="例如：这些论文如何解释长上下文中的证据位置偏差？" {...register("question")} /><div><span>{errors.question?.message ?? (message || "支持方法对比、结论核对与跨论文综合")}</span><button className="primary-button" disabled={busy}><Send size={15} />{busy ? "正在检索" : "开始提问"}</button></div></form>
        {answer && <article className="ask-answer"><div className="answer-run"><Search size={14} />已在 {scope} 中检索 · {answer.citations.length} 条证据</div><h3>{answer.question}</h3><p>{answer.answer}</p><div className="answer-sources">{answer.citations.map((item, index) => <a key={item.id} href={`/library/${item.paperId}`}><span>{String(index + 1).padStart(2, "0")}</span><span><strong>{item.paperTitle}</strong><small>{item.quote}</small></span><em>PDF {item.page}</em><ChevronRight size={15} /></a>)}</div></article>}
        {!answer && <div className="ask-prompts"><span>可以这样问</span>{["比较 Transformer 与 RNN 的计算路径", "RAG 的检索器在训练中如何更新？", "这些论文有哪些互相矛盾的结论？"].map((item) => <button key={item}>{item}<ChevronRight size={14} /></button>)}</div>}
      </section>
    </div>
  );
}
