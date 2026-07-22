"use client";

import { Leaf } from "lucide-react";

export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <span className="brand-lockup">
      <span className="brand-mark" aria-hidden="true"><Leaf size={15} strokeWidth={1.8} /></span>
      {!compact && <span>PaperLeaf</span>}
    </span>
  );
}
