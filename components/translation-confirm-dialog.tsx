"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { Languages, X } from "lucide-react";

const languageOptions = [
  { value: "zh-CN", label: "简体中文" },
  { value: "zh-TW", label: "繁体中文" },
  { value: "ja", label: "日语" },
  { value: "ko", label: "韩语" },
  { value: "en", label: "英语" },
];

export function translationLanguageLabel(value: string): string {
  return languageOptions.find((item) => item.value === value)?.label ?? "已配置语言";
}

interface TranslationConfirmDialogProps {
  open: boolean;
  pages: number;
  targetLanguage: string;
  busy: boolean;
  error?: string;
  onTargetLanguageChange: (value: string) => void;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}

export function TranslationConfirmDialog({ open, pages, targetLanguage, busy, error, onTargetLanguageChange, onOpenChange, onConfirm }: TranslationConfirmDialogProps) {
  return (
    <Dialog.Root open={open} onOpenChange={(next) => { if (!busy) onOpenChange(next); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content translation-confirm-dialog" aria-describedby="translation-confirm-description">
          <div className="dialog-head">
            <div><Dialog.Title>翻译整篇论文</Dialog.Title><Dialog.Description id="translation-confirm-description">任务会在后台逐页处理，离开阅读器后仍会继续。</Dialog.Description></div>
            <Dialog.Close className="icon-button" aria-label="关闭翻译确认" disabled={busy}><X size={17} /></Dialog.Close>
          </div>
          <div className="translation-confirm-summary">
            <Languages size={20} />
            <div><strong>{pages > 0 ? `${pages} 页原文` : "页数识别中"}</strong><p>将调用已配置的论文翻译模型。只翻译已解析页面文本，不改写原始 PDF。</p></div>
          </div>
          <label className="translation-language-field" htmlFor="translation-target-language"><span>目标语言</span><select id="translation-target-language" value={targetLanguage} disabled={busy} onChange={(event) => onTargetLanguageChange(event.target.value)}>{languageOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
          <p className="translation-privacy-note">公式、引用编号、专有名词与段落边界会尽量保留；无文本或缺少 OCR 的页面会明确标记。</p>
          {error && <p className="field-error translation-confirm-error" role="alert">{error}</p>}
          <div className="dialog-actions"><Dialog.Close asChild><button className="secondary-button" disabled={busy}>取消</button></Dialog.Close><button type="button" className="primary-button" disabled={busy || pages <= 0} onClick={onConfirm}>{busy ? "正在创建任务…" : "确认并开始翻译"}</button></div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
