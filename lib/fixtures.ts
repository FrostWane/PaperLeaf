import type { AgentAnswer, ArxivResult, Paper, PaperStructureGraph, PaperSummary, UserRecord } from "./types";

export const papers: Paper[] = [
  { id: "attention", title: "Attention Is All You Need", authors: "Vaswani 等", year: 2017, venue: "NeurIPS", pages: 15, status: "ready", tags: ["Transformer", "NLP"], arxivId: "1706.03762", abstract: "提出完全基于注意力机制的 Transformer，移除循环与卷积结构。", lastOpenedAt: "2026-07-28T08:30:00Z", createdAt: "2026-07-15T08:00:00Z" },
  { id: "bert", title: "BERT: Pre-training of Deep Bidirectional Transformers", authors: "Devlin 等", year: 2018, venue: "NAACL", pages: 16, status: "ready", tags: ["预训练", "NLP"], arxivId: "1810.04805", abstract: "使用双向 Transformer 表征进行语言模型预训练。", lastOpenedAt: "2026-07-26T13:20:00Z", createdAt: "2026-07-16T08:00:00Z" },
  { id: "rag", title: "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks", authors: "Lewis 等", year: 2020, venue: "NeurIPS", pages: 12, status: "ready", tags: ["RAG", "检索"], arxivId: "2005.11401", abstract: "将参数化生成模型与外部非参数知识索引结合。", lastOpenedAt: "2026-07-23T09:10:00Z", createdAt: "2026-07-17T08:00:00Z" },
  { id: "lora", title: "LoRA: Low-Rank Adaptation of Large Language Models", authors: "Hu 等", year: 2021, venue: "ICLR", pages: 26, status: "indexing", progress: 68, tags: ["微调"], arxivId: "2106.09685", abstract: "通过低秩矩阵降低大模型适配的训练参数量。", createdAt: "2026-07-18T08:00:00Z" },
  { id: "graphrag", title: "From Local to Global: A Graph RAG Approach", authors: "Edge 等", year: 2024, venue: "arXiv", pages: 21, status: "partial", tags: ["RAG", "知识图谱"], abstract: "使用图结构摘要提升面向私有语料的全局问题回答能力。", archivedAt: "2026-07-20T10:00:00Z", createdAt: "2026-07-19T08:00:00Z" },
];

export const groundedAnswer: AgentAnswer = {
  question: "作者为什么放弃循环结构？",
  answer: "作者用自注意力取代循环结构，主要因为它可以并行处理全部位置，减少训练中的顺序依赖；任意两个位置之间的信号路径更短，更利于学习长距离关系；在常见序列长度下，每层计算复杂度也具有竞争力。",
  citations: [
    { id: "c1", paperId: "attention", paperTitle: "Attention Is All You Need", page: 2, chunkId: "p2-c3", quote: "dispensing with recurrence and convolutions entirely", href: "/api/v1/papers/attention/file#page=2" },
    { id: "c2", paperId: "attention", paperTitle: "Attention Is All You Need", page: 6, chunkId: "p6-c1", quote: "maximum path length between any two positions", href: "/api/v1/papers/attention/file#page=6" },
    { id: "c3", paperId: "attention", paperTitle: "Attention Is All You Need", page: 11, chunkId: "p11-c2", quote: "significantly less time to train", href: "/api/v1/papers/attention/file#page=11" },
  ],
  evidenceQuality: {
    grade: "sufficient",
    confidence: 0.86,
    reasonCode: "channel_agreement",
    summary: "已定位 3 个证据页，关键词与语义检索相互印证",
    evidenceCount: 3,
    pageCount: 3,
    paperCount: 1,
    channels: ["keyword", "vector"],
    retrievalGrade: "sufficient",
    answerSupportGrade: "supported",
    answerSupportConfidence: 0.91,
    claimCount: 3,
    citedClaimCount: 3,
    supportedClaimCount: 3,
    claimCitationCoverage: 1,
    claimSupportCoverage: 1,
  },
};

export const paperSummary: PaperSummary = {
  paperId: "attention",
  mode: "model",
  content: "这篇论文要解决的是序列建模对循环计算的依赖：RNN 难以并行，长距离信息需要经过较长路径。作者提出完全基于自注意力的 Transformer，以多头注意力同时建模不同位置之间的关系，并用位置编码保留顺序信息。\n\n实验表明，该架构在机器翻译任务上取得有竞争力的结果，同时显著缩短训练时间。论文也指出，自注意力在超长序列上的计算与内存成本仍会随序列长度平方增长。",
  citations: [
    { chunkId: "p2-c3", physicalPage: 2 },
    { chunkId: "p4-c2", physicalPage: 4 },
    { chunkId: "p11-c2", physicalPage: 11 },
  ],
};

export const paperStructureGraph: PaperStructureGraph = {
  paperId: "attention",
  nodes: [
    { id: "problem", label: "循环结构限制并行与长程建模", physicalPage: 2, chunkId: "p2-c3" },
    { id: "method", label: "以多头自注意力替代循环", physicalPage: 4, chunkId: "p4-c2" },
    { id: "training", label: "位置编码与残差连接稳定训练", physicalPage: 5, chunkId: "p5-c1" },
    { id: "result", label: "翻译质量与训练效率同步改善", physicalPage: 11, chunkId: "p11-c2" },
  ],
  edges: [
    { source: "problem", target: "method" },
    { source: "method", target: "training" },
    { source: "training", target: "result" },
  ],
  mermaid: "flowchart TD\n    problem[\"循环结构限制并行与长程建模\"]\n    method[\"以多头自注意力替代循环\"]\n    training[\"位置编码与残差连接稳定训练\"]\n    result[\"翻译质量与训练效率同步改善\"]\n    problem --> method\n    method --> training\n    training --> result",
};

export const arxivResults: ArxivResult[] = [
  { id: "2402.19473", title: "Retrieval-Augmented Generation for Large Language Models: A Survey", authors: "Gao 等", year: 2024, summary: "系统梳理 Naive、Advanced 与 Modular RAG，并总结检索、生成和评测方法。" },
  { id: "2312.10997", title: "Retrieval-Augmented Generation for AI-Generated Content", authors: "Zhao 等", year: 2023, summary: "从生成内容视角综述 RAG 的主要方法、数据集和挑战。" },
  { id: "2307.03172", title: "Lost in the Middle: How Language Models Use Long Contexts", authors: "Liu 等", year: 2023, summary: "分析长上下文模型对证据位置的敏感性，为检索后上下文组织提供依据。" },
];

export const users: UserRecord[] = [
  { id: "u1", name: "林研究员", email: "lin@example.org", role: "管理员", status: "正常", papers: 38 },
  { id: "u2", name: "陈同学", email: "chen@example.org", role: "用户", status: "正常", papers: 17 },
  { id: "u3", name: "王同学", email: "wang@example.org", role: "用户", status: "已停用", papers: 4 },
];
