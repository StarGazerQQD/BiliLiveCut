import {
  ArrowRight,
  Clapperboard,
  Clock3,
  Radio,
  Sparkles,
  Upload,
  WandSparkles,
} from "lucide-react";
import { Link } from "react-router";
import { useApi } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Empty, Page, QueryState, Stat, Status } from "@/components/workbench";
import { obj, rows, number, text, time, sessionSource } from "@/lib/data";
import { AddRoom, RoomCard } from "./rooms";

export default function OverviewPage() {
  const dashboard = useApi("/api/dashboard");
  const jobs = useApi("/api/jobs");
  const sessions = useApi("/api/recording");
  const counts = obj(obj(dashboard.data).counts);
  const rooms = rows(obj(dashboard.data).rooms);
  const recent = rows(jobs.data).length
    ? rows(jobs.data)
    : rows(obj(jobs.data).jobs);
  return (
    <Page
      eyebrow={text(obj(dashboard.data).version, "")}
      title="工作台概览"
      description="从一段录播开始，把值得保留的瞬间整理成高光。"
      actions={<AddRoom />}
    >
      <div className="overview-hero">
        <div className="relative z-10 max-w-xl">
          <div className="mb-4 flex items-center gap-2 text-xs font-medium tracking-wider text-sky-200">
            <span className="size-1.5 rounded-full bg-sky-300" />
            RECORD · DISCOVER · REFINE
          </div>
          <h2 className="text-[28px] leading-tight font-semibold tracking-tight md:text-[34px]">
            内容已经发生，
            <br />
            精彩值得被看见。
          </h2>
          <p className="mt-4 max-w-md text-sm leading-7 text-slate-300">
            导入录播，让转写和高光识别帮你找到线索。
            <br className="hidden md:block" />
            再用人工审片，决定哪些片刻值得留下。
          </p>
          <div className="mt-6 flex flex-wrap gap-3">
            <Button
              asChild
              className="bg-white text-slate-900 hover:bg-slate-100"
            >
              <Link to="/imports">
                <Upload className="size-4" />
                导入录播
                <ArrowRight className="size-4" />
              </Link>
            </Button>
            <Button
              asChild
              variant="ghost"
              className="text-white hover:bg-white/10 hover:text-white"
            >
              <Link to="/review">
                进入审片工作台
                <ArrowRight className="size-4" />
              </Link>
            </Button>
          </div>
        </div>
        <div className="hero-timeline" aria-hidden="true">
          <div className="hero-frame">
            <Clapperboard className="size-12 text-white/90" />
            <span>YOUR NEXT HIGHLIGHT</span>
          </div>
          <div className="hero-wave">
            {Array.from({ length: 36 }, (_, index) => (
              <i
                key={index}
                style={{
                  height: `${10 + Math.abs(Math.sin(index * 0.79)) * 46}px`,
                }}
              />
            ))}
          </div>
          <span className="hero-playhead" />
          <span className="hero-cut hero-cut-a" />
          <span className="hero-cut hero-cut-b" />
        </div>
      </div>
      <QueryState query={dashboard}>
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <Stat
            label="正在录制"
            value={number(counts.active_sessions)}
            hint={`${rooms.length} 个已添加的直播间`}
            icon={<Radio className="size-4" />}
          />
          <Stat
            label="高光候选"
            value={number(counts.candidates)}
            hint="等待你发现值得保留的片刻"
            icon={<WandSparkles className="size-4" />}
          />
          <Stat
            label="成品切片"
            value={number(counts.clips)}
            hint="查看字幕、封面与发布状态"
            icon={<Clapperboard className="size-4" />}
          />
          <Stat
            label="进行中的作业"
            value={
              recent.filter((job) =>
                ["running", "queued", "cancelling"].includes(
                  String(job.status),
                ),
              ).length
            }
            hint="后台处理独立于当前页面"
            icon={<Clock3 className="size-4" />}
          />
        </div>
        <section>
          <div className="section-heading">
            <div>
              <h2>直播来源</h2>
              <p>录制状态与自动化一目了然</p>
            </div>
            <Button variant="ghost" size="sm" asChild>
              <Link to="/rooms">
                管理全部
                <ArrowRight className="size-3" />
              </Link>
            </Button>
          </div>
          {rooms.length ? (
            <div className="grid gap-5 md:grid-cols-2 xl:grid-cols-3">
              {rooms.slice(0, 3).map((room) => (
                <RoomCard key={text(room.id)} room={room} compact />
              ))}
            </div>
          ) : (
            <Empty
              title="还没有添加直播间"
              description="连接直播来源，或直接导入已有录播开始分析。"
              action={
                <div className="flex gap-2">
                  <AddRoom />
                  <Button asChild variant="outline">
                    <Link to="/imports">导入已有录播</Link>
                  </Button>
                </div>
              }
            />
          )}
        </section>
      </QueryState>
      <div className="grid gap-6 xl:grid-cols-[1.2fr_1fr]">
        <section>
          <div className="section-heading">
            <div>
              <h2>最近录制</h2>
              <p>每一场内容都有自己的时间线</p>
            </div>
            <Button variant="ghost" size="sm" asChild>
              <Link to="/recording">
                查看全部
                <ArrowRight className="size-3" />
              </Link>
            </Button>
          </div>
          <QueryState query={sessions}>
            {rows(sessions.data).length ? (
              <Card className="gap-0 py-0 shadow-none">
                <CardContent className="divide-y px-5">
                  {rows(sessions.data)
                    .slice(0, 5)
                    .map((session) => (
                      <Link
                        key={text(session.id)}
                        to={`/sessions?session=${session.id}`}
                        className="flex items-center gap-3 py-4"
                      >
                        <span className="grid size-9 place-items-center rounded-xl bg-primary/8 text-primary">
                          <Radio className="size-4" />
                        </span>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm font-medium">
                            {text(
                              session.session_title,
                              `录制会话 #${session.id}`,
                            )}
                          </p>
                          <p className="mt-1 text-xs text-muted-foreground">
                            {sessionSource(session)} ·{" "}
                            {time(session.started_at)}
                          </p>
                        </div>
                        <Status value={session.status} />
                      </Link>
                    ))}
                </CardContent>
              </Card>
            ) : (
              <Empty
                title="录制历史将显示在这里"
                description="开始一次录制，或导入录播，即可在时间线上追踪处理进度。"
              />
            )}
          </QueryState>
        </section>
        <section>
          <div className="section-heading">
            <div>
              <h2>后台作业</h2>
              <p>上传、分析与渲染的最新进展</p>
            </div>
            <Button variant="ghost" size="sm" asChild>
              <Link to="/tasks">
                任务中心
                <ArrowRight className="size-3" />
              </Link>
            </Button>
          </div>
          <QueryState query={jobs}>
            {recent.length ? (
              <Card className="gap-0 py-0 shadow-none">
                <CardContent className="divide-y px-5">
                  {recent.slice(0, 5).map((job) => (
                    <div
                      key={text(job.id)}
                      className="flex items-center gap-3 py-4"
                    >
                      <span className="grid size-9 place-items-center rounded-xl bg-muted text-muted-foreground">
                        <Sparkles className="size-4" />
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium">
                          {text(job.label || job.kind)}
                        </p>
                        <p className="mt-1 truncate text-xs text-muted-foreground">
                          {text(job.message, "等待下一次状态更新")}
                        </p>
                      </div>
                      <Status value={job.status} />
                    </div>
                  ))}
                </CardContent>
              </Card>
            ) : (
              <Empty
                title="现在没有后台作业"
                description="任务提交后会在这里持续更新，离开页面不会停止处理。"
              />
            )}
          </QueryState>
        </section>
      </div>
    </Page>
  );
}
