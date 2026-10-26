import { statusLabels } from "@/lib/status";
import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  CheckCheck,
  ChevronLeft,
  ChevronRight,
  Play,
  Save,
  ShieldCheck,
  Undo2,
} from "lucide-react";
import { toast } from "sonner";
import { useAction, useApi, type Row } from "@/api/client";
import { useAuth } from "@/api/auth";
import { useDraft } from "@/lib/drafts";
import { obj, rows, text, number, duration, time } from "@/lib/data";
import {
  Page,
  QueryState,
  Empty,
  Status,
  Refresh,
  ConfirmButton,
  FormField,
  DetailList,
} from "@/components/workbench";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { EditorialResult } from "@/components/editorial-result";

export const decisions = [
  ["approved_solo", "独立成片"],
  ["approved_collection", "合集候选"],
  ["in_collection", "加入合集"],
  ["maybe_topic", "可能属于主题"],
  ["hold", "保留待定"],
  ["not_exciting", "不够精彩"],
  ["insufficient_context", "上下文不足"],
  ["start_too_late", "开头截晚"],
  ["end_too_early", "结尾截早"],
  ["duplicate_content", "内容重复"],
  ["subtitle_error", "字幕错误"],
  ["visual_issue", "画面异常"],
  ["sensitive", "涉及敏感内容"],
  ["rejected", "拒绝"],
];
export function ReviewQueuePage() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") || "pending";
  const offset = number(params.get("offset"));
  const mine = params.get("mine") === "1";
  const [selected, setSelected] = useState<number[]>([]);
  const [audit, setAudit] = useState(false);
  const query = useApi(
    `/review/api/queue?status=${status}&mine=${mine}&limit=30&offset=${offset}`,
    10000,
  );
  const auth = useAuth();
  const action = useAction();
  const data = obj(query.data);
  const items = rows(data.items);
  const auditQuery = useApi(
    "/review/api/audit?limit=100",
    false,
    audit && auth.role === "admin",
  );
  function filter(next: Record<string, string>) {
    setSelected([]);
    setParams({ ...Object.fromEntries(params), ...next });
  }
  async function batch(kind: string) {
    const result = await action.mutateAsync({
      path: "/api/candidates/batch",
      body: { action: kind, candidate_ids: selected },
    });
    const failures = rows(obj(result).failed);
    setSelected(failures.map((entry) => number(entry.candidate_id)));
    if (failures.length)
      toast.error(
        failures
          .map((entry) => `#${entry.candidate_id} ${entry.error}`)
          .join("；"),
      );
  }
  return (
    <Page
      eyebrow="PRODUCE / REVIEW"
      title="人工审片"
      description="把算法找到的片刻，交给人的判断。领取后再修改，避免多人同时覆盖。"
      actions={
        <>
          <Refresh refresh={query.refetch} />
          {auth.role === "admin" && (
            <Button variant="outline" onClick={() => setAudit(!audit)}>
              审核审计
            </Button>
          )}
        </>
      }
    >
      <div className="flex flex-wrap gap-2">
        {[
          ["pending", "待审核"],
          ["claimed", "已领取"],
          ["reviewed", "已审核"],
          ["all", "全部"],
        ].map(([value, label]) => (
          <Button
            key={value}
            variant={status === value ? "default" : "outline"}
            size="sm"
            onClick={() => filter({ status: value, offset: "0" })}
          >
            {label}
            {obj(data.counts)[value] !== undefined && (
              <span className="ml-1 opacity-70">
                {number(obj(data.counts)[value])}
              </span>
            )}
          </Button>
        ))}
        <label className="ml-auto flex items-center gap-2 text-sm">
          <Checkbox
            checked={mine}
            onCheckedChange={(next) =>
              filter({ mine: next ? "1" : "0", offset: "0" })
            }
          />
          只看我的领取
        </label>
      </div>
      {selected.length > 0 && auth.role === "admin" && (
        <div className="selection-bar">
          <span className="mr-auto text-sm">
            已选择 {selected.length} 个候选
          </span>
          {[
            ["approve", "通过并渲染"],
            ["reject", "拒绝"],
            ["publish", "标记就绪"],
            ["delete", "删除"],
          ].map(([value, label]) => (
            <ConfirmButton
              key={value}
              title={`${label}选中的候选？`}
              description="逐项执行并显示失败项。涉及上传的结果请到上传页面核对。"
              destructive={value === "delete"}
              disabled={action.isPending}
              onConfirm={() => batch(value)}
            >
              {label}
            </ConfirmButton>
          ))}
        </div>
      )}
      <QueryState query={query}>
        {items.length ? (
          <div className="space-y-3">
            {items.map((item) => (
              <Card key={text(item.id)} className="gap-0 py-0 shadow-none">
                <CardContent className="flex items-start gap-4 p-5">
                  {auth.role === "admin" && (
                    <Checkbox
                      aria-label={`选择候选 ${item.id}`}
                      checked={selected.includes(number(item.id))}
                      onCheckedChange={(next) =>
                        setSelected(
                          next
                            ? [...selected, number(item.id)]
                            : selected.filter((id) => id !== item.id),
                        )
                      }
                    />
                  )}
                  <span className="grid size-11 shrink-0 place-items-center rounded-xl bg-primary/8 text-primary">
                    <Play className="size-5" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <Link
                        className="font-semibold hover:text-primary"
                        to={`/review/${item.id}`}
                      >
                        候选 #{text(item.id)}
                      </Link>
                      <Status value={item.review_status || item.status} />
                      {item.blinded ? (
                        <Badge variant="outline">盲审</Badge>
                      ) : null}
                    </div>
                    <p className="mt-2 line-clamp-2 text-sm leading-6 text-muted-foreground">
                      {text(
                        item.reason,
                        item.blinded
                          ? "评分和推荐理由已隐藏，请独立判断。"
                          : "等待人工判断",
                      )}
                    </p>
                    <p className="mt-3 text-xs text-muted-foreground">
                      场次 #{text(item.session_id)} · {time(item.start_ts)} ·{" "}
                      {duration(
                        (Date.parse(text(item.end_ts)) -
                          Date.parse(text(item.start_ts))) /
                          1000,
                      )}
                      {obj(item.claim).active
                        ? ` · ${text(obj(item.claim).claimed_by)} 正在审核`
                        : ""}
                      {item.score !== null && item.score !== undefined
                        ? ` · 评分 ${number(item.score).toFixed(3)}`
                        : ""}
                    </p>
                  </div>
                  <Button asChild size="sm" variant="outline">
                    <Link to={`/review/${item.id}`}>
                      开始审片
                      <ChevronRight className="size-3" />
                    </Link>
                  </Button>
                </CardContent>
              </Card>
            ))}
          </div>
        ) : (
          <Empty
            title="当前没有待审候选"
            description="录制或导入分析完成后，候选会出现在这里。也可以切换状态查看已经处理的项目。"
          />
        )}
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>
            共 {number(data.total)} 项 · 第 {Math.floor(offset / 30) + 1} 页
          </span>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={offset === 0}
              onClick={() =>
                filter({ offset: String(Math.max(0, offset - 30)) })
              }
            >
              <ChevronLeft className="size-3" />
              上一页
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={!data.has_more}
              onClick={() => filter({ offset: String(offset + 30) })}
            >
              下一页
              <ChevronRight className="size-3" />
            </Button>
          </div>
        </div>
      </QueryState>
      {audit && (
        <Card>
          <CardContent>
            <h2 className="mb-4 font-semibold">审核操作记录</h2>
            <QueryState query={auditQuery}>
              <Evidence value={auditQuery.data} />
            </QueryState>
          </CardContent>
        </Card>
      )}
    </Page>
  );
}

