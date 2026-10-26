import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Link } from "react-router";
import { send, useApi, useAction, type Schemas } from "@/api/client";
import { obj, text, rows } from "@/lib/data";
import { Button } from "@/components/ui/button";
import { ConfirmButton, Status } from "@/components/workbench";
import { editorialLabels } from "./editorial-result";

type Selection = Schemas["ReanalysisSelection"];
type Preview = Schemas["EditorialPreview"];
const protections: Record<string, string> = {
  human_decision: "已有人工决定",
  active_claim: "已被人工领取",
  human_workflow: "存在人工草稿或历史",
  manual_bounds: "人工调整过边界",
  topic: "已关联主题",
  active_task: "关联任务正在执行",
  active_media_job: "媒体作业正在执行",
  downstream_task: "任务已取消或进入下游阶段",
  downstream_asset: "已有媒体、主题或人工反馈",
  retained_candidate: "已生成媒体或归档",
  missing_candidate: "候选缺失",
  missing_review_event: "缺少复核关联",
  identity_changed: "对象关系已变化",
  missing_source: "缺少来源",
  state_changed: "预览状态已变化",
  evidence_changed: "证据已变化",
};

export function EditorialReanalysis({ sessionId }: { sessionId: number }) {
  const [verdict, setVerdict] = useState("");
  const [failed, setFailed] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [prepared, setPrepared] = useState<{
    selection: Selection;
    preview: Preview;
  }>();
  const [accepted, setAccepted] = useState<Schemas["EditorialJobAccepted"]>();
  const path = `/api/sessions/${sessionId}/editorial-reanalysis`;
  const action = useAction();
  const job = useApi(accepted?.status_url || "/api/jobs", 3000, !!accepted);
  const selection = (offset = 0, keys: string[] = []): Selection => ({
    session_id: sessionId,
    keys,
    verdicts: verdict
      ? [verdict as "recommend" | "reject" | "needs_review" | "unknown"]
      : [],
    failed_only: failed,
    offset,
    limit: 50,
  });
  const preview = useMutation({
    mutationFn: (value: Selection) =>
      send<Preview>(`${path}/preview`, "POST", value),
    onSuccess: (value, requested) => {
      setPrepared({ selection: requested, preview: value });
      setSelected(
        value.targets
          .filter((item) => !item.protection)
          .map((item) => item.key),
      );
    },
  });
  const eligible =
    prepared?.preview.targets
      .filter((item) => !item.protection)
      .map((item) => item.key) || [];
  const selectionChanged =
    eligible.length !== selected.length ||
    eligible.some((key) => !selected.includes(key));
  const active = ["queued", "running", "cancelling"].includes(
    text(obj(job.data).status, accepted?.status || ""),
  );
  const canRun =
    !!prepared &&
    selected.length > 0 &&
    !selectionChanged &&
    !preview.isPending &&
    !action.isPending &&
    !active;
  function clearPreview() {
    setPrepared(undefined);
    setSelected([]);
    preview.reset();
  }
  return (
    <section
      className="space-y-4 rounded-2xl border bg-card p-5"
      aria-label="重新分析编辑判断"
    >
      <h2 className="font-semibold">重新分析编辑判断</h2>
      <p className="text-xs leading-6 text-muted-foreground">
        先预览对象及保护原因，再确认调用模型。每页最多 50
        项，共享本次调用预算；保留原有 ID
        和人工成果。预算用尽的对象会记录为跳过。
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <select
          className="native-select w-44"
          aria-label="编辑结论筛选"
          disabled={preview.isPending || action.isPending || active}
          value={verdict}
          onChange={(event) => {
            setVerdict(event.target.value);
            clearPreview();
          }}
        >
          <option value="">全部结论</option>
          {["recommend", "reject", "needs_review", "unknown"].map((value) => (
            <option key={value} value={value}>
              {editorialLabels[value]}
            </option>
          ))}
        </select>
        <label className="flex gap-2 text-sm">
          <input
            type="checkbox"
            disabled={preview.isPending || action.isPending || active}
            checked={failed}
            onChange={(event) => {
              setFailed(event.target.checked);
              clearPreview();
            }}
          />
          仅调用失败
        </label>
        <Button
          variant="outline"
          disabled={preview.isPending || active}
          onClick={() => preview.mutate(selection())}
        >
          预览分析对象
        </Button>
      </div>
      {preview.isError && (
        <p role="alert" className="text-sm text-destructive">
          {preview.error.message}
        </p>
      )}
      {prepared && (
        <>
          <p className="text-xs">
            筛选匹配 {prepared.preview.total} 项；当前页{" "}
            {prepared.preview.targets.length} 项，可更新 {eligible.length} 项。
          </p>
          <div className="max-h-80 space-y-2 overflow-auto">
            {prepared.preview.targets.map((item) => (
              <label
                key={item.key}
                className="flex items-start gap-3 rounded-lg border p-3 text-xs"
              >
                <input
                  type="checkbox"
                  aria-label={`选择 ${item.key}`}
                  disabled={!!item.protection || active || preview.isPending}
                  checked={selected.includes(item.key)}
                  onChange={(event) =>
                    setSelected((previous) =>
                      event.target.checked
                        ? [...previous, item.key]
                        : previous.filter((key) => key !== item.key),
                    )
                  }
                />
                <span>
                  {item.key} · {editorialLabels[item.verdict || "unknown"]}
                  {item.failed ? " · 调用失败" : ""}
                  <br />
                  {item.protection
                    ? `已保护：${protections[item.protection] || item.protection}`
                    : "允许重新判断"}
                </span>
              </label>
            ))}
          </div>
          {selectionChanged && (
            <Button
              variant="outline"
              disabled={!selected.length || preview.isPending || active}
              onClick={() => preview.mutate(selection(0, selected))}
            >
              更新所选对象预览
            </Button>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              variant="ghost"
              disabled={
                !prepared.selection.offset || preview.isPending || active
              }
              onClick={() =>
                preview.mutate(
                  selection(Math.max(0, (prepared.selection.offset || 0) - 50)),
                )
              }
            >
              上一页
            </Button>
            <Button
              variant="ghost"
              disabled={
                prepared.preview.next_offset == null ||
                preview.isPending ||
                active
              }
              onClick={() =>
                preview.mutate(selection(prepared.preview.next_offset || 0))
              }
            >
              下一页
            </Button>
            <ConfirmButton
              title={`重新分析预览中的 ${selected.length} 个可更新对象？`}
              description="将调用当前配置的模型；保护项自动跳过，输入或人工状态改变时拒绝提交旧结果。"
              disabled={!canRun}
              onConfirm={async () => {
                if (!prepared || !canRun) return;
                const value = await action.mutateAsync({
                  path,
                  body: {
                    selection: prepared.selection,
                    fingerprint: prepared.preview.fingerprint,
                  },
                  success: "重新分析作业已排队",
                });
                setAccepted(value as Schemas["EditorialJobAccepted"]);
                setPrepared(undefined);
                setSelected([]);
              }}
            >
              确认重新分析
            </ConfirmButton>
          </div>
        </>
      )}
      {accepted && (
        <div
          className="space-y-2 rounded-lg bg-muted/50 p-3 text-sm"
          aria-live="polite"
        >
          <p>
            作业 {accepted.job_id} ·{" "}
            <Status value={obj(job.data).status || accepted.status} />
          </p>
          {job.isError ? (
            <p role="alert">状态读取失败：{job.error.message}</p>
          ) : (
            <p>{text(obj(job.data).message, "正在读取作业状态")}</p>
          )}
          {obj(job.data).error ? <p>{text(obj(job.data).error)}</p> : null}
          {obj(job.data).result ? (
            <p>
              已更新 {text(obj(obj(job.data).result).updated)} 项，跳过{" "}
              {text(obj(obj(job.data).result).skipped)} 项
            </p>
          ) : null}
          {rows(obj(obj(job.data).result).targets).map((item) => (
            <p className="text-xs" key={text(item.key)}>
              {text(item.key)}：
              {item.status === "updated" ? "已更新" : "已跳过"}
              {item.reason
                ? ` · ${protections[text(item.reason)] || editorialLabels[text(item.reason)] || text(item.reason)}`
                : ""}
            </p>
          ))}
          {active && (
            <Button
              variant="outline"
              disabled={action.isPending}
              onClick={() =>
                void action
                  .mutateAsync({ path: `${accepted.status_url}/cancel` })
                  .catch(() => {})
              }
            >
              取消本次重新分析
            </Button>
          )}
          <p className="text-xs">
            作业已持久保存，刷新页面后也可在任务队列的后台作业中查看。
          </p>
          <Button asChild variant="link">
            <Link to="/tasks">打开任务队列</Link>
          </Button>
        </div>
      )}
    </section>
  );
}
