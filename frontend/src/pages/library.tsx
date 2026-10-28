import { copyText } from "@/lib/clipboard";
import { useEffect, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import {
  CalendarClock,
  Check,
  Download,
  FileText,
  Play,
  Plus,
  Radio,
  RefreshCw,
  Send,
} from "lucide-react";
import { toast } from "sonner";
import { send, useAction, useApi } from "@/api/client";
import { authHeaders, expireAuth } from "@/api/auth";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  Page,
  QueryState,
  Empty,
  Refresh,
  Status,
  Stat,
  FormDialog,
  ConfirmButton,
  DetailList,
} from "@/components/workbench";
import { duration, number, obj, rows, text, time, title } from "@/lib/data";

export async function download(path: string, filename: string) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: authHeaders(),
  });
  if (!response.ok) {
    if (response.status === 401) expireAuth();
    let message = `下载失败（${response.status}）`;
    try {
      const value = await response.json();
      message = value.detail || message;
    } catch {
      /* Keep the HTTP diagnostic. */
    }
    throw new Error(message);
  }
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

export function SessionPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  const query = useApi("/api/sessions/history", 15000);
  return (
    <select
      className="native-select max-w-xs"
      aria-label="筛选录制场次"
      value={value}
      onChange={(event) => onChange(event.target.value)}
    >
      <option value="">全部场次</option>
      {rows(query.data).map((session) => (
        <option key={text(session.session_id)} value={text(session.session_id)}>
          #{text(session.session_id)} ·{" "}
          {text(
            session.session_title || session.uploader_name || session.title,
            "录制场次",
          )}
        </option>
      ))}
    </select>
  );
}

const LIBRARY_PAGE_SIZE = 30;
const SESSION_PAGE_SIZE = 12;

function useLibraryState(kind: "transcripts" | "clips") {
  const [params, setParams] = useSearchParams();
  const session = params.get("session") || "";
  const unknown = params.get("unknown_session") === "1";
  const scoped = Boolean(session || unknown);
  const rawOffset = Number(params.get("offset") || "0");
  const offset =
    Number.isSafeInteger(rawOffset) && rawOffset >= 0 ? rawOffset : 0;
  const search = params.get("q") || "";
  const status = params.get("status") || "all";
  const filters = new URLSearchParams({ q: search });
  if (kind === "clips" && status !== "all") filters.set("status", status);
  const itemParams = new URLSearchParams(filters);
  itemParams.set("limit", String(LIBRARY_PAGE_SIZE + 1));
  itemParams.set("offset", String(offset));
  if (unknown) itemParams.set("unknown_session", "true");
  else if (session) itemParams.set("session_id", session);
  const query = useApi(`/api/${kind}?${itemParams}`, 5000, scoped);
  const groupParams = new URLSearchParams(filters);
  groupParams.set("kind", kind);
  groupParams.set("limit", String(SESSION_PAGE_SIZE + 1));
  groupParams.set("offset", String(offset));
  function update(next: Record<string, string | null>) {
    const updated = new URLSearchParams(params);
    updated.delete("offset");
    for (const [key, value] of Object.entries(next)) {
      if (value) updated.set(key, value);
      else updated.delete(key);
    }
    setParams(updated);
  }
  return {
    kind,
    session,
    unknown,
    scoped,
    offset,
    search,
    status,
    query,
    groupParams,
    update,
  };
}

