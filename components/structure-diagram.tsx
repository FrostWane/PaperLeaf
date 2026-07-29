"use client";

import { useEffect, useId, useState } from "react";
import type { PaperStructureGraph } from "@/lib/types";

export function StructureDiagram({ graph, onOpenPage }: { graph: PaperStructureGraph; onOpenPage: (page: number) => void }) {
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

  return (
    <div className="structure-figure">
      <div className="structure-canvas" aria-hidden="true">
        {!svg && !failed && <span className="artifact-loading">正在绘制结构…</span>}
        {svg && <div dangerouslySetInnerHTML={{ __html: svg }} />}
        {failed && <span className="artifact-loading">图形渲染失败，仍可使用下方证据目录。</span>}
      </div>
      <ol className="structure-outline" aria-label="结构节点与原文页码">
        {graph.nodes.map((node, index) => <li key={node.id}><button onClick={() => onOpenPage(node.physicalPage)}><span className="structure-index">{String(index + 1).padStart(2, "0")}</span><span>{node.label}</span><em>PDF {node.physicalPage}</em></button></li>)}
      </ol>
    </div>
  );
}
