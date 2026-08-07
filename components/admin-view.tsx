"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";
import { Activity, Plus, RefreshCw, UserX, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { createAdminUser, getAdminModelHealth, listAdminJobs, listAdminUsers, retryAdminJob, setAdminUserActive } from "@/lib/data-source";
import { users as fixtureUsers } from "@/lib/fixtures";
import type { AdminJob, ModelRuntimeHealth, UserRecord } from "@/lib/types";

const helper = createColumnHelper<UserRecord>();
const demoJobs: AdminJob[] = [{ id: "job-demo", paperId: "attention", type: "parse_pdf", status: "running", progress: 68, attempts: 1, maxAttempts: 3 }];
const demoHealth: ModelRuntimeHealth = {
  configured: true,
  providers: [{
    provider: "primary",
    purposes: Object.fromEntries(["answer", "evidence_support", "summary", "translation", "embedding"].map((purpose) => [purpose, { configured: true, status: "closed", consecutiveFailures: 0, retryAfterMs: 0 }])),
  }],
  policy: { timeoutSeconds: 30, attemptsPerProvider: 2, failureThreshold: 3, cooldownSeconds: 60 },
};

const purposeCopy: Record<string, { label: string; description: string }> = {
  answer: { label: "回答生成", description: "根据检索到的论文证据组织回答" },
  evidence_support: { label: "证据核验", description: "检查回答中的结论是否有原文支持" },
  summary: { label: "论文总结", description: "提炼研究问题、方法、结果与局限" },
  translation: { label: "全文翻译", description: "按物理页翻译已解析的论文文本" },
  embedding: { label: "向量检索", description: "按语义查找与问题相关的论文段落" },
  vision: { label: "视觉 OCR", description: "识别扫描版 PDF 页面中的文字" },
};
const visiblePurposeNames = ["answer", "evidence_support", "summary", "translation", "embedding", "vision"] as const;

const jobTypeLabels: Record<string, string> = {
  parse_pdf: "解析 PDF",
  delete_paper: "删除文献",
  import_arxiv: "导入论文",
  import_paper: "导入论文",
  translate_paper: "翻译全文",
  agent_run: "运行问答",
  summarize_paper: "生成论文概括",
  build_structure_graph: "生成研究脑图",
};

const jobErrorLabels: Record<string, string> = {
  PDF_PARSE_FAILED: "PDF 解析失败",
  PAPER_NOT_FOUND: "关联文献不存在",
  PDF_ENCRYPTED: "PDF 已加密，无法处理",
  PDF_INVALID: "文件不是有效的 PDF",
  MODEL_TIMEOUT: "AI 服务响应超时",
  MODEL_NOT_CONFIGURED: "尚未配置所需的 AI 服务",
};

function jobStatusCopy(job: AdminJob): string {
  if (job.status === "queued") return "等待处理";
  if (job.status === "completed") return "已完成";
  if (job.status === "running") {
    return `处理进度 ${job.progress}% · 第 ${Math.max(job.attempts, 1)} 次执行，最多 ${job.maxAttempts} 次`;
  }
  const reason = job.errorMessage
    ?? (job.errorCode ? jobErrorLabels[job.errorCode] ?? `任务执行失败（错误代码：${job.errorCode}）` : "任务执行失败，请检查服务状态后重试");
  return `处理失败 · 已尝试 ${job.attempts} 次 · 失败原因：${reason}`;
}

function jobProgressLabel(job: AdminJob): string {
  if (job.status === "queued") return "等待处理";
  if (job.status === "completed") return "已完成";
  if (job.status === "failed") return "失败";
  return `${job.progress}%`;
}

export function AdminView() {
  const real = process.env.NEXT_PUBLIC_DATA_MODE === "real";
  const [users, setUsers] = useState(fixtureUsers);
  const [jobs, setJobs] = useState(demoJobs);
  const [modelHealth, setModelHealth] = useState<ModelRuntimeHealth>(demoHealth);
  const [message, setMessage] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [pendingDeactivation, setPendingDeactivation] = useState<UserRecord | null>(null);
  const [email, setEmail] = useState("");
  const [temporaryPassword, setTemporaryPassword] = useState("");

  const refresh = useCallback(async () => {
    if (!real) return;
    setMessage("正在刷新管理数据…");
    try {
      const [nextUsers, nextJobs, nextModelHealth] = await Promise.all([listAdminUsers(), listAdminJobs(), getAdminModelHealth()]);
      setUsers(nextUsers);
      setJobs(nextJobs);
      setModelHealth(nextModelHealth);
      setMessage("");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "管理数据读取失败");
    }
  }, [real]);

  useEffect(() => { void refresh(); }, [refresh]);

  const toggleUser = useCallback(async (user: UserRecord) => {
    const disabling = user.status === "正常";
    if (!real) {
      if (user.role === "管理员" && users.filter((item) => item.role === "管理员" && item.status === "正常").length <= 1) {
        setMessage("不能停用最后一名管理员");
        return;
      }
      setUsers((items) => items.map((item) => item.id === user.id ? { ...item, status: item.status === "正常" ? "已停用" : "正常" } : item));
      setMessage(disabling ? `已停用用户 ${user.name}` : `已启用用户 ${user.name}`);
      return;
    }
    try {
      const updated = await setAdminUserActive(user.id, user.status !== "正常");
      setUsers((items) => items.map((item) => item.id === updated.id ? updated : item));
    } catch (error) { setMessage(error instanceof Error ? error.message : "用户状态更新失败"); }
  }, [real, users]);

  async function createUser() {
    if (!email.includes("@") || temporaryPassword.length < 12) { setMessage("请输入有效邮箱和至少 12 位临时密码"); return; }
    if (!real) {
      setUsers((items) => [...items, { id: `u${items.length + 1}`, name: email.split("@")[0], email, role: "用户", status: "正常", papers: 0 }]);
    } else {
      try { const created = await createAdminUser(email, temporaryPassword); setUsers((items) => [...items, created]); }
      catch (error) { setMessage(error instanceof Error ? error.message : "用户创建失败"); return; }
    }
    setEmail(""); setTemporaryPassword(""); setShowCreate(false); setMessage("用户已创建，首次登录必须修改临时密码。 ");
  }

  const columns = useMemo(() => [
    helper.accessor("name", { header: "用户", cell: ({ row }) => <div className="admin-user"><span className="avatar small">{row.original.name.slice(0, 1)}</span><span><strong>{row.original.name}</strong><small>{row.original.email}</small></span></div> }),
    helper.accessor("role", { header: "角色" }),
    helper.accessor("papers", { header: "文献数", cell: (info) => <span className="mono">{real ? "—" : info.getValue()}</span> }),
    helper.accessor("status", { header: "状态", cell: (info) => <span className={info.getValue() === "正常" ? "status-pill ready" : "status-pill partial"}><span>{info.getValue() === "正常" ? "✓" : "!"}</span>{info.getValue()}</span> }),
    helper.display({ id: "actions", header: "操作", cell: ({ row }) => <button type="button" className="secondary-button" aria-label={`${row.original.status === "正常" ? "停用用户" : "启用用户"} ${row.original.name}`} onClick={() => row.original.status === "正常" ? setPendingDeactivation(row.original) : void toggleUser(row.original)}>{row.original.status === "正常" ? "停用用户" : "启用用户"}</button> }),
  ], [real, toggleUser]);
  const table = useReactTable({ data: users, columns, getCoreRowModel: getCoreRowModel() });
  const activeUsers = users.filter((user) => user.status === "正常").length;
  const runningJobs = jobs.filter((job) => job.status === "running" || job.status === "queued").length;
  const modelCircuitOpen = modelHealth.providers.some((provider) => Object.values(provider.purposes).some((purpose) => purpose.configured && purpose.status === "open"));
  const modelState = !modelHealth.configured ? "降级模式" : modelCircuitOpen ? "需检查" : "正常";
  const runtimeProviders = modelHealth.providers.length > 0
    ? modelHealth.providers
    : [{ provider: "unconfigured", purposes: {} }];

  return <div className="admin-layout">
    <div className="metric-row"><article><span>活跃用户</span><strong>{activeUsers}</strong><small>共 {users.length} 个账号</small></article><article><span>处理中任务</span><strong>{runningJobs}</strong><small>仅展示任务元数据</small></article><article><span>AI 服务</span><strong>{modelState}</strong><small>{modelHealth.providers.length || 0} 个已配置服务</small></article><article><span>内容访问</span><strong>关闭</strong><small>管理员默认不可读取</small></article></div>
    {message && <p className="admin-message" role="status">{message}</p>}
    <Dialog.Root open={Boolean(pendingDeactivation)} onOpenChange={(open) => !open && setPendingDeactivation(null)}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content">
          <div className="dialog-head"><div><Dialog.Title>确认停用用户</Dialog.Title><Dialog.Description>停用后，该用户的现有会话将失效，重新启用后才能再次登录。</Dialog.Description></div><Dialog.Close className="icon-button" aria-label="关闭"><X size={17} /></Dialog.Close></div>
          <div className="confirm-paper"><span>即将停用</span><strong>{pendingDeactivation?.name}</strong><p>{pendingDeactivation?.email}</p></div>
          <div className="dialog-actions"><Dialog.Close asChild><button type="button" className="secondary-button">取消</button></Dialog.Close><button type="button" className="danger-button" onClick={() => { const user = pendingDeactivation; setPendingDeactivation(null); if (user) void toggleUser(user); }}><UserX size={15} />确认停用</button></div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
    <section className="admin-section"><div className="section-bar"><div><span className="eyebrow">账号与访问权限</span><h2>用户与权限</h2></div><button className="primary-button" onClick={() => setShowCreate((value) => !value)}><Plus size={15} />创建用户</button></div>
      {showCreate && <div className="admin-create"><label>邮箱<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} /></label><label>临时密码<input type="password" value={temporaryPassword} onChange={(event) => setTemporaryPassword(event.target.value)} /></label><button className="primary-button" onClick={() => void createUser()}>保存用户</button></div>}
      <div className="table-scroll"><table className="data-table admin-table"><thead>{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => <th key={header.id}>{flexRender(header.column.columnDef.header, header.getContext())}</th>)}</tr>)}</thead><tbody>{table.getRowModel().rows.map((row) => <tr key={row.id}>{row.getVisibleCells().map((cell) => <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody></table></div>
    </section>
    <section className="admin-section model-runtime"><div className="section-bar"><div><span className="eyebrow">AI 服务可用性</span><h2>AI 能力状态</h2></div><small>单次调用超时 {modelHealth.policy.timeoutSeconds} 秒 · 每项能力最多尝试 {modelHealth.policy.attemptsPerProvider} 次</small></div>
      {!modelHealth.configured && <div className="runtime-empty"><Activity size={17} /><span><strong>当前未配置外部模型</strong><small>系统继续使用全文检索与确定性回答，不会产生模型调用费用。</small></span></div>}
      {runtimeProviders.map((provider, providerIndex) => <div className="runtime-provider" key={provider.provider}><span className="runtime-provider-name"><Activity size={15} /><strong>{provider.provider === "unconfigured" ? "尚未配置 AI 服务" : providerIndex === 0 ? "主要 AI 服务" : `备用 AI 服务 ${providerIndex}`}</strong></span><div className="runtime-purposes">{visiblePurposeNames.map((purposeName) => { const copy = purposeCopy[purposeName]; const purpose = provider.purposes[purposeName]; const state = !purpose?.configured ? "暂不可用 · 尚未配置" : purpose.status === "closed" ? "可用" : purpose.status === "half_open" ? "正在检测恢复" : `暂不可用，${Math.ceil(purpose.retryAfterMs / 1000)} 秒后重试`; const statusName = purpose?.configured ? purpose.status : "unconfigured"; return <span key={purposeName} data-status={statusName} title={copy.description}><i />{copy.label}<small>{state} · {copy.description}</small></span>; })}</div></div>)}
    </section>
    <section className="admin-section jobs"><div className="section-bar"><div><span className="eyebrow">异步处理队列</span><h2>后台任务</h2><small>上传、导入、删除等耗时操作会在离开页面后继续执行。</small></div><button className="secondary-button" onClick={() => void refresh()}><RefreshCw size={15} />刷新</button></div>
      {jobs.length === 0 && <div className="table-message">当前没有后台任务。</div>}
      {jobs.map((job) => <div className="job-row" key={job.id}><span className="job-icon"><Activity size={16} /></span><span><strong>{jobTypeLabels[job.type] ?? "其他后台任务"}</strong><small>{jobStatusCopy(job)}</small></span><div className="job-progress" role={job.status === "running" ? "progressbar" : undefined} aria-label={job.status === "running" ? `处理进度 ${job.progress}%` : undefined} aria-valuemin={job.status === "running" ? 0 : undefined} aria-valuemax={job.status === "running" ? 100 : undefined} aria-valuenow={job.status === "running" ? job.progress : undefined}><span style={{ width: `${job.status === "completed" ? 100 : job.progress}%` }} /></div><span className="mono">{jobProgressLabel(job)}</span>{job.status === "failed" && <button className="secondary-button" onClick={async () => { try { const updated = await retryAdminJob(job.id); setJobs((items) => items.map((item) => item.id === updated.id ? updated : item)); } catch (error) { setMessage(error instanceof Error ? error.message : "重试失败"); } }}>重试</button>}</div>)}
    </section>
  </div>;
}