const evidenceLabels: Record<string, string> = {
  state: "状态",
  status: "状态",
  text: "内容",
  language: "语言",
  actor: "操作者",
  action: "操作",
  reason: "原因",
  summary: "摘要",
  title: "标题",
  error: "错误",
  detail: "详情",
  count: "数量",
  total: "合计",
  completed: "已完成",
  failed: "失败",
  in_progress: "进行中",
  queued: "排队",
  id: "编号",
  session_id: "场次",
  candidate_id: "候选",
  created_at: "创建时间",
  updated_at: "更新时间",
  model: "模型",
  models: "模型",
  backend: "引擎",
  duration_s: "时长（秒）",
  calls: "调用次数",
  elapsed_s: "耗时（秒）",
  label: "项目",
  value: "数值",
  weight: "权重",
  contribution: "贡献",
  claimed_by: "领取人",
  score: "评分",
  threshold: "阈值",
  sample_count: "样本数",
  recommended_threshold: "推荐阈值",
  peaks: "峰值",
  enabled: "已启用",
  running: "正在运行",
  t: "时间",
  keywords: "关键词",
  chapters: "章节",
  points: "高光片段",
  segments: "媒体分段",
  progress: "进度",
  message: "说明",
  details: "详情",
  available: "可用",
  memory_mb: "内存（MB）",
  pid: "进程",
  source: "来源",
  by_stage: "阶段统计",
  oldest_wait_s: "最久等待（秒）",
  last_error: "最近错误",
};

