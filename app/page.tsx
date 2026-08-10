"use client";

import { ArrowRight, BookOpen, Check, GitBranch as Github, Search, ShieldCheck, Upload } from "lucide-react";
import { Brand } from "@/components/brand";

export default function Home() {
  const realDeployment = process.env.NEXT_PUBLIC_DATA_MODE === "real";
  const workspaceHref = realDeployment ? "/login" : "/demo";
  const workspaceLabel = realDeployment ? "登录工作台" : "打开演示";

  return (
    <main className="landing">
      <header className="landing-nav"><a href="/" aria-label="PaperLeaf 首页"><Brand /></a><nav aria-label="首页导航"><a href="#workflow">工作方式</a><a href="#principles">设计原则</a><a href="https://github.com/FrostWane/PaperLeaf">GitHub</a><a className="nav-demo" href={workspaceHref}>{workspaceLabel}</a></nav></header>
      <section className="hero"><div className="hero-copy"><span className="eyebrow">Open-source research workspace</span><h1>让每一次回答，<br />都回到论文原页。</h1><p>上传、阅读和整理 PDF，并让研究 Agent 在可验证的证据上回答。PaperLeaf 把文献库、页码引用与受控工具调用放进一个安静的工作台。</p><div className="hero-actions"><a className="primary-button large" href={workspaceHref}>{realDeployment ? "进入 PaperLeaf" : "体验 PaperLeaf"} <ArrowRight size={17} /></a><a className="secondary-button large" href="https://github.com/FrostWane/PaperLeaf"><Github size={17} />查看源码</a></div><div className="trust-line"><span><Check size={14} />数据留在你的部署中</span><span><Check size={14} />回答带物理页码</span><span><Check size={14} />Agent 操作可审计</span></div></div><div className="hero-preview" aria-label="PaperLeaf 工作区预览"><div className="preview-top"><span className="preview-brand">PL</span><span>Attention Is All You Need</span><span className="status-pill ready"><span>✓</span>可提问</span></div><div className="preview-grid"><div className="preview-index"><small>LIBRARY / 03</small>{["Attention Is All You Need", "BERT: Pre-training", "Retrieval-Augmented Generation"].map((name, index) => <div className={index === 0 ? "selected" : ""} key={name}><span>00{index + 1}</span>{name}</div>)}</div><div className="preview-paper"><article><small>NEURIPS 2017 · PAGE 02</small><p className="preview-title">Attention Is All You Need</p><p>The dominant sequence transduction models are based on complex recurrent or convolutional neural networks…</p><p className="preview-highlight">We propose a new simple network architecture, the Transformer, based solely on attention mechanisms.</p></article></div><div className="preview-answer"><small>GROUNDED ANSWER</small><strong>作者为什么放弃循环结构？</strong><p>自注意力可以并行处理全部位置，并缩短任意位置间的信号路径。</p><button>01 · PDF 02</button><button>02 · PDF 06</button></div></div></div></section>
      <section id="workflow" className="landing-section"><div className="section-intro"><span className="eyebrow">一条完整的研究链路</span><h2>从文件到证据，不跳过中间过程。</h2><p>PaperLeaf 不把“上传成功”当作终点。每篇论文都有清楚的解析状态、检索证据和页码归属。</p></div><div className="workflow-list">{[{ icon: Upload, no: "01", title: "保存与解析", copy: "PDF 原件私有存储，按物理页提取文本，异常页面清楚标记。" }, { icon: Search, no: "02", title: "混合检索", copy: "向量与关键词共同召回，检索过程可以评测，也可以逐步优化。" }, { icon: BookOpen, no: "03", title: "引用回读", copy: "回答中的每个证据编号，都能回到对应 PDF 页和原文片段。" }].map(({ icon: Icon, no, title, copy }) => <article key={no}><span className="workflow-no">{no}</span><Icon size={20} /><h3>{title}</h3><p>{copy}</p></article>)}</div></section>
      <section id="principles" className="principles"><div><ShieldCheck size={24} /><h2>Agent 有边界，数据有归属。</h2></div><p>联网只访问受控学术数据源，导入 PDF 必须确认；管理员能管理任务，但默认看不到用户的论文与对话。</p><a href={workspaceHref}>{realDeployment ? "登录后查看引用跳转" : "在演示中查看引用跳转"} <ArrowRight size={16} /></a></section>
      <footer className="landing-footer"><Brand /><span>面向研究过程的开源文献工作台</span><a href="https://github.com/FrostWane/PaperLeaf">Apache-2.0 · GitHub</a></footer>
    </main>
  );
}
