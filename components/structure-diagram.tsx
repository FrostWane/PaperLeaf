"use client";

import { useEffect, useId, useState } from "react";
import { structureNodeTypeLabels } from "@/lib/artifacts";
import type { PaperStructureGraph } from "@/lib/types";
import { artifactCitationSources, CitationMarkers, CitationSources } from "./citation-sources";

export function StructureDiagram({ graph, paperTitle, onOpenPage }: { graph: PaperStructureGraph; paperTitle: string; onOpenPage: (page: number) => void }) {
  const reactId = useId();
  const [svg, setSvg] = useState("");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    void import("mermaid").then(async ({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: "base",
        themeVariables: {
          background: "#ffffff",
          primaryColor: "#edf3f6",
          primaryBorderColor: "#afc3ce",
          primaryTextColor: "#222523",
          lineColor: "#6d7472",
          fontFamily: "Geist, Source Han Sans SC, sans-serif",
        },
        flowchart: { curve: "basis", htmlLabels: false },
      });
      const id = `paperleaf-graph-${reactId.replace(/[^a-zA-Z0-9]/g, "")}`;
      const rendered = await mermaid.render(id, graph.mermaid);
      if (active) { setSvg(rendered.svg); setFailed(false); }
    }).catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [graph, reactId]);

  const sources = artifactCitationSources(graph.nodes.flatMap((node) => node.citations), paperTitle);

  return (
    <div className="structure-figure">
      <div className="structure-canvas" aria-hidden="true">
        {!svg && !failed && <span className="artifact-loading">正在绘制结构…</span>}
        {svg && <div dangerouslySetInnerHTML={{ __html: svg }} />}
        {failed && <span className="artifact-loading">图形渲染失败，仍可使用下方证据目录。</span>}
      </div>
      <ol className="structure-outline" aria-label="结构节点与原文页码">
        {graph.nodes.map((node, index) => <li key={node.id}>
          <button className="structure-node-main" type="button" onClick={() => onOpenPage(node.citations[0].physicalPage)} aria-label={`${structureNodeTypeLabels[node.type]}：${node.label}，查看首条证据 PDF 第 ${node.citations[0].physicalPage} 页`}>
            <span className="structure-index">{String(index + 1).padStart(2, "0")}</span>
            <span><small>{structureNodeTypeLabels[node.type]}</small><strong>{node.label}</strong><em>{node.summary}</em></span>
          </button>
          <CitationMarkers citations={node.citations} sources={sources} label={`${node.label}的原文引用`} onOpen={(source) => onOpenPage(source.page)} />
        </li>)}
      </ol>
      <CitationSources sources={sources} onOpen={(source) => onOpenPage(source.page)} />
    </div>
  );
}
