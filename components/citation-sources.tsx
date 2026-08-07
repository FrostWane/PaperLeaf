"use client";

import type { ArtifactCitation, Citation } from "@/lib/types";

export interface CitationSource {
  key: string;
  paperTitle: string;
  page: number;
  quote?: string;
}

export function uniqueCitationSources(sources: CitationSource[]): CitationSource[] {
  const seen = new Set<string>();
  return sources.filter((source) => {
    if (seen.has(source.key)) return false;
    seen.add(source.key);
    return true;
  });
}

export function chatCitationSources(citations: Citation[]): CitationSource[] {
  return uniqueCitationSources(citations.map((citation) => ({
    key: citation.chunkId,
    paperTitle: citation.paperTitle,
    page: citation.page,
    quote: citation.quote,
  })));
}

export function artifactCitationSources(citations: ArtifactCitation[], paperTitle: string): CitationSource[] {
  return uniqueCitationSources(citations.map((citation) => ({
    key: citation.chunkId,
    paperTitle,
    page: citation.physicalPage,
    quote: citation.quote,
  })));
}

export function CitationMarkers({
  citations,
  sources,
  label,
  onOpen,
}: {
  citations: ArtifactCitation[];
  sources: CitationSource[];
  label: string;
  onOpen: (source: CitationSource) => void;
}) {
  const indexes = new Map(sources.map((source, index) => [source.key, index]));
  const visible = uniqueCitationSources(citations.flatMap((citation) => {
    const index = indexes.get(citation.chunkId);
    return index === undefined ? [] : [sources[index]];
  }));
  if (visible.length === 0) return null;
  return <span className="citation-markers" aria-label={label}>
    {visible.map((source) => {
      const number = (indexes.get(source.key) ?? 0) + 1;
      return <button key={source.key} type="button" className="inline-citation" aria-label={`引用 [${number}]，查看 PDF 第 ${source.page} 页`} onClick={() => onOpen(source)}>[{number}]</button>;
    })}
  </span>;
}

export function CitationSources({ sources, onOpen }: { sources: CitationSource[]; onOpen: (source: CitationSource) => void }) {
  const uniqueSources = uniqueCitationSources(sources);
  if (uniqueSources.length === 0) return null;
  return <div className="chat-citations citation-sources" aria-label={`引用来源，共 ${uniqueSources.length} 条`}>
    <p className="chat-citations-title">引用来源 · {uniqueSources.length}</p>
    {uniqueSources.map((source, index) => <button type="button" key={source.key} title={`打开《${source.paperTitle}》PDF 第 ${source.page} 页`} onClick={() => onOpen(source)}>
      <span>{index + 1}</span>
      <span><strong>{source.paperTitle}</strong>{source.quote && <small>{source.quote}</small>}</span>
      <em>PDF 第 {source.page} 页</em>
    </button>)}
  </div>;
}
