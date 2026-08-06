"use client";

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Citation } from "@/lib/types";

interface MarkdownNode {
  type: string;
  value?: string;
  children?: MarkdownNode[];
  data?: { hName?: string; hProperties?: Record<string, unknown> };
}

function citationRemarkPlugin(options: { citations: Citation[] }) {
  const byChunk = new Map(options.citations.map((citation, index) => [citation.chunkId, index]));
  const marker = /\[\[?chunk:([^\]\s]+)\]\]?|\[\^(\d+)\](?!:)/gi;
  return (tree: MarkdownNode) => {
    const visit = (node: MarkdownNode) => {
      if (["link", "image", "code", "inlineCode", "html"].includes(node.type)) return;
      if (!node.children) return;
      const nextChildren: MarkdownNode[] = [];
      for (const child of node.children) {
        if (child.type !== "text" || !child.value) {
          visit(child);
          nextChildren.push(child);
          continue;
        }
        let cursor = 0;
        marker.lastIndex = 0;
        for (const match of child.value.matchAll(marker)) {
          const start = match.index ?? 0;
          if (start > cursor) nextChildren.push({ type: "text", value: child.value.slice(cursor, start) });
          const index = match[1] ? byChunk.get(match[1]) : Number(match[2]) - 1;
          const citation = index === undefined ? undefined : options.citations[index];
          if (citation) {
            nextChildren.push({
              type: "paperleafCitation",
              data: { hName: "paperleaf-citation", hProperties: { "data-citation-index": index } },
              children: [{ type: "text", value: `PDF ${citation.page}` }],
            });
          }
          cursor = start + match[0].length;
        }
        if (cursor < child.value.length) nextChildren.push({ type: "text", value: child.value.slice(cursor) });
      }
      node.children = nextChildren;
    };
    visit(tree);
  };
}

function safeHref(href: string | undefined): { href?: string; external: boolean; blocked: boolean } {
  if (!href) return { external: false, blocked: true };
  try {
    const url = new URL(href);
    if (url.protocol === "http:" || url.protocol === "https:") return { href, external: true, blocked: false };
  } catch {
    return { external: false, blocked: true };
  }
  return { external: false, blocked: true };
}

export function SafeMarkdown({ content, citations = [], onOpenCitation, className }: {
  content: string;
  citations?: Citation[];
  onOpenCitation?: (citation: Citation) => void;
  className?: string;
}) {
  const components = {
    a: ({ href, children }: { href?: string; children?: React.ReactNode }) => {
      const safe = safeHref(href);
      if (safe.blocked) return <span>{children}</span>;
      return <a href={safe.href} target="_blank" rel="noopener noreferrer">{children}</a>;
    },
    img: () => null,
    "paperleaf-citation": ({ children, ...props }: { children?: React.ReactNode; [key: string]: unknown }) => {
      const citation = citations[Number(props["data-citation-index"])];
      return citation ? <button type="button" className="markdown-citation" onClick={() => onOpenCitation?.(citation)} aria-label={`查看《${citation.paperTitle}》PDF 第 ${citation.page} 页`}>{children}</button> : null;
    },
  } as Components;

  return (
    <div className={className ? `safe-markdown ${className}` : "safe-markdown"}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, [citationRemarkPlugin, { citations }]]}
        skipHtml
        disallowedElements={["img"]}
        unwrapDisallowed
        components={components}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
