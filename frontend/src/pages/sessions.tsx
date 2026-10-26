import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { Clock3, Sparkles } from "lucide-react";
import { useAction, useApi } from "@/api/client";
import { EditorialResult } from "@/components/editorial-result";
import { EditorialReanalysis } from "@/components/editorial-reanalysis";
import { obj, rows, text, number, time, duration } from "@/lib/data";
import {
  Page,
  QueryState,
  Empty,
  Refresh,
  Status,
  ConfirmButton,
  DetailList,
} from "@/components/workbench";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Evidence } from "./review";
import { SessionPicker } from "./library";

export function SessionsPage() {
  const [params, setParams] = useSearchParams();
  const session = params.get("session") || "";
  const [rejected, setRejected] = useState(false);
  const query = useApi(
    `/api/sessions/timeline?limit=100${params.get("room") ? `&room_db_id=${params.get("room")}` : ""}${session ? `&session_id=${session}` : ""}`,
    10000,
  );
  const detail = useApi(
    `/api/sessions/${session}/timeline?include_rejected=${rejected}`,
    10000,
    !!session,
  );
  const titles = useApi(`/api/sessions/${session}/titles`, false, !!session);
  const action = useAction();
  const data = obj(detail.data);
  const info = obj(data.session);
  return (
    <Page
      eyebrow="ANALYZE / MOMENTS"
      title="场次时间线"
      description="从整场内容回看热点、语义证据与候选。热度高的时刻，也需要结合上下文判断。"
      actions={
        <>
          <SessionPicker
            value={session}
            onChange={(value) => setParams(value ? { session: value } : {})}
          />
          <Refresh
            refresh={() => {
              void query.refetch();
              if (session) void detail.refetch();
            }}
          />
        </>
      }
    >
      {!session ? (
        <QueryState query={query}>
          {rows(query.data).length ? (
            <div className="grid gap-4 md:grid-cols-2">
              {rows(query.data).map((entry) => {
                const id = entry.session_id || entry.id;
                return (
                  <Card key={text(id)} className="gap-0 py-0 shadow-none">
                    <CardContent className="space-y-4 p-6">
                      <div className="flex items-start justify-between gap-4">
                        <span className="grid size-11 place-items-center rounded-xl bg-primary/8 text-primary">
                          <Clock3 className="size-5" />
                        </span>
                        <Status
                          value={entry.processing_state || entry.status}
                        />
                      </div>
                      <h2 className="font-semibold">
                        {text(
                          entry.session_title ||
                            entry.title ||
                            entry.uploader_name,
                          `场次 #${id}`,
                        )}
                      </h2>
                      <p className="text-xs text-muted-foreground">
                        {time(entry.started_at, "GMT+8")} · 时长{" "}
                        {duration(entry.duration_s)}
                      </p>
                      <div className="flex gap-2">
                        <Badge variant="secondary">
                          {number(entry.candidate_count)} 个候选
                        </Badge>
                        <Badge variant="outline">
                          {number(entry.hotspot_count || entry.timeline_count)}{" "}
                          个热点
                        </Badge>
                      </div>
                      <Button
                        variant="outline"
                        className="w-full"
                        onClick={() => setParams({ session: String(id) })}
                      >
                        查看场次时间线
                      </Button>
                    </CardContent>
                  </Card>
                );
              })}
            </div>
          ) : (
            <Empty
              title="还没有场次时间线"
              description="完成一场录制或导入视频，并运行高光分析。"
            />
          )}
        </QueryState>
      ) : (
        <>
          <Button variant="ghost" onClick={() => setParams({})}>
            ← 返回全部场次
          </Button>
          <QueryState query={detail}>
            <Card>
              <CardContent className="space-y-5">
                <div className="flex flex-wrap justify-between gap-3">
                  <div>
                    <h2 className="text-lg font-semibold">
                      {text(
                        info.session_title || info.title || info.uploader_name,
                        `场次 #${session}`,
                      )}
                    </h2>
                    <p className="mt-2 text-xs text-muted-foreground">
                      {time(info.started_at, "GMT+8")} · 时长{" "}
                      {duration(info.duration_s)}
                    </p>
                  </div>
                  <Status value={info.processing_state || info.status} />
                </div>
                <DetailList
                  values={[
                    {
                      label: "可见热点",
                      value: number(obj(data.counts).visible),
                    },
                    {
                      label: "已拒绝",
                      value: number(obj(data.counts).rejected),
                    },
                  ]}
                />
                <div className="flex flex-wrap gap-2">
                  <ConfirmButton
                    title="重新生成整场总结？"
                    description="将提交后台总结任务。"
                    onConfirm={() =>
                      action.mutateAsync({
                        path: `/api/sessions/${session}/timeline-summary`,
                      })
                    }
                  >
                    生成整场总结
                  </ConfirmButton>
                  <ConfirmButton
                    title="重新分析场次？"
                    description="使用现有转写重新运行分析，会消耗计算或大模型调用资源。"
                    onConfirm={() =>
                      action.mutateAsync({
                        path: `/api/sessions/${session}/reanalyze`,
                        body: { reason: "manual", retranscribe: false },
                      })
                    }
                  >
                    重新分析
                  </ConfirmButton>
                  <ConfirmButton
                    title="重新转写并分析？"
                    description="会重新处理整场媒体，耗时较长。已有登录凭据和原始媒体不会删除。"
                    onConfirm={() =>
                      action.mutateAsync({
                        path: `/api/sessions/${session}/reanalyze`,
                        body: { reason: "manual", retranscribe: true },
                      })
                    }
                  >
                    重新转写并分析
                  </ConfirmButton>
                  <ConfirmButton
                    title="按主题聚类候选？"
                    description="后端将为本场候选生成主题分组。"
                    onConfirm={() =>
                      action.mutateAsync({
                        path: `/api/sessions/${session}/cluster`,
                      })
                    }
                  >
                    主题聚类
                  </ConfirmButton>
                  <Button variant="ghost" asChild>
                    <Link to={`/transcripts?session=${session}`}>查看转写</Link>
                  </Button>
                </div>
              </CardContent>
            </Card>
            <EditorialReanalysis key={session} sessionId={Number(session)} />
            <details className="rounded-2xl border bg-card p-5">
              <summary className="cursor-pointer font-medium">
                整场高光总结
              </summary>
              <div className="mt-4">
                <Evidence value={data.whole_session_summary} />
              </div>
            </details>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={rejected}
                onChange={(event) => setRejected(event.target.checked)}
              />
              显示已拒绝的事件
            </label>
            {rows(data.points).length ? (
              <div className="relative ml-3 border-l-2 border-primary/15 pl-7">
                {rows(data.points).map((point) => (
                  <article
                    key={text(point.key, `hotspot:${point.hotspot_event_id}`)}
                    className="relative mb-7 rounded-2xl border bg-card p-5"
                  >
                    <span className="absolute top-6 -left-[37px] size-4 rounded-full border-4 border-background bg-primary" />
                    <div className="mb-3 flex flex-wrap items-center gap-2">
                      <Badge variant="secondary">
                        {duration(point.offset_s)}
                      </Badge>
                      <span className="text-xs text-muted-foreground">
                        {point.hotspot_duration_s != null
                          ? `热点跨度 ${duration(point.hotspot_duration_s)}`
                          : `分析范围 ${duration(point.duration_s)}`}
                        {point.clip_duration_s != null
                          ? ` · 实际成片 ${duration(point.clip_duration_s)}`
                          : " · 尚无成片范围"}
                      </span>
                      {point.rejected ? <Status value="rejected" /> : null}
                      {point.candidate_id ? (
                        <Badge variant="outline">
                          候选 #{text(point.candidate_id)}
                        </Badge>
                      ) : null}
                    </div>
                    <h3 className="font-semibold">
                      {text(point.title, "热点事件")}
                    </h3>
                    <p className="mt-3 text-sm leading-7 text-muted-foreground">
                      {text(point.summary)}
                    </p>
                    <div className="mt-4 flex flex-wrap gap-2">
                      {Array.isArray(point.source_signals)
                        ? point.source_signals.map((signal) => (
                            <Badge key={String(signal)} variant="outline">
                              {text(signal)}
                            </Badge>
                          ))
                        : null}
                      <span className="ml-auto text-xs text-muted-foreground">
                        热度{" "}
                        {point.heat_score == null
                          ? "未知"
                          : number(point.heat_score).toFixed(2)}{" "}
                        · 技术分数{" "}
                        {point.clip_score == null
                          ? "未知"
                          : number(point.clip_score).toFixed(2)}
                      </span>
                    </div>
                    <div className="mt-4">
                      <EditorialResult
                        value={point.editorial}
                        heading={false}
                      />
                    </div>
                    {point.candidate_id ? (
                      <Button asChild size="sm" className="mt-4">
                        <Link to={`/review/${point.candidate_id}`}>
                          <Sparkles className="size-3" />
                          打开审片
                        </Link>
                      </Button>
                    ) : null}
                    <details className="mt-4">
                      <summary className="cursor-pointer text-xs text-muted-foreground">
                        弹幕与来源证据
                      </summary>
                      <div className="mt-3 space-y-3">
                        <Evidence value={point.representative_danmaku} />
                        <Evidence value={point.provenance} />
                      </div>
                    </details>
                  </article>
                ))}
              </div>
            ) : (
              <Empty
                title="这场还没有热点事件"
                description="可以检查分析任务和转写结果，或手动重新分析。"
              />
            )}
            <details className="rounded-2xl border bg-card p-5">
              <summary className="cursor-pointer text-sm font-medium">
                直播标题变化
              </summary>
              <div className="mt-4">
                <QueryState query={titles}>
                  <Evidence value={titles.data} />
                </QueryState>
              </div>
            </details>
          </QueryState>
        </>
      )}
    </Page>
  );
}
