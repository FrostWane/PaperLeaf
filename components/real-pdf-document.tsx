"use client";

import { useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";

const PDF_OPTIONS = { withCredentials: true } as const;
const DEFAULT_PDF_ERROR = "PDF 加载失败，请刷新页面后重试。";

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString();

function describePdfError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error ?? "");
  if (/\b401\b|unauthori[sz]ed/i.test(message)) {
    return "登录状态已失效，请重新登录后再打开 PDF。";
  }
  if (/API version.*Worker version/i.test(message)) {
    return "PDF 阅读器组件版本不一致，请刷新页面或联系维护者。";
  }
  return DEFAULT_PDF_ERROR;
}

export function RealPdfDocument({ url, page, onPageCount }: { url: string; page: number; onPageCount: (count: number) => void }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(620);
  const [loadError, setLoadError] = useState<{ url: string; message: string } | null>(null);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const update = () => setWidth(Math.max(260, Math.min(960, element.clientWidth - 36)));
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  function handleLoadError(error: unknown) {
    console.error("PaperLeaf PDF load failed", error);
    setLoadError({ url, message: describePdfError(error) });
  }

  return (
    <div className="real-pdf" ref={containerRef}>
      <Document
        file={url}
        options={PDF_OPTIONS}
        loading={<p role="status">正在载入 PDF…</p>}
        error={<p role="alert">{loadError?.url === url ? loadError.message : DEFAULT_PDF_ERROR}</p>}
        onLoadError={handleLoadError}
        onSourceError={handleLoadError}
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
