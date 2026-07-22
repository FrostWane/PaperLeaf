"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { ArrowRight, Eye, EyeOff, LockKeyhole, Mail } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { login } from "@/lib/data-source";

const schema = z.object({ email: z.email("请输入有效邮箱"), password: z.string().min(8, "密码至少 8 位") });
type Values = z.infer<typeof schema>;

export function LoginForm() {
  const [showPassword, setShowPassword] = useState(false);
  const [message, setMessage] = useState("");
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { email: "demo@paperleaf.local", password: "paperleaf-demo" } });
  async function submit(values: Values) { setMessage(""); try { const user = process.env.NEXT_PUBLIC_DATA_MODE === "real" ? await login(values.email, values.password) : null; const destination = user?.mustChangePassword ? "/settings#change-password" : "/library"; setMessage(user?.mustChangePassword ? "首次登录需要先修改临时密码…" : "验证成功，正在进入文献库…"); window.setTimeout(() => { window.location.href = destination; }, 350); } catch (error) { setMessage(error instanceof Error ? error.message : "登录失败"); } }
  return <form className="login-form" onSubmit={handleSubmit(submit)}><div className="form-field"><label htmlFor="email">邮箱</label><div className="input-with-icon"><Mail size={16} /><input id="email" autoComplete="email" {...register("email")} /></div>{errors.email && <span className="field-error">{errors.email.message}</span>}</div><div className="form-field"><label htmlFor="password">密码</label><div className="input-with-icon"><LockKeyhole size={16} /><input id="password" type={showPassword ? "text" : "password"} autoComplete="current-password" {...register("password")} /><button type="button" className="field-icon" onClick={() => setShowPassword((value) => !value)} aria-label={showPassword ? "隐藏密码" : "显示密码"}>{showPassword ? <EyeOff size={16} /> : <Eye size={16} />}</button></div>{errors.password && <span className="field-error">{errors.password.message}</span>}</div><button className="primary-button login-submit" disabled={isSubmitting}>进入工作区 <ArrowRight size={16} /></button>{message && <p role="status" className="form-success">{message}</p>}<p className="login-help">PaperLeaf 默认关闭公开注册，账号由工作区管理员创建。</p></form>;
}
