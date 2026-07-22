import type { Metadata } from "next";
import { headers } from "next/headers";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import "./globals.css";

function metadataBaseFromRequest(requestHeaders: Headers) {
  const configuredUrl = process.env.NEXT_PUBLIC_SITE_URL;
  if (configuredUrl) {
    const parsedUrl = new URL(configuredUrl);
    if (parsedUrl.protocol === "http:" || parsedUrl.protocol === "https:") return parsedUrl;
  }

  const forwardedHost = requestHeaders.get("x-forwarded-host")?.split(",")[0]?.trim();
  const requestHost = forwardedHost ?? requestHeaders.get("host")?.trim();
  const safeHost = requestHost?.match(/^[a-z0-9.-]+(?::\d+)?$/i)?.[0];
  if (!safeHost) return new URL("http://localhost:3000");

  const forwardedProtocol = requestHeaders.get("x-forwarded-proto")?.split(",")[0]?.trim();
  const protocol = forwardedProtocol === "http" || forwardedProtocol === "https"
    ? forwardedProtocol
    : safeHost.startsWith("localhost")
      ? "http"
      : "https";
  return new URL(`${protocol}://${safeHost}`);
}

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  return {
    metadataBase: metadataBaseFromRequest(requestHeaders),
    title: { default: "PaperLeaf · 让论文真正可追溯地参与思考", template: "%s · PaperLeaf" },
    description: "面向科研人员的开源 PDF 文献库，提供页码级引用、可审计 RAG 与研究 Agent。",
    openGraph: {
      type: "website",
      title: "PaperLeaf · 让每一次回答，都回到论文原页",
      description: "上传、阅读与整理 PDF，让研究 Agent 在可验证的页码证据上回答。",
      images: [{ url: "/paperleaf-social.png", width: 1731, height: 909, alt: "PaperLeaf 文献阅读与证据问答工作台" }],
    },
    twitter: {
      card: "summary_large_image",
      title: "PaperLeaf · 让每一次回答，都回到论文原页",
      description: "上传、阅读与整理 PDF，让研究 Agent 在可验证的页码证据上回答。",
      images: ["/paperleaf-social.png"],
    },
    icons: {
      icon: "/favicon.svg",
      shortcut: "/favicon.svg",
    },
  };
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body
        className={`${GeistSans.variable} ${GeistMono.variable}`}
      >
        {children}
      </body>
    </html>
  );
}
