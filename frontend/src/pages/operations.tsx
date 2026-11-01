import { taskStages, statusLabels } from "@/lib/status";
import { useState } from "react";
import { Link } from "react-router";
import { Activity, Cpu, HardDrive, ListTodo, Upload } from "lucide-react";
import { useAction, useApi, type Row } from "@/api/client";
import { obj, rows, text, number, time, duration } from "@/lib/data";
import {
  Page,
  QueryState,
  Empty,
  Refresh,
  Status,
  Stat,
  ConfirmButton,
  FormDialog,
  ExternalLink,
  DetailList,
} from "@/components/workbench";
import { Evidence } from "./review";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";

export function TasksPage() {
  const [kind, setKind] = useState("pipeline");
  const [stage, setStage] = useState("");
  const query = useApi(
    kind === "pipeline"
      ? `/api/tasks?limit=200${stage ? `&stage=${stage}` : ""}`
      : "/api/jobs?limit=100",
  );
  const action = useAction();
  const data = obj(query.data);
  const items = rows(kind === "pipeline" ? data.tasks : data.jobs);
  return (
    <Page
      eyebrow="OPERATIONS / QUEUE"
      title="任务队列"
      description="追踪后台工作、失败原因与重试状态。取消请求被接受后，运行中的任务可能仍需收尾。"
      actions={<Refresh refresh={query.refetch} />}
    >
      <div className="flex flex-wrap gap-2">
        <Button
          variant={kind === "pipeline" ? "default" : "outline"}
          onClick={() => setKind("pipeline")}
        >
          分析流水线
        </Button>
        <Button
          variant={kind === "jobs" ? "default" : "outline"}
          onClick={() => setKind("jobs")}
        >
          后台作业
        </Button>
        {kind === "pipeline" && (
          <select
            className="native-select ml-auto w-44"
            aria-label="任务阶段"
            value={stage}
            onChange={(event) => setStage(event.target.value)}
          >
            <option value="">全部阶段</option>
            {taskStages.map((value) => (
              <option key={value} value={value}>
                {statusLabels[value]}
              </option>
            ))}
          </select>
        )}
      </div>
      <QueryState query={query}>
        {kind === "pipeline" && (
          <details className="rounded-2xl border bg-card p-4">
            <summary className="cursor-pointer text-sm font-medium">
              工作器统计
            </summary>
            <div className="mt-4">
              <Evidence value={data.stats} />
            </div>
          </details>
        )}
        {items.length ? (
          <div className="space-y-3">
            {items.map((item) => {
              const status = text(item.stage || item.status);
              const id = item.id;
              const prefix = kind === "pipeline" ? "/api/tasks" : "/api/jobs";
              return (
                <Card key={text(id)} className="gap-0 py-0 shadow-none">
                  <CardContent className="p-5">
                    <div className="flex flex-wrap items-start gap-4">
                      <span className="grid size-10 place-items-center rounded-xl bg-muted">
                        <ListTodo className="size-4 text-muted-foreground" />
                      </span>
                      <div className="min-w-0 flex-1">
                        <h2 className="text-sm font-semibold">
                          {text(item.label || item.kind, `流水线任务 #${id}`)}
                        </h2>
                        <p className="mt-2 text-xs text-muted-foreground">
                          #{text(id)} · {time(item.created_at)}
                          {item.segment_id ? ` · 分段 #${item.segment_id}` : ""}
                          {item.session_id ? ` · 场次 #${item.session_id}` : ""}
                        </p>
                      </div>
                      <Status value={status} />
                      <div className="flex gap-2">
                        {/failed|cancelled/.test(status) && (
                          <ConfirmButton
                            title="重试此任务？"
                            description="按当前服务端配置重新执行；发布结果未知时请先核对平台记录。"
                            onConfirm={() =>
                              action.mutateAsync({
                                path: `${prefix}/${id}/retry`,
                              })
                            }
                          >
                            重试
                          </ConfirmButton>
                        )}
                        {!/completed|succeeded|failed|cancelled/.test(
                          status,
                        ) && (
                          <ConfirmButton
                            title="请求取消任务？"
                            description="已执行的阶段无法撤销，正在发布的任务可能需要先核对结果。"
                            onConfirm={() =>
                              action.mutateAsync({
                                path: `${prefix}/${id}/cancel`,
                              })
                            }
                          >
                            取消
                          </ConfirmButton>
                        )}
                      </div>
                    </div>
                    {item.progress !== undefined &&
                      typeof item.progress === "number" && (
                        <Progress
                          value={
                            item.progress <= 1
                              ? item.progress * 100
                              : item.progress
                          }
                          className="mt-4"
                        />
                      )}
                    {item.error || item.last_error ? (
                      <p role="alert" className="error-panel mt-4 text-xs">
                        {text(item.error || item.last_error)}
                      </p>
                    ) : null}
                    <details className="mt-4">
                      <summary className="cursor-pointer text-xs text-muted-foreground">
                        任务详情
                      </summary>
                      <div className="mt-3">
                        <Evidence value={item} />
                      </div>
                    </details>
                  </CardContent>
                </Card>
              );
            })}
          </div>
        ) : (
          <Empty
            title="当前没有任务"
            description="录制、导入、分析和渲染触发的后台工作会显示在这里。"
          />
        )}
      </QueryState>
    </Page>
  );
}
export function UploadsPage() {
  const query = useApi("/api/uploads?limit=200");
  const action = useAction();
  return (
    <Page
      eyebrow="PUBLISH / DELIVERIES"
      title="上传与发布"
      description="核对每一次投稿结果。手动模式导出清单后，还需要在成品库确认平台投稿。"
      actions={<Refresh refresh={query.refetch} />}
    >
      <QueryState query={query}>
        {rows(query.data).length ? (
          <div className="space-y-4">
            {rows(query.data).map((item, index) => (
              <Card key={text(item.id, String(index))}>
                <CardContent className="space-y-4">
                  <div className="flex flex-wrap items-center gap-3">
                    <Upload className="size-5 text-primary" />
                    <h2 className="flex-1 text-sm font-semibold">
                      {text(item.title, `投稿任务 #${item.id}`)}
                    </h2>
                    <Status value={item.status || item.stage} />
                  </div>
                  <DetailList
                    values={[
                      {
                        label: "平台",
                        value: text(item.platform || item.uploader),
                      },
                      {
                        label: "稿件号",
                        value: text(
                          item.submission_id || item.remote_id || item.bvid,
                        ),
                      },
                      {
                        label: "任务 ID",
                        value: text(item.task_id || item.id),
                      },
                      {
                        label: "更新时间",
                        value: time(item.updated_at || item.created_at),
                      },
                    ]}
                  />
                  {item.error || item.last_error ? (
                    <p className="error-panel text-xs">
                      {text(item.error || item.last_error)}
                    </p>
                  ) : null}
                  <div className="flex gap-3">
                    {item.published_url ? (
                      <ExternalLink href={text(item.published_url)}>
                        查看平台稿件
                      </ExternalLink>
                    ) : null}
                    {/failed|retry/.test(text(item.status || item.stage)) && (
                      <ConfirmButton
                        title="重试上传？"
                        description="如果平台结果不明确，请先核对稿件，避免重复投稿。"
                        onConfirm={() =>
                          action.mutateAsync({
                            path: `/api/uploads/${item.task_id || item.id}/retry`,
                          })
                        }
                      >
                        重试上传
                      </ConfirmButton>
                    )}
                    <Button variant="ghost" size="sm" asChild>
                      <Link to="/clips">查看成品库</Link>
                    </Button>
                  </div>
                  <details>
                    <summary className="cursor-pointer text-xs text-muted-foreground">
                      查看投递详情
                    </summary>
                    <Evidence value={item} />
                  </details>
                </CardContent>
              </Card>
            ))}
          </div>
        ) : (
          <Empty
            title="还没有上传记录"
            description="审核成品后可以导出手动投稿清单，或按配置加入自动上传队列。"
            action={
              <Button asChild>
                <Link to="/clips">查看成品库</Link>
              </Button>
            }
          />
        )}
      </QueryState>
    </Page>
  );
}
function logTimestamp(entry: Row): number | null {
  const value = entry.created_at || entry.timestamp;
  if (!value) return null;
  const source = String(value);
  const timestamp = Date.parse(
    /[zZ]|[+-]\d\d:\d\d$/.test(source) ? source : `${source}Z`,
  );
  return Number.isFinite(timestamp) ? timestamp : null;
}

