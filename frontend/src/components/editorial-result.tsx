import type { Schemas } from "@/api/client";
import { obj, text, duration } from "@/lib/data";
import { Badge } from "@/components/ui/badge";

export const editorialLabels: Record<string, string> = {
  recommend: "建议剪辑",
  reject: "不建议剪辑",
  needs_review: "需要人工复核",
  unknown: "尚无判断",
  succeeded: "判断已完成",
  failed: "调用失败",
  disabled: "未启用模型",
  cancelled: "已取消",
  not_run: "尚未判断",
  invalid: "保存的判断无效，需重新分析",
  available: "解释已核验",
  unverified: "历史解释未经本轮核验",
  complete: "边界完整",
  needs_expansion: "需要补充上下文",
  uncertain: "边界待确认",
  before: "前文",
  after: "后文",
  both: "前后文",
  asr: "语音转写",
  context_asr: "上下文转写",
  danmaku: "观众弹幕",
  audio: "音频信号",
  sensevoice: "语音事件",
  trend: "趋势信号",
  visual: "画面证据",
  degraded: "证据质量不足",
  missing: "缺失",
  pending: "等待处理",
  error: "证据读取失败",
  word: "字词定位",
  segment: "分段定位",
  event: "事件范围",
  mixed: "跨越事件范围",
  invalid_output: "响应格式无效",
  empty_response: "响应为空",
  timeout: "请求超时",
  deadline: "总时间预算耗尽",
  attempt_budget: "请求次数预算耗尽",
  unauthorized: "模型授权不可用",
  context_unavailable: "上下文不可用",
  daily_budget: "每日预算耗尽",
  rate_limited: "服务请求限流",
  evidence_changed: "证据已变化",
  provider_error: "服务返回错误",
  unsupported_format: "服务不支持请求格式",
};

export function EditorialResult({
  value,
  heading = true,
}: {
  value: unknown;
  heading?: boolean;
}) {
  if (obj(value).schema_version !== "editorial-view-v1") return null;
  const view = value as Schemas["EditorialPresentation"];
  const execution = view.execution;
  const result = execution?.result;
  return (
    <section
      className="space-y-3 rounded-xl border p-4"
      aria-label="编辑判断与证据"
    >
      {heading && (
        <>
          <h2 className="font-semibold">{view.title}</h2>
          <p className="text-sm leading-7">{view.summary}</p>
        </>
      )}
      <div className="flex flex-wrap gap-2">
        <Badge variant="outline">
          {view.explanation_state === "failed"
            ? "解释未完成"
            : view.explanation_state === "not_run"
              ? "尚无内容解释"
              : editorialLabels[view.explanation_state]}
        </Badge>
        <Badge variant="secondary">
          {result
            ? editorialLabels[result.verdict]
            : editorialLabels[view.editorial_state || "not_run"]}
        </Badge>
        {view.hotspot_event_id != null && (
          <span className="text-xs">热点 #{view.hotspot_event_id}</span>
        )}
        {view.candidate_id != null && (
          <span className="text-xs">候选 #{view.candidate_id}</span>
        )}
        {view.event_id != null && (
          <span className="text-xs">复核事件 #{view.event_id}</span>
        )}
      </div>
      {view.explanation_error && (
        <p className="text-xs">
          解释状态：
          {editorialLabels[view.explanation_error] || view.explanation_error}
        </p>
      )}
      {execution?.error && (
        <p className="text-sm" role="status">
          判断未完成：{editorialLabels[execution.error] || execution.error}
        </p>
      )}
      <p className="text-xs text-muted-foreground">
        技术分数{" "}
        {view.technical_score == null
          ? "未知"
          : view.technical_score.toFixed(3)}{" "}
        · 兴趣等级{" "}
        {result?.interest_rating == null
          ? "未知"
          : `${result.interest_rating} / 4`}
        （序数等级）
      </p>
      {result && (
        <>
          {result.hook && <p className="text-sm">吸引点：{result.hook}</p>}
          <p className="text-sm leading-7">判断依据：{result.reason}</p>
          <p className="text-xs">
            独立理解：
            {result.standalone == null
              ? "未知"
              : result.standalone
                ? "可以"
                : "仍依赖上下文"}{" "}
            · {editorialLabels[result.boundary_status]}
          </p>
          {result.missing_context && (
            <p className="text-xs">
              缺少{editorialLabels[result.missing_context]}
            </p>
          )}
          {result.suggested_bounds && (
            <p className="text-xs">
              建议范围：{duration(result.suggested_bounds.start_s)}–
              {duration(result.suggested_bounds.end_s)}
              （证据时间轴；实际成片边界单独显示）
            </p>
          )}
          {!!result.uncertainties.length && (
            <ul className="list-disc pl-5 text-xs leading-6">
              {result.uncertainties.map((entry) => (
                <li key={entry}>{entry}</li>
              ))}
            </ul>
          )}
        </>
      )}
      {!!view.evidence?.length && (
        <details>
          <summary className="cursor-pointer text-xs">
            判断使用的证据与时间精度
          </summary>
          <div className="mt-3 space-y-3">
            {view.evidence.map((item) => (
              <div
                key={item.evidence_id}
                className="rounded-lg bg-muted/50 p-3 text-xs"
              >
                <p>
                  {editorialLabels[item.type]} ·{" "}
                  {item.state === "available"
                    ? "可用"
                    : item.state === "disabled"
                      ? "此类证据未启用"
                      : editorialLabels[item.state] || item.state}{" "}
                  ·{" "}
                  {item.timing_precision === "unknown"
                    ? "时间未知"
                    : editorialLabels[item.timing_precision]}
                </p>
                <p className="mt-1">
                  {item.start_s == null || item.end_s == null
                    ? "无精确起止时间"
                    : `${duration(item.start_s)}–${duration(item.end_s)}`}{" "}
                  ·{" "}
                  {item.context_role === "unknown"
                    ? "上下文位置未知"
                    : editorialLabels[item.context_role]}
                </p>
                {item.excerpts.map((excerpt, index) => (
                  <p className="mt-2 whitespace-pre-wrap leading-6" key={index}>
                    {excerpt}
                  </p>
                ))}
                <p className="mt-2 text-muted-foreground">
                  引用 {item.evidence_id}
                  {result?.evidence_ids.includes(item.evidence_id)
                    ? " · 本次判断已引用"
                    : ""}
                </p>
              </div>
            ))}
          </div>
        </details>
      )}
      {execution && (
        <details>
          <summary className="cursor-pointer text-xs">
            模型调用与用量（未知值不计为零）
          </summary>
          <p className="my-2 text-xs">
            本次判断 {execution.attempts.length} 次请求 ·{" "}
            {execution.elapsed_s.toFixed(2)} 秒
          </p>
          {execution.attempts.map((attempt) => (
            <div
              className="mb-2 rounded-lg bg-muted/50 p-3 text-xs leading-6"
              key={attempt.number}
            >
              <p>
                请求 #{attempt.number} · {attempt.provider_id} · {attempt.model}
              </p>
              <p>
                服务报告模型：{text(attempt.reported_model, "未报告")} ·{" "}
                {attempt.status === "success" ? "成功" : "失败"}
              </p>
              <p>
                输入 token：{text(attempt.usage?.input_tokens, "未知")} · 输出
                token：{text(attempt.usage?.output_tokens, "未知")}
              </p>
              <p>
                估算费用（USD）：
                {text(attempt.usage?.estimated_cost_usd, "未知")}
              </p>
            </div>
          ))}
        </details>
      )}
    </section>
  );
}
