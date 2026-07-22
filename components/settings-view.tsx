"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Check, Database, KeyRound, Laptop, Shield, Wifi } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { changePassword } from "@/lib/data-source";

const passwordSchema = z.object({
  currentPassword: z.string().min(8, "请输入当前密码"),
  newPassword: z.string().min(12, "新密码至少 12 位"),
  confirmPassword: z.string(),
}).refine((value) => value.newPassword === value.confirmPassword, {
  message: "两次输入的新密码不一致",
  path: ["confirmPassword"],
});

type PasswordValues = z.infer<typeof passwordSchema>;

export function SettingsView() {
  const [web, setWeb] = useState(true);
  const [compact, setCompact] = useState(false);
  const [saved, setSaved] = useState(false);
  const [passwordMessage, setPasswordMessage] = useState("");
  const { register, handleSubmit, reset, formState: { errors, isSubmitting } } = useForm<PasswordValues>({
    resolver: zodResolver(passwordSchema),
    defaultValues: { currentPassword: "", newPassword: "", confirmPassword: "" },
  });

  async function submitPassword(values: PasswordValues) {
    setPasswordMessage("");
    try {
      if (process.env.NEXT_PUBLIC_DATA_MODE === "real") {
        await changePassword(values.currentPassword, values.newPassword);
      } else {
        await new Promise((resolve) => window.setTimeout(resolve, 300));
      }
      reset();
      setPasswordMessage("密码已更新，现在可以使用全部功能。 ");
    } catch (error) {
      setPasswordMessage(error instanceof Error ? error.message : "密码修改失败");
    }
  }

  return (
    <div className="settings-layout">
      <nav className="settings-nav">
        <a href="#profile" className="active">个人偏好</a>
        <a href="#models">模型连接</a>
        <a href="#change-password">密码</a>
        <a href="#privacy">隐私与安全</a>
      </nav>
      <div className="settings-content">
        <section id="profile" className="settings-section">
          <div className="setting-title"><Laptop size={19} /><div><h2>阅读偏好</h2><p>只保存在你的个人工作区。</p></div></div>
          <label className="setting-row"><span><strong>紧凑文献列表</strong><small>减少行高，在同一屏展示更多论文。</small></span><button className={compact ? "switch on" : "switch"} role="switch" aria-checked={compact} onClick={() => setCompact(!compact)}><span /></button></label>
          <label className="setting-row"><span><strong>允许 Agent 搜索 arXiv</strong><small>仅搜索候选；导入前仍然需要确认。</small></span><button className={web ? "switch on" : "switch"} role="switch" aria-checked={web} onClick={() => setWeb(!web)}><span /></button></label>
        </section>
        <section id="models" className="settings-section">
          <div className="setting-title"><KeyRound size={19} /><div><h2>模型连接</h2><p>密钥只保存在服务端环境变量中。</p></div></div>
          <div className="connection-row"><span className="connection-icon"><Wifi size={17} /></span><span><strong>OpenAI-compatible API</strong><small>由部署者在服务端配置</small></span><span className="status-pill neutral">读取服务端配置</span></div>
          <div className="connection-row"><span className="connection-icon"><Database size={17} /></span><span><strong>Ollama</strong><small>可选本地模型服务</small></span><span className="status-pill neutral">可选</span></div>
        </section>
        <section id="change-password" className="settings-section">
          <div className="setting-title"><KeyRound size={19} /><div><h2>修改密码</h2><p>使用临时密码首次登录时，必须先完成此步骤。</p></div></div>
          <form className="password-form" onSubmit={handleSubmit(submitPassword)}>
            <label><span>当前密码</span><input type="password" autoComplete="current-password" {...register("currentPassword")} />{errors.currentPassword && <small className="field-error">{errors.currentPassword.message}</small>}</label>
            <label><span>新密码</span><input type="password" autoComplete="new-password" {...register("newPassword")} />{errors.newPassword && <small className="field-error">{errors.newPassword.message}</small>}</label>
            <label><span>确认新密码</span><input type="password" autoComplete="new-password" {...register("confirmPassword")} />{errors.confirmPassword && <small className="field-error">{errors.confirmPassword.message}</small>}</label>
            <button className="primary-button" disabled={isSubmitting}>{isSubmitting ? "正在更新" : "更新密码"}</button>
            {passwordMessage && <p className="form-note" role="status">{passwordMessage}</p>}
          </form>
        </section>
        <section id="privacy" className="settings-section">
          <div className="setting-title"><Shield size={19} /><div><h2>隐私与安全</h2><p>管理员只能查看运行状态，默认不能打开你的论文或对话。</p></div></div>
          <div className="privacy-note"><Check size={16} />PDF 原件通过鉴权接口访问，并支持范围请求。</div>
        </section>
        <button className="primary-button save-settings" onClick={() => { setSaved(true); window.setTimeout(() => setSaved(false), 1500); }}>{saved ? <><Check size={15} />已保存</> : "保存偏好"}</button>
      </div>
    </div>
  );
}