export function Evidence({ value }: { value: unknown }) {
  if (value === null || value === undefined)
    return <span className="text-xs text-muted-foreground">暂无数据</span>;
  if (Array.isArray(value))
    return value.length ? (
      <div className="space-y-2">
        {value.map((entry, index) => (
          <div key={index} className="rounded-lg bg-muted/45 px-3 py-2">
            <Evidence value={entry} />
          </div>
        ))}
      </div>
    ) : (
      <span className="text-xs text-muted-foreground">暂无数据</span>
    );
  if (
    typeof value === "object" &&
    "text" in value &&
    typeof value.text === "string"
  )
    return (
      <div className="space-y-3">
        <p className="whitespace-pre-wrap text-sm leading-8">
          {value.text || "当前没有语音文本"}
        </p>
        {"language" in value && (
          <span className="text-xs text-muted-foreground">
            语言：{String(value.language || "未标注")}
          </span>
        )}
      </div>
    );
  if (typeof value === "object")
    return (
      <dl className="space-y-2">
        {Object.entries(value).map(([key, item]) => (
          <div key={key} className="flex flex-wrap gap-2 text-xs leading-5">
            <dt className="min-w-24 text-muted-foreground">
              {evidenceLabels[key] ||
                statusLabels[key] ||
                key.replaceAll("_", " ")}
            </dt>
            <dd className="min-w-0 flex-1 break-words">
              {typeof item === "object" ? (
                <Evidence value={item} />
              ) : (
                String(item ?? "—")
              )}
            </dd>
          </div>
        ))}
      </dl>
    );
  return (
    <p className="whitespace-pre-wrap text-sm leading-7">{String(value)}</p>
  );
}
export function ReviewPage() {
  const params = useParams();
  return <ReviewEditor key={params.id} />;
}
function ReviewEditor() {
  const { id = "" } = useParams();
  const path = `/review/api/${id}`;
  const query = useApi<Row>(path, 10000);
  const waveform = useApi<Row>(`${path}/waveform`, false);
  const auth = useAuth();
  const action = useAction();
  const cache = useQueryClient();
  const data = obj(query.data);
  const candidate = obj(data.candidate);
  const workflow = obj(data.workflow);
  const claim = obj(workflow.claim);
  const serverDraft = obj(workflow.draft);
  const draft = useDraft<Row>(`review-${id}`, {
    decision: serverDraft.decision || "hold",
    reason: serverDraft.reason || "",
  });
  const video = useRef<HTMLVideoElement>(null);
  const [side, setSide] = useState("both");
  const [adjust, setAdjust] = useState(3);
  const owned = claim.active && claim.claimed_by === obj(data.viewer).actor;
  const disabled = action.isPending || !owned || !auth.authenticated;
  async function write(suffix: string, body?: unknown, method = "POST") {
    await action.mutateAsync({ path: `${path}/${suffix}`, method, body });
    await query.refetch();
  }
  useEffect(() => {
    function keyboard(event: KeyboardEvent) {
      if (
        !auth.authenticated ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        (event.target instanceof HTMLElement &&
          (event.target.matches(
            "input,textarea,select,button,a,[contenteditable]",
          ) ||
            event.target.closest('[role="dialog"]')))
      )
        return;
      const media = video.current;
      if (!media) return;
      switch (event.key.toLowerCase()) {
        case " ":
        case "k":
          event.preventDefault();
          if (media.paused) void media.play().catch(() => {});
          else media.pause();
          break;
        case "j":
        case "arrowleft":
          event.preventDefault();
          media.currentTime = Math.max(0, media.currentTime - 5);
          break;
        case "l":
        case "arrowright":
          event.preventDefault();
          media.currentTime = Math.min(
            media.duration || 0,
            media.currentTime + 5,
          );
          break;
        case "i":
          setSide("start");
          break;
        case "o":
          setSide("end");
          break;
      }
    }
    document.addEventListener("keydown", keyboard);
    return () => document.removeEventListener("keydown", keyboard);
  }, [auth.authenticated]);
  const peaks = Array.isArray(waveform.data?.peaks) ? waveform.data.peaks : [];
  return (
    <Page
      eyebrow="PRODUCE / REVIEW STUDIO"
      title={`审片工作台 · #${id}`}
      description="先观看，再决策。空格 / K 播放暂停，J / L 前后跳转，I / O 选择入点和出点。"
      actions={
        <>
          <Button asChild variant="outline" size="sm">
            <Link to="/review">
              <ArrowLeft className="size-3" />
              返回队列
            </Link>
          </Button>
          <Refresh refresh={query.refetch} />
        </>
      }
    >
      <QueryState query={query}>
        <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
          <div className="space-y-5">
            {!obj(data.viewer).blinded && (
              <EditorialResult value={data.editorial} />
            )}
            <div className="overflow-hidden rounded-2xl border bg-slate-950">
              <video
                ref={video}
                src={text(data.media_url, `${path}/preview`)}
                controls
                preload="metadata"
                className="aspect-video w-full"
              />
              <div className="flex items-center justify-between px-5 py-3 text-xs text-slate-400">
                <span>
                  场次 #{text(candidate.session_id)} · 候选 #{id}
                </span>
                <span>
                  {duration(
                    (Date.parse(
                      text(
                        obj(data.boundary).adjusted_end_ts || candidate.end_ts,
                      ),
                    ) -
                      Date.parse(
                        text(
                          obj(data.boundary).adjusted_start_ts ||
                            candidate.start_ts,
                        ),
                      )) /
                      1000,
                  )}
                </span>
              </div>
            </div>
            <Card className="gap-0 py-0">
              <CardContent className="p-5">
                <h2 className="mb-4 text-sm font-semibold">音频波形</h2>
                <QueryState query={waveform}>
                  {peaks.length ? (
                    <svg
                      viewBox={`0 0 ${peaks.length} 64`}
                      className="h-20 w-full cursor-crosshair text-primary"
                      role="img"
                      aria-label="音频波形，点击跳转播放"
                      onClick={(event) => {
                        if (video.current) {
                          const rect =
                            event.currentTarget.getBoundingClientRect();
                          video.current.currentTime =
                            ((event.clientX - rect.left) / rect.width) *
                            number(waveform.data?.duration_s);
                        }
                      }}
                    >
                      {peaks.map((peak, index) => (
                        <line
                          key={index}
                          x1={index}
                          x2={index}
                          y1={32 - Math.abs(number(peak)) * 30}
                          y2={32 + Math.abs(number(peak)) * 30}
                          stroke="currentColor"
                          strokeWidth="0.65"
                        />
                      ))}
                    </svg>
                  ) : (
                    <p className="text-xs text-muted-foreground">
                      {text(waveform.data?.error, "当前媒体没有可用的波形数据")}
                    </p>
                  )}
                </QueryState>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="space-y-4">
                <h2 className="text-sm font-semibold">剪辑边界</h2>
                <DetailList
                  values={[
                    {
                      label: "入点",
                      value: time(
                        obj(data.boundary).adjusted_start_ts ||
                          candidate.start_ts,
                      ),
                    },
                    {
                      label: "出点",
                      value: time(
                        obj(data.boundary).adjusted_end_ts || candidate.end_ts,
                      ),
                    },
                  ]}
                />
                <div className="flex flex-wrap gap-2">
                  <select
                    className="native-select w-28"
                    aria-label="调整边界"
                    value={side}
                    onChange={(event) => setSide(event.target.value)}
                  >
                    <option value="start">入点 I</option>
                    <option value="end">出点 O</option>
                    <option value="both">整体平移</option>
                  </select>
                  <input
                    className="native-select w-24"
                    aria-label="调整秒数"
                    type="number"
                    min={-900}
                    max={900}
                    step="0.1"
                    value={adjust}
                    onChange={(event) => setAdjust(Number(event.target.value))}
                  />
                  <Button
                    variant="outline"
                    disabled={disabled}
                    onClick={() =>
                      void write("adjust", { side, adjust_s: adjust }).catch(
                        () => {},
                      )
                    }
                  >
                    移动 {adjust} 秒
                  </Button>
                  {[3, 5, 10, 30].map((seconds) => (
                    <Button
                      key={seconds}
                      variant="ghost"
                      size="sm"
                      disabled={disabled}
                      onClick={() =>
                        void write("adjust", {
                          side,
                          adjust_s: side === "start" ? -seconds : seconds,
                        }).catch(() => {})
                      }
                    >
                      扩展 {seconds}s
                    </Button>
                  ))}
                </div>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={disabled || !workflow.can_undo}
                    onClick={() => void write("undo").catch(() => {})}
                  >
                    <Undo2 className="size-3" />
                    撤销最近操作
                  </Button>
                  <ConfirmButton
                    title="重新渲染当前边界？"
                    description="提交后台渲染任务，进度可在任务队列查看。"
                    disabled={disabled}
                    onConfirm={() => write("rerender")}
                  >
                    重新渲染
                  </ConfirmButton>
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="space-y-4">
                <h2 className="font-semibold">转写上下文</h2>
                <Evidence value={data.transcript} />
              </CardContent>
            </Card>
            <details className="rounded-2xl border bg-card p-5">
              <summary className="cursor-pointer text-sm font-semibold">
                弹幕证据与热度
              </summary>
              <div className="mt-4 space-y-4">
                <Evidence value={data.danmaku_buckets} />
                <Evidence value={data.danmaku_window} />
              </div>
            </details>
            {!obj(data.viewer).blinded && (
              <details className="rounded-2xl border bg-card p-5">
                <summary className="cursor-pointer text-sm font-semibold">
                  评分解释与来源信号
                </summary>
                <div className="mt-4 space-y-4">
                  <Evidence value={data.score_breakdown} />
                  <Evidence value={data.danmaku_explain} />
                  <Evidence value={data.features} />
                </div>
              </details>
            )}
          </div>
          <aside className="space-y-5 xl:sticky xl:top-24">
            <Card>
              <CardContent className="space-y-5">
                <div className="flex items-center justify-between">
                  <h2 className="font-semibold">审核决定</h2>
                  <ShieldCheck className="size-4 text-primary" />
                </div>
                <Status value={candidate.review_status || candidate.status} />
                {obj(data.viewer).blinded ? (
                  <Badge variant="outline">盲审 · 评分已隐藏</Badge>
                ) : null}
                <div className="rounded-xl bg-muted/60 p-3 text-xs leading-6">
                  {claim.active
                    ? `${claim.claimed_by} 已领取 · 到期 ${time(claim.claim_expires_at)}`
                    : "当前候选尚未领取"}
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant={owned ? "outline" : "default"}
                    size="sm"
                    disabled={action.isPending}
                    onClick={() =>
                      void write("claim", { force: false }).catch(() => {})
                    }
                  >
                    {owned ? "续期领取" : "领取审核"}
                  </Button>
                  {Boolean(owned) && (
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={action.isPending}
                      onClick={() => void write("release").catch(() => {})}
                    >
                      释放
                    </Button>
                  )}
                  {Boolean(claim.active && !owned && auth.role === "admin") && (
                    <ConfirmButton
                      title="强制接管此候选？"
                      description="当前审核员会失去修改权限。"
                      onConfirm={() => write("claim", { force: true })}
                    >
                      强制接管
                    </ConfirmButton>
                  )}
                </div>
                <FormField
                  field={{
                    name: "decision",
                    label: "审核结论",
                    options: decisions.map(([value, label]) => ({
                      value,
                      label,
                    })),
                  }}
                  value={draft.value.decision}
                  disabled={disabled}
                  onChange={(next) =>
                    draft.update({ ...draft.value, decision: next })
                  }
                />
                <FormField
                  field={{
                    name: "reason",
                    label: "审核备注",
                    type: "textarea",
                    description: "可先保存草稿，领取到期后需重新领取。",
                  }}
                  value={draft.value.reason}
                  disabled={disabled}
                  onChange={(next) =>
                    draft.update({ ...draft.value, reason: next })
                  }
                />
                <Button
                  className="w-full"
                  variant="outline"
                  disabled={disabled}
                  onClick={() =>
                    void write("draft", draft.value, "PUT")
                      .then(() => draft.reset())
                      .catch(() => {})
                  }
                >
                  <Save className="size-3" />
                  保存审核草稿
                </Button>
                <ConfirmButton
                  title="提交当前审核决定？"
                  description="通过独立成片会提交渲染；其他决定会按对应审核规则保存。"
                  disabled={disabled}
                  onConfirm={async () => {
                    await write("review", draft.value);
                    draft.reset();
                    await cache.invalidateQueries({ queryKey: ["api"] });
                  }}
                >
                  <CheckCheck className="size-3" />
                  提交审核
                </ConfirmButton>
                {draft.dirty && (
                  <p className="text-xs text-amber-600">有未提交的审核草稿</p>
                )}
              </CardContent>
            </Card>
            <Card>
              <CardContent className="space-y-3">
                <h2 className="text-sm font-semibold">相邻候选</h2>
                {[
                  ...rows(data.prev_candidates),
                  ...rows(data.next_candidates),
                ].map((entry) => (
                  <Button
                    key={text(entry.id)}
                    asChild
                    variant="ghost"
                    className="w-full justify-start"
                  >
                    <Link to={`/review/${entry.id}`}>
                      候选 #{text(entry.id)}
                    </Link>
                  </Button>
                ))}
                <Evidence value={data.existing_clips} />
              </CardContent>
            </Card>
          </aside>
        </div>
      </QueryState>
    </Page>
  );
}
