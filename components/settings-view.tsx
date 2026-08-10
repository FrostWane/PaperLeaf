"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Brain, Check, KeyRound, Languages, LogOut, PanelLeft, Pin, Plus, Shield, SlidersHorizontal, Trash2, Type, UserRound } from "lucide-react";
import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import {
  applyFontScale,
  demoCurrentUser,
  getUserPreferences,
  logout as logoutSession,
  updatePassword,
  updateUserPreferences,
  type FontScale,
  type UserPreferences,
} from "@/lib/preferences-api";
import { clearCurrentUserState } from "./current-user-provider";
import {
  clearMemories,
  createMemory,
  deleteMemory,
  listMemories,
  updateMemory,
  type MemoryItem,
  type MemoryType,
} from "@/lib/memory-api";

const passwordSchema = z.object({
  currentPassword: z.string().min(8, "请输入当前密码"),
  newPassword: z.string().min(12, "新密码至少 12 位"),
  confirmPassword: z.string(),
}).refine((value) => value.newPassword === value.confirmPassword, {
  message: "两次输入的新密码不一致",
  path: ["confirmPassword"],
});

type PasswordValues = z.infer<typeof passwordSchema>;
type SettingsState = UserPreferences & { displayName: string };

const fontOptions: Array<{ value: FontScale; label: string; hint: string }> = [
  { value: "small", label: "小", hint: "适合高密度浏览" },
  { value: "standard", label: "标准", hint: "默认阅读字号" },
  { value: "large", label: "大", hint: "适合 2K 与远距离阅读" },
];

const demoSettings: SettingsState = {
  displayName: demoCurrentUser.displayName,
  ...demoCurrentUser.preferences,
};

