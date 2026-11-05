import { obj, text } from "@/lib/data";

const executionLabels: Record<string, string> = {
  recording: "录制结束后生成整场总结",
  waiting: "等待转写与分析完成",
  pending: "等待生成或继续处理",
  generating: "正在生成或核验总结",
  unconfigured: "请在模型设置中启用整场总结路由，待处理请求会继续。",
  failed: "本次总结执行未完成",
  ready: "整场总结已完成",
};

const verificationLabels: Record<string, string> = {
  not_run: "尚未开始核验",
  pending: "等待核验",
  verifying: "核验中",
  verified: "核验通过",
  needs_review: "存在待复核内容",
  failed: "核验执行失败",
};

export function SessionSummary({ value }: { value: unknown }) {
  const summary = obj(value);
  const verification = obj(summary.verification);
  const ready = summary.status === "ready";
  const verified = ready && verification.status === "verified";
  const body = ready ? summary.summary : summary.draft_summary;
  const bodyLabel = verified ? "已核验总结" : ready ? "总结正文" : "总结草稿";
  const error = text(verification.error || summary.error, "");
  const total = Number(verification.total_nodes || 0);
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground" role="status">
        {executionLabels[text(summary.status)] || "尚无整场总结"}
      </p>
      <div className="text-sm">
        <span>事实核验：</span>
        <span>
          {verificationLabels[text(verification.status)] ||
            (ready ? "核验状态未知" : "尚未开始核验")}
        </span>
        {total > 0 && (
          <span className="ml-2 text-muted-foreground">
            {Number(verification.completed_nodes || 0)} / {total} 个节点
          </span>
        )}
      </div>
      {typeof body === "string" && body.trim() && (
        <article aria-label={bodyLabel}>
          <p className="mb-2 text-sm font-medium">
            {verified
              ? "已核验总结"
              : ready
                ? "总结正文（核验状态未知）"
                : "总结草稿（尚未通过核验）"}
          </p>
          <p className="whitespace-pre-wrap text-sm leading-7">{body}</p>
        </article>
      )}
      {error && (
        <p
          className="whitespace-pre-wrap text-sm text-destructive"
          role="alert"
        >
          {error}
        </p>
      )}
    </div>
  );
}
