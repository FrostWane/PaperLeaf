"use client";

import type { ReactNode } from "react";
import type { ArtifactCitation, SummarySection } from "@/lib/types";

export interface SummaryContentProps {
  content: string;
  citations: ArtifactCitation[];
  onOpenPage: (page: number) => void;
}

interface CitationMatch {
  citation: ArtifactCitation;
  index: number;
}

const referencePattern = /\[(?:chunk:([^\]\r\n]+)|物理页\s*(\d+))\]/g;

function renderInlineReferences(
  text: string,
  citationsByChunk: Map<string, CitationMatch>,
  citationsByPage: Map<number, CitationMatch>,
  onOpenPage: (page: number) => void,
): ReactNode[] {
  const nodes: ReactNode[] = [];
  let lastIndex = 0;

  for (const match of text.matchAll(referencePattern)) {
    const marker = match[0];
    const markerIndex = match.index;
    const chunkId = match[1];
    const physicalPage = match[2] ? Number(match[2]) : undefined;
    const citation = chunkId
      ? citationsByChunk.get(chunkId.trim())
      : physicalPage !== undefined
        ? citationsByPage.get(physicalPage)
        : undefined;

    if (markerIndex > lastIndex) nodes.push(text.slice(lastIndex, markerIndex));

    if (!citation) {
      nodes.push(marker);
    } else {
      const number = citation.index + 1;
      nodes.push(
        <button
          key={`${markerIndex}-${citation.citation.chunkId}`}
          type="button"
          className="inline-citation"
          aria-label={`引用 [${number}]，查看 PDF 第 ${citation.citation.physicalPage} 页`}
          title={`${marker} · PDF ${citation.citation.physicalPage}`}
          onClick={() => onOpenPage(citation.citation.physicalPage)}
        >
          [{number}]
        </button>,
      );
    }

    lastIndex = markerIndex + marker.length;
  }

  if (lastIndex < text.length) nodes.push(text.slice(lastIndex));
  return nodes;
}

export function SummaryContent({ content, citations, onOpenPage }: SummaryContentProps) {
  const citationsByChunk = new Map<string, CitationMatch>();
  const citationsByPage = new Map<number, CitationMatch>();

  citations.forEach((citation, index) => {
    const match = { citation, index };
    if (!citationsByChunk.has(citation.chunkId)) citationsByChunk.set(citation.chunkId, match);
    if (!citationsByPage.has(citation.physicalPage)) citationsByPage.set(citation.physicalPage, match);
  });

  const inline = (text: string) => renderInlineReferences(text, citationsByChunk, citationsByPage, onOpenPage);
  const lines = content.replace(/\r\n?/g, "\n").split("\n");
  const sourceHeadingLevels = lines.flatMap((line) => {
    const match = /^(#{1,6})\s+/.exec(line.trim());
    return match ? [match[1].length] : [];
  });
  const firstSourceHeadingLevel = sourceHeadingLevels.length > 0
    ? Math.min(...sourceHeadingLevels)
    : 1;
  const blocks: ReactNode[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const heading = /^(#{1,6})\s+(.+?)\s*$/.exec(line.trim());
    if (heading) {
      // 本组件嵌在工作台 h3 标题之后。模型 Markdown 的起始级别不可信，
      // 因此保留相对层级，但统一从 h4 开始，避免生成 h1/h2 破坏页面大纲。
      const level = Math.min(6, 4 + heading[1].length - firstSourceHeadingLevel);
      const Heading = `h${level}` as keyof React.JSX.IntrinsicElements;
      blocks.push(<Heading key={`heading-${index}`}>{inline(heading[2])}</Heading>);
      index += 1;
      continue;
    }

    const unorderedItem = /^\s*[-*+]\s+(.+)$/.exec(line);
    const orderedItem = /^\s*\d+[.)]\s+(.+)$/.exec(line);
    if (unorderedItem || orderedItem) {
      const ordered = Boolean(orderedItem);
      const items: ReactNode[] = [];

      while (index < lines.length) {
        const item = ordered
          ? /^\s*\d+[.)]\s+(.+)$/.exec(lines[index])
          : /^\s*[-*+]\s+(.+)$/.exec(lines[index]);
        if (!item) break;
        items.push(<li key={`item-${index}`}>{inline(item[1])}</li>);
        index += 1;
      }

      const List = ordered ? "ol" : "ul";
      blocks.push(<List key={`list-${index}`}>{items}</List>);
      continue;
    }

    const paragraph: string[] = [];
    while (index < lines.length && lines[index].trim()) {
      const candidate = lines[index];
      if (paragraph.length > 0 && (
        /^(#{1,6})\s+/.test(candidate.trim())
        || /^\s*[-*+]\s+/.test(candidate)
        || /^\s*\d+[.)]\s+/.test(candidate)
      )) break;
      paragraph.push(candidate.trim());
      index += 1;
    }
    blocks.push(<p key={`paragraph-${index}`}>{inline(paragraph.join(" "))}</p>);
  }

  return <div className="summary-content">{blocks}</div>;
}

export function StructuredSummary({ sections, onOpenPage }: { sections: SummarySection[]; onOpenPage: (page: number) => void }) {
  return <div className="summary-sections">
    {sections.map((section, sectionIndex) => <section key={section.key} className="summary-section" aria-label={section.title}>
      <div className="summary-section-heading"><span>{String(sectionIndex + 1).padStart(2, "0")}</span><h4>{section.title}</h4></div>
      <ul>{section.facts.map((fact, factIndex) => <li key={`${section.key}-${factIndex}`}>
        <p>{fact.text}</p>
        <div className="summary-fact-citations" aria-label={`${section.title}第 ${factIndex + 1} 条事实的原文引用`}>
          {fact.citations.map((citation, citationIndex) => <button key={`${citation.chunkId}-${citation.physicalPage}`} type="button" aria-label={`查看 ${section.title}第 ${factIndex + 1} 条事实的引用 ${citationIndex + 1}，PDF 第 ${citation.physicalPage} 页`} onClick={() => onOpenPage(citation.physicalPage)}>
            PDF {citation.physicalPage}<span className="mono">{citation.chunkId}</span>
          </button>)}
        </div>
      </li>)}</ul>
    </section>)}
  </div>;
}