function PreferenceSwitch({ checked, onChange, label, disabled = false }: { checked: boolean; onChange: (next: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button type="button" className={checked ? "switch on" : "switch"} role="switch" aria-checked={checked} aria-label={label} disabled={disabled} onClick={() => onChange(!checked)}>
      <span />
    </button>
  );
}

export function SettingsView() {
  const usesDemoData = process.env.NEXT_PUBLIC_DATA_MODE !== "real";
  const [settings, setSettings] = useState<SettingsState>(demoSettings);
  const [isLoading, setIsLoading] = useState(!usesDemoData);
  const [preferencesReady, setPreferencesReady] = useState(usesDemoData);
  const [isSaving, setIsSaving] = useState(false);
  const [isLoggingOut, setIsLoggingOut] = useState(false);
  const [settingsMessage, setSettingsMessage] = useState("");
  const [settingsError, setSettingsError] = useState("");
  const [passwordMessage, setPasswordMessage] = useState("");
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [memoryCapacity, setMemoryCapacity] = useState(200);
  const [memoryDraft, setMemoryDraft] = useState("");
  const [memoryType, setMemoryType] = useState<MemoryType>("preference");
  const [memoryBusy, setMemoryBusy] = useState(false);
  const [memoryError, setMemoryError] = useState("");
  const { register, handleSubmit, reset, formState: { errors, isSubmitting } } = useForm<PasswordValues>({
    resolver: zodResolver(passwordSchema),
    defaultValues: { currentPassword: "", newPassword: "", confirmPassword: "" },
  });

  useEffect(() => {
    let active = true;
    if (usesDemoData) {
      applyFontScale(demoCurrentUser.preferences.fontScale);
      return () => { active = false; };
    }
    void getUserPreferences()
      .then((loaded) => {
        if (!active) return;
        const next: SettingsState = { ...loaded, displayName: loaded.displayName?.trim() || "研究者" };
        setSettings(next);
        setPreferencesReady(true);
        applyFontScale(next.fontScale);
      })
      .catch((error) => {
        if (active) setSettingsError(error instanceof Error ? error.message : "个人设置读取失败");
      })
      .finally(() => {
        if (active) setIsLoading(false);
      });
    return () => { active = false; };
  }, [usesDemoData]);

  useEffect(() => {
    if (usesDemoData) return;
    let active = true;
    void listMemories()
      .then((result) => {
        if (!active) return;
        setMemories(result.items);
        setMemoryCapacity(result.capacity);
      })
      .catch((error) => {
        if (active) setMemoryError(error instanceof Error ? error.message : "长期记忆读取失败");
      });
    return () => { active = false; };
  }, [usesDemoData]);

  function updateSetting<K extends keyof SettingsState>(key: K, value: SettingsState[K]) {
    setSettings((current) => ({ ...current, [key]: value }));
    setSettingsMessage("");
    setSettingsError("");
    if (key === "fontScale") applyFontScale(value as FontScale);
  }

  async function saveSettings() {
    const displayName = settings.displayName.trim();
    if (!displayName) {
      setSettingsError("昵称不能为空");
      return;
    }
    setIsSaving(true);
    setSettingsMessage("");
    setSettingsError("");
    try {
      const saved = usesDemoData ? { ...settings, displayName } : await updateUserPreferences({ ...settings, displayName });
      const next: SettingsState = { ...saved, displayName: saved.displayName?.trim() || displayName };
      setSettings(next);
      applyFontScale(next.fontScale);
      window.dispatchEvent(new CustomEvent("paperleaf:profile-updated", {
        detail: {
          displayName: next.displayName,
          preferences: {
            fontScale: next.fontScale,
            pdfZoom: next.pdfZoom,
            leftPanelOpen: next.leftPanelOpen,
            assistantPanelOpen: next.assistantPanelOpen,
            translationLanguage: next.translationLanguage,
            arxivSearchEnabled: next.arxivSearchEnabled,
            memoryEnabled: next.memoryEnabled,
          },
        },
      }));
      setSettingsMessage(usesDemoData ? "演示偏好已应用到当前页面。" : "个人设置已保存，并会在下次打开工作区时继续使用。");
    } catch (error) {
      setSettingsError(error instanceof Error ? error.message : "个人设置保存失败");
    } finally {
      setIsSaving(false);
    }
  }

  async function addMemory() {
    const value = memoryDraft.trim();
    if (value.length < 2 || memoryBusy) return;
    setMemoryBusy(true);
    setMemoryError("");
    try {
      const created = usesDemoData
        ? { id: crypto.randomUUID(), type: memoryType, value, confidence: 1, sourceKind: "manual", sourceExcerpt: "由用户手动创建", pinned: false, enabled: true, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() }
        : await createMemory(memoryType, value);
      setMemories((items) => [created, ...items.filter((item) => item.id !== created.id)]);
      setMemoryDraft("");
    } catch (error) {
      setMemoryError(error instanceof Error ? error.message : "长期记忆保存失败");
    } finally {
      setMemoryBusy(false);
    }
  }

  async function saveMemory(item: MemoryItem, changes: Partial<Pick<MemoryItem, "value" | "pinned" | "enabled">>) {
    if (memoryBusy) return;
    setMemoryBusy(true);
    setMemoryError("");
    try {
      const updated = usesDemoData ? { ...item, ...changes, updatedAt: new Date().toISOString() } : await updateMemory(item.id, changes);
      setMemories((items) => items.map((candidate) => candidate.id === item.id ? updated : candidate));
    } catch (error) {
      setMemoryError(error instanceof Error ? error.message : "长期记忆更新失败");
    } finally {
      setMemoryBusy(false);
    }
  }

  async function removeMemory(item: MemoryItem) {
    if (!window.confirm("确定删除这条长期记忆吗？删除后不会再进入后续对话。")) return;
    setMemoryBusy(true);
    setMemoryError("");
    try {
      if (!usesDemoData) await deleteMemory(item.id);
      setMemories((items) => items.filter((candidate) => candidate.id !== item.id));
    } catch (error) {
      setMemoryError(error instanceof Error ? error.message : "长期记忆删除失败");
    } finally {
      setMemoryBusy(false);
    }
  }

  async function removeAllMemories() {
    if (!memories.length || !window.confirm("确定清空全部长期记忆吗？该操作不可撤销。")) return;
    setMemoryBusy(true);
    setMemoryError("");
    try {
      if (!usesDemoData) await clearMemories();
      setMemories([]);
    } catch (error) {
      setMemoryError(error instanceof Error ? error.message : "长期记忆清空失败");
    } finally {
      setMemoryBusy(false);
    }
  }

  async function submitPassword(values: PasswordValues) {
    setPasswordMessage("");
    try {
      if (!usesDemoData) await updatePassword(values.currentPassword, values.newPassword);
      else await new Promise((resolve) => window.setTimeout(resolve, 250));
      reset();
      setPasswordMessage("密码已更新，请在下次登录时使用新密码。");
    } catch (error) {
      setPasswordMessage(error instanceof Error ? error.message : "密码修改失败");
    }
  }

  async function handleLogout() {
    setIsLoggingOut(true);
    setSettingsError("");
    try {
      if (!usesDemoData) await logoutSession();
      clearCurrentUserState();
      window.location.replace("/login");
    } catch (error) {
      setSettingsError(error instanceof Error ? error.message : "退出登录失败");
      setIsLoggingOut(false);
    }
  }

  return (
    <div className="settings-layout" aria-busy={isLoading}>
      <nav className="settings-nav" aria-label="设置分类">
        <a href="#profile" className="active">个人资料</a>
        <a href="#reading">阅读体验</a>
        <a href="#agent">AI 与翻译</a>
        <a href="#memory">长期记忆</a>
        <a href="#change-password">密码</a>
        <a href="#privacy">隐私与安全</a>
      </nav>
      <div className="settings-content">
        {isLoading && <p className="settings-feedback" role="status">正在读取个人设置…</p>}
        {settingsError && <p className="settings-feedback error" role="alert">{settingsError}</p>}
        {settingsMessage && <p className="settings-feedback success" role="status"><Check size={17} />{settingsMessage}</p>}

        <section id="profile" className="settings-section">
          <div className="setting-title"><UserRound size={20} /><div><h2>个人资料</h2><p>用于工作区左下角的账户标识，不会写入论文元数据。</p></div></div>
          <div className="settings-form-row">
            <label htmlFor="display-name"><strong>昵称</strong><small>建议使用容易辨认的姓名或研究代号。</small></label>
            <input id="display-name" value={settings.displayName} maxLength={100} disabled={!preferencesReady} onChange={(event) => updateSetting("displayName", event.target.value)} />
          </div>
        </section>

        <section id="reading" className="settings-section">
          <div className="setting-title"><SlidersHorizontal size={20} /><div><h2>阅读体验</h2><p>控制界面字号和进入论文工作台时的默认布局。</p></div></div>
          <div className="settings-form-row stacked">
            <div><strong><Type size={16} />界面字号</strong><small>选择后立即预览，保存后同步到你的账户。</small></div>
            <div className="font-scale-options" role="radiogroup" aria-label="界面字号">
              {fontOptions.map((option) => (
                <button key={option.value} type="button" role="radio" aria-checked={settings.fontScale === option.value} className={settings.fontScale === option.value ? "selected" : ""} disabled={!preferencesReady} onClick={() => updateSetting("fontScale", option.value)}>
                  <strong>{option.label}</strong><small>{option.hint}</small>
                </button>
              ))}
            </div>
          </div>
          <div className="settings-form-row">
            <label htmlFor="pdf-zoom"><strong>默认 PDF 缩放</strong><small>进入阅读器时采用的初始比例，可在工具栏继续调整。</small></label>
            <div className="zoom-control"><input id="pdf-zoom" type="range" min="50" max="200" step="10" value={settings.pdfZoom} disabled={!preferencesReady} onChange={(event) => updateSetting("pdfZoom", Number(event.target.value))} /><output htmlFor="pdf-zoom">{settings.pdfZoom}%</output></div>
          </div>
          <div className="setting-row"><span><strong><PanelLeft size={16} />默认展开文献资料</strong><small>进入论文时显示作者、摘要和页码导航。</small></span><PreferenceSwitch checked={settings.leftPanelOpen} onChange={(value) => updateSetting("leftPanelOpen", value)} label="默认展开文献资料" disabled={!preferencesReady} /></div>
          <div className="setting-row"><span><strong>默认展开论文助手</strong><small>进入论文时显示问答、概览和结构图面板。</small></span><PreferenceSwitch checked={settings.assistantPanelOpen} onChange={(value) => updateSetting("assistantPanelOpen", value)} label="默认展开论文助手" disabled={!preferencesReady} /></div>
        </section>

        <section id="agent" className="settings-section">
          <div className="setting-title"><Languages size={20} /><div><h2>AI 与翻译</h2><p>控制需要联网或调用模型的个人功能偏好。</p></div></div>
          <div className="settings-form-row">
            <label htmlFor="translation-language"><strong>全文翻译目标语言</strong><small>创建翻译任务时默认使用，可在任务确认框中再次核对。</small></label>
            <select id="translation-language" value={settings.translationLanguage} disabled={!preferencesReady} onChange={(event) => updateSetting("translationLanguage", event.target.value)}>
              <option value="zh-CN">简体中文</option>
              <option value="zh-TW">繁体中文</option>
              <option value="en">英语</option>
              <option value="ja">日语</option>
              <option value="ko">韩语</option>
            </select>
          </div>
          <div className="setting-row"><span><strong>允许联网学术搜索</strong><small>发现页使用 arXiv；Agent 可按问题调用 arXiv、OpenAlex 或 Semantic Scholar。不会上传 PDF，下载导入前仍需确认。</small></span><PreferenceSwitch checked={settings.arxivSearchEnabled} onChange={(value) => updateSetting("arxivSearchEnabled", value)} label="允许联网学术搜索" disabled={!preferencesReady} /></div>
        </section>

        <section id="memory" className="settings-section">
          <div className="setting-title"><Brain size={20} /><div><h2>长期记忆</h2><p>用于跨会话保留你的明确偏好和研究方向，不会把论文内容当成记忆。</p></div></div>
          <div className="setting-row"><span><strong>允许 Agent 使用长期记忆</strong><small>关闭后保留既有条目，但不会读取或自动新增。</small></span><PreferenceSwitch checked={settings.memoryEnabled} onChange={(value) => updateSetting("memoryEnabled", value)} label="允许 Agent 使用长期记忆" disabled={!preferencesReady} /></div>
          <div className="memory-create">
            <select aria-label="新记忆类型" value={memoryType} onChange={(event) => setMemoryType(event.target.value as MemoryType)}>
              <option value="preference">表达偏好</option><option value="research_interest">研究方向</option><option value="entity_alias">实体别名</option><option value="workflow">工作习惯</option><option value="pinned_context">固定背景</option>
            </select>
            <input aria-label="新增长期记忆" value={memoryDraft} maxLength={2000} placeholder="例如：回答默认使用中文" onChange={(event) => setMemoryDraft(event.target.value)} />
            <button type="button" className="secondary-button" disabled={memoryBusy || memoryDraft.trim().length < 2} onClick={() => void addMemory()}><Plus size={16} />添加</button>
          </div>
          <div className="memory-toolbar"><span>{memories.filter((item) => item.enabled).length} 条启用 / {memories.length} 条，共 {memoryCapacity} 条容量</span><button type="button" className="text-button danger" disabled={memoryBusy || memories.length === 0} onClick={() => void removeAllMemories()}>清空全部</button></div>
          {memoryError && <p className="settings-feedback error" role="alert">{memoryError}</p>}
          <div className="memory-list">
            {memories.length === 0 && <p className="memory-empty">还没有长期记忆。你也可以在对话中说“记住……”。</p>}
            {memories.map((item) => <article key={item.id} className={item.enabled ? "memory-item" : "memory-item disabled"}>
              <div><span>{item.type === "research_interest" ? "研究方向" : item.type === "entity_alias" ? "实体别名" : item.type === "workflow" ? "工作习惯" : item.type === "pinned_context" ? "固定背景" : "表达偏好"}</span><small title={item.sourceExcerpt}>来源：{item.sourceKind === "manual" ? "手动创建" : item.sourceKind === "explicit" ? "明确要求记住" : "用户原话"}</small></div>
              <textarea aria-label={`编辑记忆 ${item.value}`} value={item.value} disabled={memoryBusy} onChange={(event) => setMemories((items) => items.map((candidate) => candidate.id === item.id ? { ...candidate, value: event.target.value } : candidate))} />
              <div className="memory-actions"><button type="button" className="text-button" disabled={memoryBusy || item.value.trim().length < 2} onClick={() => void saveMemory(item, { value: item.value.trim() })}>保存</button><button type="button" className="icon-button" aria-label={item.pinned ? "取消固定" : "固定记忆"} disabled={memoryBusy} onClick={() => void saveMemory(item, { pinned: !item.pinned })}><Pin size={15} fill={item.pinned ? "currentColor" : "none"} /></button><PreferenceSwitch checked={item.enabled} onChange={(enabled) => void saveMemory(item, { enabled })} label={`${item.enabled ? "停用" : "启用"}记忆`} disabled={memoryBusy} /><button type="button" className="icon-button danger" aria-label="删除记忆" disabled={memoryBusy} onClick={() => void removeMemory(item)}><Trash2 size={15} /></button></div>
            </article>)}
          </div>
        </section>

        <button className="primary-button save-settings" type="button" disabled={!preferencesReady || isSaving} onClick={saveSettings}>{isSaving ? "正在保存…" : "保存个人设置"}</button>

        <section id="change-password" className="settings-section">
          <div className="setting-title"><KeyRound size={20} /><div><h2>修改密码</h2><p>临时密码首次登录后必须修改，新密码至少 12 位。</p></div></div>
          <form className="password-form" onSubmit={handleSubmit(submitPassword)}>
            <label><span>当前密码</span><input type="password" autoComplete="current-password" {...register("currentPassword")} />{errors.currentPassword && <small className="field-error">{errors.currentPassword.message}</small>}</label>
            <label><span>新密码</span><input type="password" autoComplete="new-password" {...register("newPassword")} />{errors.newPassword && <small className="field-error">{errors.newPassword.message}</small>}</label>
            <label><span>确认新密码</span><input type="password" autoComplete="new-password" {...register("confirmPassword")} />{errors.confirmPassword && <small className="field-error">{errors.confirmPassword.message}</small>}</label>
            <button className="primary-button" disabled={isSubmitting}>{isSubmitting ? "正在更新…" : "更新密码"}</button>
            {passwordMessage && <p className="form-note" role="status">{passwordMessage}</p>}
          </form>
        </section>

        <section id="privacy" className="settings-section">
          <div className="setting-title"><Shield size={20} /><div><h2>隐私与安全</h2><p>管理员只能查看运行状态，默认不能打开你的论文或对话。</p></div></div>
          <div className="privacy-note"><Check size={17} />PDF 原件通过鉴权接口访问，并支持范围请求。</div>
          <div className="setting-row"><span><strong>当前登录会话</strong><small>退出后需要重新输入邮箱和密码，未保存的设置不会保留。</small></span><button type="button" className="secondary-button session-logout" disabled={isLoggingOut} onClick={handleLogout}><LogOut size={17} />{isLoggingOut ? "正在退出…" : "退出登录"}</button></div>
        </section>
      </div>
    </div>
  );
}
