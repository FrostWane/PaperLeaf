"use client";

import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";
import { Activity, MoreHorizontal, Plus, RefreshCw, Users } from "lucide-react";
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
    purposes: Object.fromEntries(["answer", "evidence_support", "summary", "embedding"].map((purpose) => [purpose, { configured: true, status: "closed", consecutiveFailures: 0, retryAfterMs: 0 }])),
  }],
  policy: { timeoutSeconds: 30, attemptsPerProvider: 2, failureThreshold: 3, cooldownSeconds: 60 },
};

const purposeLabels: Record<string, string> = { answer: "回答", evidence_support: "证据核验", summary: "总结", embedding: "向量", vision: "视觉 OCR" };

export function AdminView() {
  const real = process.env.NEXT_PUBLIC_DATA_MODE === "real";
  const [users, setUsers] = useState(fixtureUsers);
  const [jobs, setJobs] = useState(demoJobs);
  const [modelHealth, setModelHealth] = useState<ModelRuntimeHealth>(demoHealth);
  const [message, setMessage] = useState("");
  const [showCreate, setShowCreate] = useState(false);
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
    if (!real) {
      setUsers((items) => items.map((item) => item.id === user.id && item.role !== "管理员" ? { ...item, status: item.status === "正常" ? "已停用" : "正常" } : item));
      return;
    }
    try {
      const updated = await setAdminUserActive(user.id, user.status !== "正常");
      setUsers((items) => items.map((item) => item.id === updated.id ? updated : item));
    } catch (error) { setMessage(error instanceof Error ? error.message : "用户状态更新失败"); }
  }, [real]);

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
    helper.display({ id: "actions", header: "", cell: ({ row }) => <button className="icon-button" aria-label={`${row.original.status === "正常" ? "停用" : "启用"} ${row.original.name}`} onClick={() => void toggleUser(row.original)}><MoreHorizontal size={16} /></button> }),
  ], [real, toggleUser]);
  const table = useReactTable({ data: users, columns, getCoreRowModel: getCoreRowModel() });
  const activeUsers = users.filter((user) => user.status === "正常").length;
  const runningJobs = jobs.filter((job) => job.status === "running" || job.status === "queued").length;
  const modelCircuitOpen = modelHealth.providers.some((provider) => Object.values(provider.purposes).some((purpose) => purpose.configured && purpose.status === "open"));
  const modelState = !modelHealth.configured ? "降级模式" : modelCircuitOpen ? "需检查" : "正常";

  return <div className="admin-layout">
    <div className="metric-row"><article><span>活跃用户</span><strong>{activeUsers}</strong><small>共 {users.length} 个账号</small></article><article><span>处理中任务</span><strong>{runningJobs}</strong><small>仅展示任务元数据</small></article><article><span>模型路由</span><strong>{modelState}</strong><small>{modelHealth.providers.length || 0} 个服务节点</small></article><article><span>内容访问</span><strong>关闭</strong><small>管理员默认不可读取</small></article></div>
    {message && <p className="admin-message" role="status">{message}</p>}
    <section className="admin-section"><div className="section-bar"><div><span className="eyebrow">Users & access</span><h2>用户与权限</h2></div><button className="primary-button" onClick={() => setShowCreate((value) => !value)}><Plus size={15} />创建用户</button></div>
      {showCreate && <div className="admin-create"><label>邮箱<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} /></label><label>临时密码<input type="password" value={temporaryPassword} onChange={(event) => setTemporaryPassword(event.target.value)} /></label><button className="primary-button" onClick={() => void createUser()}>保存用户</button></div>}
      <div className="table-scroll"><table className="data-table admin-table"><thead>{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => <th key={header.id}>{flexRender(header.column.columnDef.header, header.getContext())}</th>)}</tr>)}</thead><tbody>{table.getRowModel().rows.map((row) => <tr key={row.id}>{row.getVisibleCells().map((cell) => <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody></table></div>
    </section>
    <section className="admin-section model-runtime"><div className="section-bar"><div><span className="eyebrow">Model runtime</span><h2>模型运行时</h2></div><small>{modelHealth.policy.timeoutSeconds}s 超时 · 每节点最多 {modelHealth.policy.attemptsPerProvider} 次</small></div>
      {!modelHealth.configured && <div className="runtime-empty"><Activity size={17} /><span><strong>当前未配置外部模型</strong><small>系统继续使用全文检索与确定性回答，不会产生模型调用费用。</small></span></div>}
      {modelHealth.providers.map((provider) => <div className="runtime-provider" key={provider.provider}><span className="runtime-provider-name"><Activity size={15} /><strong>{provider.provider === "primary" ? "主服务" : "备用服务"}</strong></span><div className="runtime-purposes">{Object.entries(provider.purposes).filter(([, purpose]) => purpose.configured).map(([purposeName, purpose]) => <span key={purposeName} data-status={purpose.status}><i />{purposeLabels[purposeName] ?? purposeName}<small>{purpose.status === "closed" ? "可用" : purpose.status === "half_open" ? "试探恢复" : `${Math.ceil(purpose.retryAfterMs / 1000)}s 后重试`}</small></span>)}</div></div>)}
    </section>
    <section className="admin-section jobs"><div className="section-bar"><div><span className="eyebrow">Background jobs</span><h2>后台任务</h2></div><button className="secondary-button" onClick={() => void refresh()}><RefreshCw size={15} />刷新</button></div>
      {jobs.length === 0 && <div className="table-message">当前没有后台任务。</div>}
      {jobs.map((job) => <div className="job-row" key={job.id}><span className="job-icon"><Users size={16} /></span><span><strong>{job.type}</strong><small>{job.status} · 尝试 {job.attempts}/{job.maxAttempts}</small></span><div className="job-progress"><span style={{ width: `${job.progress}%` }} /></div><span className="mono">{job.progress}%</span>{job.status === "failed" && <button className="secondary-button" onClick={async () => { try { const updated = await retryAdminJob(job.id); setJobs((items) => items.map((item) => item.id === updated.id ? updated : item)); } catch (error) { setMessage(error instanceof Error ? error.message : "重试失败"); } }}>重试</button>}</div>)}
    </section>
  </div>;
}
