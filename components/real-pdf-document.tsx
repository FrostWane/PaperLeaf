"use client";

import { useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString();

export function RealPdfDocument({ url, page, onPageCount }: { url: string; page: number; onPageCount: (count: number) => void }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(620);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const update = () => setWidth(Math.max(260, Math.min(960, element.clientWidth - 36)));
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  return (
    <div className="real-pdf" ref={containerRef}>
      <Document
        file={url}
        options={{ withCredentials: true }}
        loading={<p role="status">正在载入 PDF…</p>}
        error={<p role="alert">PDF 暂时无法显示，请检查登录状态或文件是否仍在处理。</p>}
        onLoadSuccess={({ numPages }) => onPageCount(numPages)}
      >
        <Page
          pageNumber={page}
          width={width}
          renderAnnotationLayer
          renderTextLayer
        />
      </Document>
    </div>
  );
}