export function LogsPage() {
  const [level, setLevel] = useState("");
  const [search, setSearch] = useState("");
  const [order, setOrder] = useState("desc");
  const [date, setDate] = useState("");
  const params = new URLSearchParams({ limit: "500" });
  if (level) params.set("level", level);
  if (date) {
    const start = new Date(`${date}T00:00:00`);
    const end = new Date(start);
    end.setDate(end.getDate() + 1);
    params.set("start_at", start.toISOString());
    params.set("end_at", end.toISOString());
  }
  const query = useApi(`/api/logs?${params}`, 10000);
  const logs = rows(query.data)
    .filter((entry) =>
      JSON.stringify(entry).toLowerCase().includes(search.toLowerCase()),
    )
    .sort((left, right) => {
      const a = logTimestamp(left);
      const b = logTimestamp(right);
      if (a === null || b === null) return a === b ? 0 : a === null ? 1 : -1;
      const comparison = a - b || number(left.id) - number(right.id);
      return order === "desc" ? -comparison : comparison;
    });
  return (
    <Page
      eyebrow="OPERATIONS / LOGS"
      title="错误日志"
      description="查看后端持久化的警告与错误。浏览器读取失败会直接显示，方便区分服务异常与网络问题。"
      actions={<Refresh refresh={query.refetch} />}
    >
      <div className="flex flex-wrap gap-3">
        <Input
          className="max-w-md bg-card"
          aria-label="搜索日志内容"
          placeholder="搜索日志内容…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          className="native-select w-40"
          aria-label="日志等级"
          value={level}
          onChange={(event) => setLevel(event.target.value)}
        >
          <option value="">全部等级</option>
          <option>WARNING</option>
          <option>ERROR</option>
          <option>CRITICAL</option>
        </select>
        <Input
          type="date"
          aria-label="日志日期"
          title="按本地日期筛选日志"
          className="w-44 bg-card"
          value={date}
          onChange={(event) => setDate(event.target.value)}
        />
        {date && (
          <Button variant="ghost" onClick={() => setDate("")}>
            清除日期
          </Button>
        )}
        <select
          className="native-select w-40"
          aria-label="日志排序"
          value={order}
          onChange={(event) => setOrder(event.target.value)}
        >
          <option value="desc">最新在前</option>
          <option value="asc">最早在前</option>
        </select>
      </div>
      <p className="text-xs text-muted-foreground">
        日期按浏览器本地时区筛选，与日志显示时间一致。最多显示符合条件的最近 500
        条。
      </p>
      <QueryState query={query}>
        {logs.length ? (
          <div className="overflow-hidden rounded-2xl border bg-card">
            {logs.map((entry, index) => (
              <div
                key={text(entry.id, String(index))}
                className="border-b p-5 last:border-0"
              >
                <div className="mb-3 flex flex-wrap gap-3 text-xs">
                  <Badge
                    variant={
                      entry.level === "ERROR" ? "destructive" : "secondary"
                    }
                  >
                    {text(entry.level)}
                  </Badge>
                  <span className="text-muted-foreground">
                    {time(entry.created_at || entry.timestamp)}
                  </span>
                  <span className="text-muted-foreground">
                    {text(entry.module || entry.source, "")}
                  </span>
                </div>
                <p className="whitespace-pre-wrap break-words font-mono text-xs leading-6">
                  {text(entry.message || entry.content)}
                </p>
              </div>
            ))}
          </div>
        ) : (
          <Empty
            title="没有匹配的警告或错误"
            description="这是后端保存的日志列表，完整运行日志仍可在服务控制台查看。"
          />
        )}
      </QueryState>
    </Page>
  );
}
export function TrendsPage() {
  const [days, setDays] = useState("7");
  const query = useApi(`/api/trends?limit=100&days=${days}`, 30000);
  const action = useAction();
  const data = obj(query.data);
  return (
    <Page
      eyebrow="ANALYZE / CULTURE"
      title="网感资料库"
      description="为内容分析补充近期热点与语境。采集状态及资料时间以服务端记录为准。"
      actions={
        <>
          <Refresh refresh={query.refetch} />
          <FormDialog
            id="trend-collect"
            title="立即采集资料"
            description="将联网采集并写入资料库，需要已启用相关配置。"
            trigger={<Button disabled={action.isPending}>立即采集</Button>}
            fields={[{ name: "topic", label: "关注主题（可选）" }]}
            onSubmit={(body) =>
              action.mutateAsync({ path: "/api/trends/collect", body })
            }
          />
        </>
      }
    >
      <div className="flex gap-3">
        <Status value={data.enabled ? "enabled" : "disabled"} />
        <select
          className="native-select ml-auto w-36"
          aria-label="资料时间范围"
          value={days}
          onChange={(event) => setDays(event.target.value)}
        >
          <option value="1">近 1 天</option>
          <option value="7">近 7 天</option>
          <option value="30">近 30 天</option>
        </select>
      </div>
      <QueryState query={query}>
        <div className="rounded-2xl border bg-card p-5">
          <h2 className="mb-4 text-sm font-semibold">近期热词</h2>
          <div className="flex flex-wrap gap-2">
            {(Array.isArray(data.keywords) ? data.keywords : []).map(
              (entry, index) => {
                const word =
                  typeof entry === "string"
                    ? entry
                    : Array.isArray(entry)
                      ? String(entry[0])
                      : text(obj(entry).keyword || obj(entry).word);
                return (
                  <Badge
                    variant="secondary"
                    key={index}
                    className="px-3 py-1.5"
                  >
                    #{word}
                  </Badge>
                );
              },
            )}
          </div>
        </div>
        {rows(data.items).length ? (
          <div className="grid gap-4 md:grid-cols-2">
            {rows(data.items).map((entry) => (
              <Card key={text(entry.id)}>
                <CardContent className="space-y-4">
                  <div className="flex gap-2">
                    <Badge variant="outline">
                      {text(entry.category || entry.source, "热点资料")}
                    </Badge>
                    <span className="ml-auto text-xs text-muted-foreground">
                      热度 {number(entry.heat)}
                    </span>
                  </div>
                  <h2 className="font-semibold">{text(entry.title)}</h2>
                  <p className="text-sm leading-7 text-muted-foreground">
                    {text(entry.summary)}
                  </p>
                  <div className="flex justify-between">
                    <ExternalLink href={text(entry.url, "")}>
                      阅读来源
                    </ExternalLink>
                    <span className="text-xs text-muted-foreground">
                      {time(entry.collected_at)}
                    </span>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        ) : (
          <Empty
            title="资料库暂时为空"
            description="启用网感采集并配置联网服务后，手动采集或等待定时更新。"
          />
        )}
        <details className="rounded-2xl border bg-card p-5">
          <summary className="cursor-pointer text-sm">采集调度状态</summary>
          <div className="mt-4">
            <Evidence value={data.scheduler} />
          </div>
        </details>
      </QueryState>
    </Page>
  );
}
export function AnalyticsPage() {
  const [days, setDays] = useState("30");
  const query = useApi(`/api/analytics?days=${days}`, 30000);
  const data = obj(query.data);
  const overview = obj(data.overview);
  const daily = rows(data.daily_trend);
  const maximum = Math.max(
    1,
    ...daily.map((entry) => number(entry.clips) + number(entry.candidates)),
  );
  return (
    <Page
      eyebrow="INSIGHTS"
      title="数据分析"
      description="按包含今天的 UTC 自然日统计所选范围内的录制、产出和候选质量。"
      actions={
        <>
          <select
            className="native-select w-32"
            aria-label="分析时间范围"
            value={days}
            onChange={(event) => setDays(event.target.value)}
          >
            <option value="7">近 7 天</option>
            <option value="30">近 30 天</option>
            <option value="90">近 90 天</option>
          </select>
          <Refresh refresh={query.refetch} />
        </>
      }
    >
      <QueryState query={query}>
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <Stat
            label="成品总数"
            value={number(overview.total_clips)}
            icon={<Activity className="size-4" />}
          />
          <Stat label="已发布" value={number(overview.published_clips)} />
          <Stat label="候选总数" value={number(overview.total_candidates)} />
          <Stat
            label="成品总时长"
            value={`${number(overview.total_duration_h).toFixed(2)} h`}
          />
        </div>
        <Card>
          <CardContent>
            <div className="mb-8 flex justify-between">
              <h2 className="font-semibold">每日产出</h2>
              <div className="flex gap-4 text-xs text-muted-foreground">
                <span>● 候选</span>
                <span className="text-primary">● 成品</span>
              </div>
            </div>
            {daily.length ? (
              <div className="flex h-56 items-end gap-1 overflow-x-auto border-b pb-6">
                {daily.map((entry) => (
                  <div
                    key={text(entry.date)}
                    className="group relative flex h-full min-w-5 flex-1 items-end justify-center gap-0.5"
                    title={`${entry.date}：${number(entry.clips)} 成品 / ${number(entry.candidates)} 候选`}
                  >
                    <div
                      className="w-2/5 rounded-t bg-primary/20"
                      style={{
                        height: `${(number(entry.candidates) / maximum) * 100}%`,
                      }}
                    />
                    <div
                      className="w-2/5 rounded-t bg-primary"
                      style={{
                        height: `${(number(entry.clips) / maximum) * 100}%`,
                      }}
                    />
                    <span className="absolute -bottom-5 text-[9px] text-muted-foreground">
                      {text(entry.date).slice(5)}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <Empty
                title="还没有统计记录"
                description="完成录制和制作后，这里会展示真实的每日产出。"
              />
            )}
          </CardContent>
        </Card>
        <div className="grid gap-5 xl:grid-cols-2">
          <Card>
            <CardContent className="space-y-4">
              <h2 className="font-semibold">评分分布</h2>
              <Evidence value={data.score_distribution} />
            </CardContent>
          </Card>
          <Card>
            <CardContent className="space-y-4">
              <h2 className="font-semibold">房间排行</h2>
              <Evidence value={data.room_ranking} />
            </CardContent>
          </Card>
        </div>
      </QueryState>
    </Page>
  );
}
export function MonitorPage() {
  const query = useApi("/api/monitor", 10000);
  const models = useApi("/api/monitor/asr-models", 30000);
  const metrics = useApi("/api/monitor/asr-metrics", 10000);
  const action = useAction();
  const data = obj(query.data);
  const disk = obj(data.disk);
  const memory = obj(data.memory);
  return (
    <Page
      eyebrow="OPERATIONS / HEALTH"
      title="运行状态"
      description="查看磁盘、资源与工作器状态；模型不可用和真实零负载分别显示。"
      actions={
        <>
          <Refresh refresh={query.refetch} />
          <ConfirmButton
            title="执行磁盘维护？"
            description="将按现有保留策略清理符合条件的媒体文件。请先确认存储设置。"
            onConfirm={() =>
              action.mutateAsync({ path: "/api/monitor/disk-maintenance" })
            }
          >
            执行磁盘维护
          </ConfirmButton>
        </>
      }
    >
      <QueryState query={query}>
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <Stat
            label="CPU 使用率"
            value={
              data.cpu_percent === null
                ? "不可用"
                : `${number(data.cpu_percent).toFixed(1)}%`
            }
            icon={<Cpu className="size-4" />}
          />
          <Stat
            label="内存使用率"
            value={
              memory.percent === null
                ? "不可用"
                : `${number(memory.percent).toFixed(1)}%`
            }
          />
          <Stat
            label="磁盘可用"
            value={`${number(disk.free_gb).toFixed(1)} GB`}
            hint={`共 ${number(disk.total_gb).toFixed(1)} GB`}
            icon={<HardDrive className="size-4" />}
          />
          <Stat label="正在录制" value={number(data.running_room_count)} />
        </div>
        <Card>
          <CardContent className="space-y-5">
            <div className="flex justify-between">
              <h2 className="font-semibold">存储状态</h2>
              <Status value={data.disk_safe ? "success" : "warning"} />
            </div>
            <Progress value={100 - number(disk.free_percent)} />
            <p className="text-sm text-muted-foreground">
              {text(data.disk_safe_message, "磁盘检查已完成")}
            </p>
            <DetailList
              values={[
                {
                  label: "原始媒体",
                  value: `${number(data.raw_size_gb).toFixed(2)} GB`,
                },
                {
                  label: "成品切片",
                  value: `${number(data.clips_size_gb).toFixed(2)} GB`,
                },
                {
                  label: "最长任务等待",
                  value: duration(obj(data.tasks).oldest_wait_s),
                },
              ]}
            />
          </CardContent>
        </Card>
        <div className="grid items-start gap-5 xl:grid-cols-2">
          <Card>
            <CardContent className="space-y-4">
              <h2 className="font-semibold">ASR 模型</h2>
              <QueryState query={models}>
                <Evidence value={models.data} />
              </QueryState>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="space-y-4">
              <h2 className="font-semibold">转写指标</h2>
              <QueryState query={metrics}>
                <Evidence value={metrics.data} />
              </QueryState>
            </CardContent>
          </Card>
        </div>
        <details className="rounded-2xl border bg-card p-5">
          <summary className="cursor-pointer font-medium">
            任务与录制监视器
          </summary>
          <div className="mt-4 space-y-4">
            <Evidence value={data.tasks} />
            <Evidence value={data.monitor} />
          </div>
        </details>
        <Card>
          <CardContent className="space-y-4">
            <h2 className="font-semibold">最近失败</h2>
            <Evidence value={data.recent_failures} />
          </CardContent>
        </Card>
      </QueryState>
    </Page>
  );
}
