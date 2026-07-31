import { Check, LoaderCircle, X } from "lucide-react";
import type { AgentActivity } from "@/lib/types";

export function AgentRunProgress({ activities }: { activities: AgentActivity[] }) {
  if (activities.length === 0) return null;
  const running = activities.find((item) => item.status === "running");
  const failed = activities.some((item) => item.status === "failed");

  return <section className="agent-progress" aria-label="Agent 运行轨迹" aria-live="polite">
    <div className="agent-progress-heading">
      <span>运行轨迹</span>
      <small>{failed ? "部分步骤未完成" : running ? running.label : `已完成 ${activities.length} 个步骤`}</small>
    </div>
    <ol>
      {activities.map((item) => <li key={item.key} data-status={item.status}>
        <span className="agent-step-mark" aria-hidden="true">
          {item.status === "completed" ? <Check size={11} /> : item.status === "failed" ? <X size={11} /> : <LoaderCircle size={11} />}
        </span>
        <span><strong>{item.label}</strong><small>{item.status === "running" ? "进行中" : item.status === "failed" ? "未完成" : item.durationMs === undefined ? "已完成" : `${item.durationMs} ms`}</small></span>
      </li>)}
    </ol>
  </section>;
}