function LibraryPagination({
  offset,
  count,
  size,
  onPage,
}: {
  offset: number;
  count: number;
  size: number;
  onPage: (offset: number) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3 text-sm">
      <span className="text-muted-foreground">
        第 {Math.floor(offset / size) + 1} 页
      </span>
      <div className="flex gap-2">
        <Button
          variant="outline"
          disabled={offset === 0}
          onClick={() => onPage(Math.max(0, offset - size))}
        >
          上一页
        </Button>
        <Button
          variant="outline"
          disabled={count <= size}
          onClick={() => onPage(offset + size)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}

function LibraryBrowser({
  library,
  children,
}: {
  library: ReturnType<typeof useLibraryState>;
  children: ReactNode;
}) {
  const groups = useApi(
    `/api/sessions/history?${library.groupParams}`,
    15000,
    !library.scoped,
  );
  const sessions = rows(groups.data);
  const onPage = (offset: number) => library.update({ offset: String(offset) });
  if (library.scoped)
    return (
      <>
        <div className="flex flex-wrap items-center gap-3">
          <Button
            variant="ghost"
            onClick={() =>
              library.update({ session: null, unknown_session: null })
            }
          >
            ← 返回场次
          </Button>
          <span className="text-sm font-medium">
            {library.unknown ? "未关联场次" : `场次 #${library.session}`}
          </span>
          {!library.unknown && (
            <Button variant="outline" size="sm" asChild>
              <Link to={`/sessions?session=${library.session}`}>
                场次时间线
              </Link>
            </Button>
          )}
        </div>
        {children}
        <LibraryPagination
          offset={library.offset}
          count={rows(library.query.data).length}
          size={LIBRARY_PAGE_SIZE}
          onPage={onPage}
        />
      </>
    );
  return (
    <QueryState query={groups}>
      <div className="flex justify-end">
        <Refresh refresh={groups.refetch} />
      </div>
      {sessions.length ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {sessions.slice(0, SESSION_PAGE_SIZE).map((session) => (
            <Card key={text(session.session_id, "unassigned")}>
              <CardContent className="space-y-4">
                <h2 className="font-semibold">
                  {text(
                    session.session_title || session.source_label,
                    session.session_id == null
                      ? "未关联场次"
                      : `场次 #${session.session_id}`,
                  )}
                </h2>
                <p className="text-xs text-muted-foreground">
                  {session.session_id == null
                    ? "历史记录缺少场次关联"
                    : `场次 #${session.session_id} · ${time(session.started_at, "GMT+8")}`}
                </p>
                <Badge variant="secondary">
                  {number(session.matching_count)}{" "}
                  {library.kind === "transcripts" ? "段转写" : "个成品"}
                </Badge>
                <Button
                  className="w-full"
                  variant="outline"
                  onClick={() =>
                    library.update({
                      session:
                        session.session_id == null
                          ? null
                          : String(session.session_id),
                      unknown_session: session.session_id == null ? "1" : null,
                    })
                  }
                >
                  查看{library.kind === "transcripts" ? "本场转写" : "本场成品"}
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <Empty
          title={library.search ? "没有匹配的场次" : "暂无可查看的场次"}
          description="录制或导入并处理媒体后，对应内容将在所属场次中显示。"
        />
      )}
      <LibraryPagination
        offset={library.offset}
        count={sessions.length}
        size={SESSION_PAGE_SIZE}
        onPage={onPage}
      />
    </QueryState>
  );
}

export function RecordingPage() {
  const progress = useApi("/api/progress");
  const query = useApi("/api/recording");
  const action = useAction();
  const sessions = rows(query.data);
  return (
    <Page
      eyebrow="CAPTURE / SESSIONS"
      title="录制状态"
      description="查看每场录制的真实状态。启动请求被接受后，仍需确认媒体持续写入。"
      actions={<Refresh refresh={query.refetch} busy={query.isFetching} />}
    >
      <QueryState query={progress}>
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <Stat
            label="媒体分段"
            value={number(obj(progress.data).total_segments)}
          />
          <Stat label="等待转写" value={number(obj(progress.data).recorded)} />
          <Stat
            label="等待分析"
            value={number(obj(progress.data).transcribed)}
          />
          <Stat
            label="已完成评分"
            value={number(obj(progress.data).scored)}
            hint={`完成比例 ${number(obj(progress.data).progress_pct)}%`}
          />
        </div>
      </QueryState>
      <QueryState query={query}>
        {sessions.length ? (
          <div className="space-y-4">
            {sessions.map((session) => (
              <Card key={text(session.id)} className="gap-0 py-0 shadow-none">
                <CardContent className="p-5">
                  <div className="flex flex-wrap items-start gap-4">
                    <span className="grid size-11 place-items-center rounded-xl bg-primary/8 text-primary">
                      <Radio className="size-5" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <h2 className="text-sm font-semibold">
                        {text(
                          session.session_title || session.uploader_name,
                          `录制会话 #${session.id}`,
                        )}
                      </h2>
                      <p className="mt-2 text-xs text-muted-foreground">
                        会话 #{text(session.id)} · {time(session.started_at)}
                        {session.ended_at ? ` → ${time(session.ended_at)}` : ""}
                      </p>
                    </div>
                    <Status value={session.status} />
                    <Button size="sm" variant="outline" asChild>
                      <Link to={`/sessions?session=${session.id}`}>
                        场次时间线
                      </Link>
                    </Button>
                    {["recording", "reconnecting", "starting"].includes(
                      String(session.status),
                    ) && (
                      <ConfirmButton
                        title="停止本场录制？"
                        description="保留已录制的媒体与后续分析任务。"
                        onConfirm={() =>
                          action.mutateAsync({
                            path: `/api/rooms/${session.room_id}/stop`,
                            body: { mode: "graceful", cancel_pending: false },
                            success: "停止请求已提交",
                          })
                        }
                      >
                        停止录制
                      </ConfirmButton>
                    )}
                  </div>
                  <div className="mt-5 grid gap-4 border-t pt-4 text-xs sm:grid-cols-4">
                    <span className="text-muted-foreground">
                      来源{" "}
                      <b className="ml-2 font-medium text-foreground">
                        {text(session.platform, "bilibili")}
                      </b>
                    </span>
                    <span className="text-muted-foreground">
                      分段{" "}
                      <b className="ml-2 font-medium text-foreground">
                        {text(
                          session.segments ??
                            session.segment_count ??
                            session.total_segments,
                          "0",
                        )}
                      </b>
                    </span>
                    <span className="text-muted-foreground">
                      重连{" "}
                      <b className="ml-2 font-medium text-foreground">
                        {number(session.reconnect_count)}
                      </b>
                    </span>
                    <span className="text-muted-foreground">
                      结束{" "}
                      <b className="ml-2 font-medium text-foreground">
                        {["stopped", "interrupted", "error"].includes(
                          String(session.status),
                        )
                          ? "已结束"
                          : "进行中"}
                      </b>
                    </span>
                  </div>
                  {session.error_message ? (
                    <p role="alert" className="error-panel mt-4 text-sm">
                      {text(session.error_message)}
                    </p>
                  ) : null}
                </CardContent>
              </Card>
            ))}
          </div>
        ) : (
          <Empty
            title="还没有录制会话"
            description="从直播间开始一段录制，或导入已有视频。"
            action={
              <Button asChild>
                <Link to="/rooms">管理直播间</Link>
              </Button>
            }
          />
        )}
      </QueryState>
    </Page>
  );
}

export function SchedulesPage() {
  const query = useApi("/api/schedules", 10000);
  const dashboard = useApi("/api/dashboard", 10000);
  const action = useAction();
  const cache = useQueryClient();
  const fields = [
    {
      name: "room_id",
      label: "直播间",
      required: true,
      type: "number" as const,
      options: rows(obj(dashboard.data).rooms).map((room) => ({
        label: text(room.uploader_name || room.title, `房间 #${room.id}`),
        value: number(room.id),
      })),
    },
    {
      name: "scheduled_at",
      label: "开始时间（当前设备时区）",
      required: true,
      type: "datetime-local" as const,
    },
    {
      name: "recurrent",
      label: "重复",
      options: [
        { label: "仅一次", value: "once" },
        { label: "每天", value: "daily" },
        { label: "每周", value: "weekly" },
      ],
    },
  ];
  return (
    <Page
      eyebrow="CAPTURE / SCHEDULES"
      title="录制预约"
      description="把开始时间提前安排好。对应直播间需要开启预约录制，服务也需保持运行。"
      actions={
        <>
          <Refresh refresh={query.refetch} />
          <FormDialog
            id="new-schedule"
            title="添加录制预约"
            trigger={
              <Button>
                <Plus className="size-4" />
                添加预约
              </Button>
            }
            fields={fields}
            initial={{ recurrent: "once" }}
            onSubmit={async (value) => {
              await send("/api/schedules", "POST", {
                room_id: Number(value.room_id),
                scheduled_at: new Date(
                  String(value.scheduled_at),
                ).toISOString(),
                recurrent: value.recurrent === "once" ? "" : value.recurrent,
              });
              await cache.invalidateQueries({ queryKey: ["api"] });
            }}
            submit="创建预约"
          />
        </>
      }
    >
      <QueryState query={query}>
        {rows(query.data).length ? (
          <div className="grid gap-4 md:grid-cols-2">
            {rows(query.data).map((schedule) => (
              <Card key={text(schedule.id)} className="gap-0 py-0 shadow-none">
                <CardContent className="p-5">
                  <div className="flex items-center gap-3">
                    <CalendarClock className="size-5 text-primary" />
                    <div className="flex-1">
                      <h2 className="text-sm font-semibold">
                        {text(
                          schedule.room_name || schedule.uploader_name,
                          `房间 #${schedule.room_id}`,
                        )}
                      </h2>
                      <p className="mt-2 text-xs text-muted-foreground">
                        {time(schedule.scheduled_at)}
                      </p>
                    </div>
                    <Status value={schedule.enabled ? "enabled" : "disabled"} />
                  </div>
                  <div className="mt-5 flex flex-wrap items-center gap-2 border-t pt-4">
                    <Badge variant="secondary">
                      {schedule.recurrent === "daily"
                        ? "每天"
                        : schedule.recurrent === "weekly"
                          ? "每周"
                          : "仅一次"}
                    </Badge>
                    {schedule.triggered ? (
                      <span className="text-xs text-muted-foreground">
                        已触发
                      </span>
                    ) : null}
                    <Button
                      size="sm"
                      variant="outline"
                      className="ml-auto"
                      disabled={action.isPending}
                      onClick={() =>
                        action.mutate({
                          path: `/api/schedules/${schedule.id}`,
                          method: "PATCH",
                          body: { enabled: !schedule.enabled },
                        })
                      }
                    >
                      {schedule.enabled ? "停用" : "启用"}
                    </Button>
                    <ConfirmButton
                      destructive
                      title="删除预约？"
                      description="已开始的录制不会因此被停止。"
                      onConfirm={() =>
                        action.mutateAsync({
                          path: `/api/schedules/${schedule.id}`,
                          method: "DELETE",
                        })
                      }
                    >
                      删除
                    </ConfirmButton>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        ) : (
          <Empty
            title="还没有录制预约"
            description="添加一个开始时间，让工作台按计划守候直播。"
          />
        )}
      </QueryState>
    </Page>
  );
}

export function TranscriptsPage() {
  const library = useLibraryState("transcripts");
  const { query, search } = library;
  const action = useAction();
  const cache = useQueryClient();
  const [preview, setPreview] = useState<number | null>(null);
  useEffect(() => {
    setPreview(null);
  }, [library.session, library.unknown, library.offset, search]);
  const transcripts = rows(query.data).slice(0, LIBRARY_PAGE_SIZE);
  return (
    <Page
      eyebrow="ANALYZE / TRANSCRIPTS"
      title="语音转写"
      description="检查真实转写结果，校正专有名词，必要时为原始分段重新转写。"
      actions={library.scoped ? <Refresh refresh={query.refetch} /> : undefined}
    >
      <div className="flex flex-wrap gap-3">
        <Input
          className="max-w-sm bg-card"
          placeholder="搜索转写内容…"
          value={search}
          onChange={(event) => {
            setPreview(null);
            library.update({ q: event.target.value });
          }}
        />
      </div>
      <LibraryBrowser library={library}>
        <QueryState query={query}>
          {transcripts.length ? (
            <div className="space-y-4">
              {transcripts.map((entry) => {
                const id = number(entry.id);
                return (
                  <Card key={id} className="gap-0 py-0 shadow-none">
                    <CardContent className="p-5">
                      <div className="mb-4 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                        <FileText className="size-4 text-primary" />
                        <span>
                          {entry.session_id == null
                            ? "未关联场次"
                            : `场次 #${entry.session_id}`}{" "}
                          · 分段
                          {entry.segment_seq == null
                            ? "编号未知"
                            : ` #${number(entry.segment_seq) + 1}`}
                        </span>
                        <Badge variant="outline">
                          {text(entry.backend || entry.primary_backend, "ASR")}
                        </Badge>
                        {entry.language ? (
                          <Badge variant="outline">
                            {text(entry.language)}
                          </Badge>
                        ) : null}
                        <span className="ml-auto">
                          {time(entry.created_at)}
                        </span>
                      </div>
                      {entry.source_file_name ? (
                        <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                          <span>源文件：{text(entry.source_file_name)}</span>
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() =>
                              void copyText(text(entry.source_file_name))
                                .then(() => toast.success("文件名已复制"))
                                .catch(() => toast.error("无法访问剪贴板"))
                            }
                          >
                            复制文件名
                          </Button>
                          <span>
                            {entry.source_mp4_available
                              ? "支持无损导出 MP4"
                              : "当前格式不支持 TS 转 MP4 导出"}
                          </span>
                        </div>
                      ) : null}
                      <p className="whitespace-pre-wrap text-sm leading-8">
                        {text(
                          entry.corrected_text ||
                            entry.refined_text ||
                            entry.text,
                          "当前分段没有可用语音文本",
                        )}
                      </p>
                      {entry.refine_summary ? (
                        <p className="mt-4 rounded-xl bg-primary/5 p-4 text-xs leading-6 text-muted-foreground">
                          {text(entry.refine_summary)}
                        </p>
                      ) : null}
                      <div className="mt-4 flex flex-wrap items-center gap-2 border-t pt-4">
                        <FormDialog
                          id={`transcript-${id}`}
                          title="校正转写"
                          description="原始 ASR 保留。校正会让旧的整理结果失效，重新分析前请确认文本。"
                          trigger={
                            <Button size="sm" variant="outline">
                              校正文本
                            </Button>
                          }
                          fields={[
                            {
                              name: "corrected_text",
                              label: "校正后的文本",
                              type: "textarea",
                              required: true,
                            },
                            {
                              name: "aliases",
                              label: "学习别名映射",
                              type: "json",
                            },
                            {
                              name: "learn_dictionary",
                              label: "保存到房间词典",
                              type: "boolean",
                            },
                          ]}
                          initial={{
                            corrected_text:
                              entry.corrected_text || entry.text || "",
                            aliases: {},
                            learn_dictionary: true,
                          }}
                          onSubmit={async (body) => {
                            await send(`/api/transcripts/${id}`, "PATCH", body);
                            await cache.invalidateQueries({
                              queryKey: ["api"],
                            });
                          }}
                        />
                        <ConfirmButton
                          title="重新转写这个分段？"
                          description="会提交后台转写任务，原始媒体保留。模型与依赖需要已准备好。"
                          onConfirm={() =>
                            action.mutateAsync({
                              path: `/api/transcripts/${id}/retranscribe`,
                              success: "重新转写任务已提交",
                            })
                          }
                        >
                          <RefreshCw className="size-3" />
                          重新转写
                        </ConfirmButton>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={!entry.source_mp4_available}
                          onClick={() => setPreview(preview === id ? null : id)}
                        >
                          <Play className="size-3" />
                          {preview === id ? "收起原片" : "查看原片"}
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={!entry.source_mp4_available}
                          onClick={() =>
                            void download(
                              `/api/transcripts/${id}/source-mp4`,
                              `session_${text(entry.session_id, "unassigned")}_segment_${entry.segment_seq == null ? "unknown" : number(entry.segment_seq) + 1}_${entry.segment_id}.mp4`,
                            ).catch((error: Error) =>
                              toast.error(error.message),
                            )
                          }
                        >
                          <Download className="size-3" />
                          下载原片
                        </Button>
                      </div>
                      {preview === id && (
                        <video
                          className="mt-4 aspect-video w-full rounded-xl bg-black"
                          controls
                          preload="metadata"
                          src={`/api/transcripts/${id}/source-mp4`}
                        />
                      )}
                    </CardContent>
                  </Card>
                );
              })}
            </div>
          ) : (
            <Empty
              title={search ? "没有匹配的转写" : "还没有转写内容"}
              description="录制或导入媒体后，启用分析并准备语音模型，结果将在这里更新。"
            />
          )}
        </QueryState>
      </LibraryBrowser>
    </Page>
  );
}

export function DanmakuPage() {
  const [session, setSession] = useState("");
  const query = useApi(
    `/api/danmaku?limit=200${session ? `&session_id=${session}` : ""}`,
  );
  const data = obj(query.data);
  return (
    <Page
      eyebrow="ANALYZE / AUDIENCE"
      title="弹幕热度"
      description="把观众的实时反应放回内容上下文。无法采集与真实的零事件会分别显示。"
      actions={<Refresh refresh={query.refetch} />}
    >
      <SessionPicker value={session} onChange={setSession} />
      <QueryState query={query}>
        <div className="grid gap-4 md:grid-cols-3">
          {rows(data.sessions).map((entry) => (
            <Card
              key={text(entry.session_id)}
              className="gap-0 py-0 shadow-none"
            >
              <CardContent className="space-y-4 p-5">
                <div className="flex justify-between">
                  <h2 className="text-sm font-semibold">
                    场次 #{text(entry.session_id)}
                  </h2>
                  <Status value={obj(entry.evidence).status || "unavailable"} />
                </div>
                <DetailList
                  values={[
                    {
                      label: "事件数量",
                      value:
                        entry.count === null ? "不可用" : text(entry.count),
                    },
                    {
                      label: "加权热度",
                      value:
                        entry.intensity === null
                          ? "不可用"
                          : text(entry.intensity),
                    },
                  ]}
                />
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    void download(
                      `/api/sessions/${entry.session_id}/danmaku-archive`,
                      `danmaku-${entry.session_id}.jsonl`,
                    ).catch((error: Error) => toast.error(error.message))
                  }
                >
                  <Download className="size-3" />
                  导出弹幕归档
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
        {rows(data.recent).length ? (
          <Card className="gap-0 py-0 shadow-none">
            <CardContent className="divide-y px-5">
              {rows(data.recent).map((entry, index) => (
                <div key={index} className="flex gap-4 py-3 text-sm">
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {time(entry.ts)}
                  </span>
                  <span className="max-w-32 truncate font-medium text-primary">
                    {text(entry.user, "观众")}
                  </span>
                  <p className="min-w-0 flex-1 break-words">
                    {text(entry.content)}
                  </p>
                  <Badge variant="outline">{text(entry.type, "弹幕")}</Badge>
                </div>
              ))}
            </CardContent>
          </Card>
        ) : (
          <Empty
            title="暂时没有弹幕事件"
            description={
              data.available
                ? "当前采集窗口内还没有事件。"
                : "当前来源没有可用的弹幕采集证据；这不等同于观众互动为零。"
            }
          />
        )}
      </QueryState>
    </Page>
  );
}

export function ClipsPage() {
  const [folder, setFolder] = useState<string | null>(null);
  const library = useLibraryState("clips");
  const { query, search, status: filter } = library;
  const action = useAction();
  const clips = rows(query.data).slice(0, LIBRARY_PAGE_SIZE);
  return (
    <Page
      eyebrow="PRODUCE / LIBRARY"
      title="成品切片"
      description="预览已经生成的视频，核对字幕、封面和文案，再决定发布方式。"
      actions={
        <>
          {library.scoped && <Refresh refresh={query.refetch} />}
          <Button
            variant="outline"
            disabled={action.isPending}
            onClick={() =>
              void action
                .mutateAsync({
                  path: `/api/open-clips-dir${library.unknown ? "?unknown_session=true" : library.session ? `?session_id=${encodeURIComponent(library.session)}` : ""}`,
                  success: "已读取服务端成品目录",
                })
                .then((result) => setFolder(text(obj(result).clips_dir)))
                .catch(() => {})
            }
          >
            打开成品目录
          </Button>
        </>
      }
    >
      <div className="flex flex-wrap gap-3">
        <Input
          className="max-w-sm bg-card"
          placeholder="搜索成品标题或简介…"
          value={search}
          onChange={(event) => library.update({ q: event.target.value })}
        />
        <select
          aria-label="成品状态"
          className="native-select w-36"
          value={filter}
          onChange={(event) => library.update({ status: event.target.value })}
        >
          <option value="all">全部状态</option>
          <option value="generated">已生成</option>
          <option value="ready">待发布</option>
          <option value="published">已发布</option>
          <option value="reviewing">审核中</option>
        </select>
      </div>
      <LibraryBrowser library={library}>
        <QueryState query={query}>
          {clips.length ? (
            <div className="grid gap-6 md:grid-cols-2 xl:grid-cols-3">
              {clips.map((clip) => (
                <Card
                  key={text(clip.id)}
                  className="gap-0 overflow-hidden py-0 shadow-none"
                >
                  <video
                    controls
                    preload="none"
                    className="aspect-video w-full bg-slate-950"
                    poster={`/api/clips/${clip.id}/cover`}
                    src={`/api/clips/${clip.id}/video`}
                  />
                  <CardContent className="space-y-4 p-5">
                    <div className="flex items-start justify-between gap-3">
                      <h2 className="line-clamp-2 text-sm leading-6 font-semibold">
                        {title(clip)}
                      </h2>
                      <Status value={clip.status} />
                    </div>
                    <p className="line-clamp-3 text-xs leading-6 text-muted-foreground">
                      {text(clip.description, "暂无简介")}
                    </p>
                    <div className="flex flex-wrap gap-1.5">
                      {Array.isArray(clip.tags)
                        ? clip.tags.map((tag) => (
                            <Badge
                              key={String(tag)}
                              variant="secondary"
                              className="text-[10px]"
                            >
                              #{text(tag)}
                            </Badge>
                          ))
                        : null}
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {duration(clip.duration_s)} ·{" "}
                      {clip.display_number == null
                        ? "未关联场次"
                        : `本场成品 #${clip.display_number}`}
                    </p>
                    <div className="flex flex-wrap gap-2 border-t pt-4">
                      <ConfirmButton
                        title="发布这个切片？"
                        description="上传关闭时标记就绪并导出清单；已启用上传时会执行投稿。请先核对成品与当前上传设置。"
                        disabled={action.isPending}
                        onConfirm={() =>
                          action.mutateAsync({
                            path: `/api/clips/${clip.id}/publish`,
                            success: "发布请求已处理，请到上传页面核对结果",
                          })
                        }
                      >
                        <Check className="size-3" />
                        发布 / 标记就绪
                      </ConfirmButton>
                      <ConfirmButton
                        title="将切片加入上传队列？"
                        description="服务端将按上传开关、频率及素材校验决定是否执行。平台结果未知时请先核对投稿记录。"
                        onConfirm={() =>
                          action.mutateAsync({
                            path: `/api/clips/${clip.id}/enqueue`,
                            success: "上传作业已提交",
                          })
                        }
                      >
                        <Send className="size-3" />
                        上传
                      </ConfirmButton>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          void download(
                            `/api/clips/${clip.id}/video`,
                            `session_${text(clip.session_id, "unassigned")}_clip_${text(clip.display_number, "unknown")}_${clip.id}.mp4`,
                          ).catch((error: Error) => toast.error(error.message))
                        }
                      >
                        <Download className="size-3" />
                        下载
                      </Button>
                      <ConfirmButton
                        destructive
                        title="拒绝这个成品？"
                        description="将成品标记为拒绝。"
                        onConfirm={() =>
                          action.mutateAsync({
                            path: `/api/clips/${clip.id}/reject`,
                          })
                        }
                      >
                        拒绝
                      </ConfirmButton>
                      <FormDialog
                        id={`manual-upload-${clip.id}`}
                        title="确认手动投稿结果"
                        description="仅在已完成平台投稿并核对结果后确认。"
                        trigger={
                          <Button size="sm" variant="ghost">
                            已手动投稿
                          </Button>
                        }
                        fields={[
                          { name: "platform", label: "投稿平台" },
                          {
                            name: "submission_id",
                            label: "稿件号（如 BV 号）",
                          },
                          { name: "published_url", label: "已发布链接" },
                        ]}
                        onSubmit={(body) =>
                          action.mutateAsync({
                            path: `/api/clips/${clip.id}/confirm-manual-upload?${new URLSearchParams(Object.entries(body).map(([key, value]) => [key, String(value)]))}`,
                          })
                        }
                        submit="确认投稿结果"
                      />
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          ) : (
            <Empty
              title="成品库还是空的"
              description="在人工审片中确认候选并渲染，生成的视频会出现在这里。"
              action={
                <Button asChild>
                  <Link to="/review">前往人工审片</Link>
                </Button>
              }
            />
          )}
        </QueryState>
      </LibraryBrowser>
      <Dialog
        open={folder !== null}
        onOpenChange={(next) => {
          if (!next) setFolder(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>服务端成品目录</DialogTitle>
            <DialogDescription>
              已在服务端尝试打开目录。无头服务器可复制路径，通过文件工具访问。
            </DialogDescription>
          </DialogHeader>
          <p className="break-all rounded-xl bg-muted p-4 font-mono text-xs">
            {folder}
          </p>
          <Button
            variant="outline"
            onClick={() =>
              void copyText(folder || "")
                .then(() => toast.success("路径已复制"))
                .catch(() => toast.error("无法访问剪贴板，请手动复制路径"))
            }
          >
            复制目录路径
          </Button>
        </DialogContent>
      </Dialog>
    </Page>
  );
}
