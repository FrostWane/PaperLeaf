import type { AgentAnswer, ArxivResult, Paper, PaperStructureGraph, PaperSummary, UserRecord } from "./types";

export const papers: Paper[] = [
  { id: "attention", title: "Attention Is All You Need", authors: "Vaswani 等", year: 2017, venue: "arXiv", publication: "Advances in Neural Information Processing Systems", pages: 15, status: "ready", arxivId: "1706.03762", abstract: "提出完全基于注意力机制的 Transformer，移除循环与卷积结构。", lastOpenedAt: "2026-07-28T08:30:00Z", createdAt: "2026-07-15T08:00:00Z" },
  { id: "bert", title: "BERT: Pre-training of Deep Bidirectional Transformers", authors: "Devlin 等", year: 2018, venue: "arXiv", publication: "Proceedings of NAACL-HLT", pages: 16, status: "ready", arxivId: "1810.04805", abstract: "使用双向 Transformer 表征进行语言模型预训练。", lastOpenedAt: "2026-07-26T13:20:00Z", createdAt: "2026-07-16T08:00:00Z" },
  { id: "rag", title: "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks", authors: "Lewis 等", year: 2020, venue: "arXiv", publication: "Advances in Neural Information Processing Systems", pages: 12, status: "ready", arxivId: "2005.11401", abstract: "将参数化生成模型与外部非参数知识索引结合。", lastOpenedAt: "2026-07-23T09:10:00Z", createdAt: "2026-07-17T08:00:00Z" },
  { id: "lora", title: "LoRA: Low-Rank Adaptation of Large Language Models", authors: "Hu 等", year: 2021, venue: "arXiv", publication: "International Conference on Learning Representations", pages: 26, status: "indexing", progress: 68, arxivId: "2106.09685", abstract: "通过低秩矩阵降低大模型适配的训练参数量。", createdAt: "2026-07-18T08:00:00Z" },
  { id: "graphrag", title: "From Local to Global: A Graph RAG Approach", authors: "Edge 等", year: 2024, venue: "本地上传", publication: "", pages: 21, status: "partial", abstract: "使用图结构摘要提升面向私有语料的全局问题回答能力。", archivedAt: "2026-07-20T10:00:00Z", createdAt: "2026-07-19T08:00:00Z" },
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
  status: "ready",
  stale: false,
  mode: "model",
  sections: [
    { key: "research_problem", title: "研究问题", facts: [
      { text: "循环神经网络的顺序计算限制训练并行度，且长距离信息需要经过更长的传播路径。", citations: [{ chunkId: "p2-c3", physicalPage: 2, quote: "sequential computation precludes parallelization" }] },
    ] },
    { key: "core_method", title: "核心方法", facts: [
      { text: "Transformer 完全以多头自注意力替代循环与卷积，并使用位置编码保留序列顺序。", citations: [{ chunkId: "p4-c2", physicalPage: 4 }, { chunkId: "p5-c1", physicalPage: 5 }] },
    ] },
    { key: "experiment_setup", title: "实验设置", facts: [
      { text: "论文在 WMT 2014 英德与英法翻译任务上比较模型质量、训练成本和并行效率。", citations: [{ chunkId: "p8-c1", physicalPage: 8 }] },
    ] },
    { key: "main_results", title: "主要结果", facts: [
      { text: "模型取得有竞争力的翻译质量，同时显著缩短训练时间。", citations: [{ chunkId: "p11-c2", physicalPage: 11 }, { chunkId: "p12-c1", physicalPage: 12 }] },
    ] },
    { key: "limitations", title: "局限与适用范围", facts: [
      { text: "标准自注意力的计算和内存开销随序列长度平方增长，超长序列仍需更高效的注意力机制。", citations: [{ chunkId: "p6-c3", physicalPage: 6 }] },
    ] },
  ],
  citations: [
    { chunkId: "p2-c3", physicalPage: 2 },
    { chunkId: "p4-c2", physicalPage: 4 },
    { chunkId: "p5-c1", physicalPage: 5 },
    { chunkId: "p8-c1", physicalPage: 8 },
    { chunkId: "p11-c2", physicalPage: 11 },
    { chunkId: "p12-c1", physicalPage: 12 },
    { chunkId: "p6-c3", physicalPage: 6 },
  ],
};

export const paperStructureGraph: PaperStructureGraph = {
  paperId: "attention",
  status: "ready",
  stale: false,
  nodes: [
    { id: "background", type: "background", label: "序列建模的并行瓶颈", summary: "循环网络依赖逐位置计算，长程信号传播路径较长。", citations: [{ chunkId: "p2-c2", physicalPage: 2 }] },
    { id: "problem", type: "research_problem", label: "研究问题：能否移除循环结构", summary: "目标是在保持序列建模能力的同时提升并行度。", citations: [{ chunkId: "p2-c3", physicalPage: 2 }, { chunkId: "p6-c1", physicalPage: 6 }] },
    { id: "method", type: "method", label: "以多头自注意力替代循环", summary: "编码器和解码器由注意力与前馈层组成。", citations: [{ chunkId: "p4-c2", physicalPage: 4 }] },
    { id: "position", type: "method", label: "位置编码保留顺序信息", summary: "向输入表示加入位置编码，并配合残差与归一化。", citations: [{ chunkId: "p5-c1", physicalPage: 5 }] },
    { id: "data", type: "data", label: "WMT 2014 机器翻译数据", summary: "使用英德和英法翻译任务验证模型。", citations: [{ chunkId: "p8-c1", physicalPage: 8 }] },
    { id: "experiment", type: "experiment", label: "比较质量、成本与并行效率", summary: "以 BLEU、训练时长与复杂度进行对比。", citations: [{ chunkId: "p10-c1", physicalPage: 10 }] },
    { id: "result", type: "result", label: "翻译质量与训练效率同步改善", summary: "在更短训练时间内取得有竞争力的翻译结果。", citations: [{ chunkId: "p11-c2", physicalPage: 11 }, { chunkId: "p12-c1", physicalPage: 12 }] },
    { id: "limitation", type: "limitation", label: "长序列二次复杂度仍是限制", summary: "自注意力对序列长度的计算和内存成本为平方级。", citations: [{ chunkId: "p6-c3", physicalPage: 6 }] },
  ],
  edges: [
    { source: "background", target: "problem" },
    { source: "problem", target: "method" },
    { source: "method", target: "position" },
    { source: "position", target: "data" },
    { source: "data", target: "experiment" },
    { source: "experiment", target: "result" },
    { source: "result", target: "limitation" },
  ],
  mermaid: "flowchart TD\n    background[\"背景：序列计算瓶颈\"]\n    problem[\"问题：移除循环结构\"]\n    method[\"方法：多头自注意力\"]\n    position[\"方法：位置编码\"]\n    data[\"数据：WMT 2014\"]\n    experiment[\"实验：质量与效率对比\"]\n    result[\"结果：质量与效率提升\"]\n    limitation[\"局限：长序列二次复杂度\"]\n    background --> problem\n    problem --> method\n    method --> position\n    position --> data\n    data --> experiment\n    experiment --> result\n    result --> limitation",
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
