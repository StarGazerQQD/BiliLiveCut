import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import {
  Check,
  LayoutGrid,
  List,
  MoreHorizontal,
  Play,
  Plus,
  Radio,
  Settings2,
  Square,
  Trash2,
  WandSparkles,
} from "lucide-react";
import { useAction, useApi, send, type Row, type Schemas } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Empty,
  ConfirmButton,
  FormDialog,
  Page,
  QueryState,
  Refresh,
  Status,
  type FieldSpec,
} from "@/components/workbench";
import { number, obj, rows, text, title } from "@/lib/data";
import { RoomDanmakuSettings } from "@/components/room-danmaku-settings";

export function AddRoom() {
  const query = useQueryClient();
  const sources = useApi("/api/live-sources", false);
  const available = rows(sources.data).length
    ? rows(sources.data)
    : rows(obj(sources.data).sources);
  return (
    <FormDialog
      id="add-room"
      title="添加直播间"
      description="输入直播间地址，选择来源并确认你拥有录制授权。"
      trigger={
        <Button>
          <Plus className="size-4" />
          添加直播间
        </Button>
      }
      fields={[
        {
          name: "url",
          label: "直播间地址或 Bilibili 房间号",
          required: true,
          placeholder: "https://live.bilibili.com/…",
        },
        {
          name: "platform",
          label: "直播来源",
          options: [
            { label: "按地址自动识别", value: "auto" },
            ...available.map((entry) => ({
              label: text(entry.name || entry.platform),
              value: text(entry.platform),
            })),
          ],
        },
        { name: "authorized", label: "我已确认拥有录制授权", type: "boolean" },
      ]}
      initial={{ platform: "auto", authorized: false }}
      onSubmit={async (value) => {
        const body: Schemas["AddRoomRequest"] = {
          url: String(value.url),
          authorized: !!value.authorized,
          ...(value.platform !== "auto"
            ? { platform: String(value.platform) }
            : {}),
        };
        await send("/api/rooms", "POST", body);
        await query.invalidateQueries({ queryKey: ["api"] });
      }}
      submit="添加并解析直播间"
    />
  );
}

const roomFields: FieldSpec[] = [
  { name: "authorized", label: "已确认录制授权", type: "boolean" },
  ...[
    ["auto_record", "开播后自动录制"],
    ["auto_analyze", "自动转写与分析"],
    ["auto_render", "自动渲染"],
    ["auto_approve", "自动审核"],
    ["auto_upload", "自动上传"],
    ["schedule_enabled", "允许预约录制"],
    ["auto_threshold_enabled", "阈值自学习"],
    ["danmaku_sentiment_enabled", "弹幕情绪分析"],
  ].map(([name, label]): FieldSpec => ({ name, label, type: "boolean" })),
  ...[
    ["highlight_threshold", "高光入选阈值"],
    ["review_threshold", "进入人工审核阈值"],
    ["auto_approve_threshold", "自动通过阈值"],
    ["auto_publish_threshold", "自动投稿阈值"],
  ].map(([name, label]): FieldSpec => ({
    name,
    label,
    type: "number",
    min: 0,
    max: 1,
    step: 0.01,
    required: true,
  })),
];

export function RoomCard({
  room,
  compact = false,
}: {
  room: Row;
  compact?: boolean;
}) {
  const action = useAction();
  const query = useQueryClient();
  const id = number(room.id);
  const running = !!room.running;
  const monitor = obj(room.monitor);
  const metadata = obj(room.metadata);
  const config = obj(room.room_config);
  const invalidate = () => {
    void query.invalidateQueries({ queryKey: ["api"] });
  };
  const roomState =
    room.source_available === false && !running
      ? "unavailable"
      : !running &&
          ["cooldown", "action_required", "manual_paused"].includes(
            text(monitor.state),
          )
        ? monitor.state
        : room.recording_state || (running ? "recording" : "idle");
  const eventState =
    monitor.state === "manual_paused"
      ? "已暂停"
      : ["stopped", "force_stopped"].includes(text(monitor.state))
        ? "已停止"
        : monitor.service_running === false
          ? "服务未运行"
          : monitor.connection_state === "available"
            ? "监听中"
            : monitor.connection_state === "failed"
              ? "正在重连"
              : "等待连接";
  return (
    <Card className="room-card gap-0 overflow-hidden py-0 shadow-none">
      <CardContent className="p-5">
        <div className="mb-5 flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-center gap-3">
            <span
              className={`room-avatar ${running ? "room-avatar-live" : ""}`}
            >
              <Radio className="size-5" />
            </span>
            <div className="min-w-0">
              <h3 className="truncate font-semibold">
                {text(room.uploader_name || room.source_name, `直播间 #${id}`)}
              </h3>
              <p className="mt-1 text-xs text-muted-foreground">
                {text(room.platform, "bilibili")}{" "}
                <span className="mx-1.5 text-border">/</span>
                {text(room.source_id || room.room_id, `#${id}`)}
              </p>
            </div>
          </div>
          <Status value={roomState} />
        </div>
        <p className="mb-4 line-clamp-2 min-h-10 text-sm leading-5 text-muted-foreground">
          {text(room.title || metadata.last_title, "尚未读取到直播标题")}
        </p>
        <div className="mb-5 flex flex-wrap gap-1.5">
          {[
            ["录制", room.auto_record],
            ["分析", room.auto_analyze],
            ["渲染", room.auto_render],
            ["审核", room.auto_approve],
            ["上传", room.auto_upload],
          ].map(([label, enabled]) => (
            <span
              key={String(label)}
              className={`automation-chip ${enabled ? "automation-chip-on" : ""}`}
            >
              {Boolean(enabled) && <Check className="size-2.5" />}
              {String(label)}
            </span>
          ))}
        </div>
        {!running &&
          !!room.auto_record &&
          monitor.detection_mode === "events" && (
            <p className="mb-3 text-xs text-muted-foreground">
              开播事件 · {eventState}
            </p>
          )}
        {monitor.state === "cooldown" && (
          <p className="mb-3 text-xs text-muted-foreground">
            {number(monitor.retry_in_s) > 0
              ? `约 ${Math.ceil(number(monitor.retry_in_s))} 秒后复核直播状态并尝试恢复`
              : "正在复核直播状态，确认仍在直播后恢复录制"}
          </p>
        )}
        {Boolean(
          monitor.error ||
          monitor.message ||
          room.recording_message ||
          room.source_error,
        ) && (
          <p className="mb-4 text-xs leading-5 text-amber-700 dark:text-amber-400">
            {text(
              monitor.error ||
                monitor.message ||
                room.recording_message ||
                room.source_error,
            )}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-2 border-t pt-4">
          {running ? (
            <>
              <FormDialog
                id={`stop-${id}`}
                title="停止录制"
                description="优雅停止会完成当前片段；强制停止立即中断。可单独选择是否取消后续任务。"
                trigger={
                  <Button size="sm" variant="outline">
                    <Square className="size-3" />
                    停止
                  </Button>
                }
                fields={[
                  {
                    name: "mode",
                    label: "停止方式",
                    options: [
                      { label: "优雅停止", value: "graceful" },
                      { label: "强制停止", value: "force" },
                    ],
                  },
                  {
                    name: "cancel_pending",
                    label: "同时取消尚未完成的分析任务",
                    type: "boolean",
                  },
                ]}
                initial={{ mode: "graceful", cancel_pending: false }}
                onSubmit={(body) =>
                  action.mutateAsync({
                    path: `/api/rooms/${id}/stop`,
                    body,
                    success: "停止请求已提交，正在收尾",
                  })
                }
                submit="确认停止"
              />
              <FormDialog
                id={`marker-${id}`}
                title="标记当前高光"
                trigger={
                  <Button size="sm" variant="outline">
                    <WandSparkles className="size-3" />
                    打点
                  </Button>
                }
                fields={[
                  {
                    name: "pre_roll_s",
                    label: "保留此前（秒）",
                    type: "number",
                    min: 0,
                    max: 300,
                    required: true,
                  },
                  {
                    name: "post_roll_s",
                    label: "保留此后（秒）",
                    type: "number",
                    min: 2,
                    max: 300,
                    required: true,
                  },
                  { name: "note", label: "高光备注" },
                ]}
                initial={{ pre_roll_s: 60, post_roll_s: 40, note: "" }}
                onSubmit={(body) =>
                  action.mutateAsync({
                    path: `/api/rooms/${id}/markers`,
                    body,
                    success: "高光打点已登记",
                  })
                }
                submit="标记高光"
              />
            </>
          ) : (
            <FormDialog
              id={`start-${id}`}
              title="开始录制"
              description="选择是否同时转写分析，以及在高光通过后生成成品。"
              trigger={
                <Button
                  size="sm"
                  disabled={room.source_available === false || !room.authorized}
                >
                  <Play className="size-3" />
                  开始录制
                </Button>
              }
              fields={[
                {
                  name: "pipeline",
                  label: "同时运行转写与高光分析",
                  type: "boolean",
                },
                {
                  name: "produce",
                  label: "高光通过后自动渲染",
                  type: "boolean",
                },
              ]}
              initial={{
                pipeline: !!room.auto_analyze,
                produce: !!room.auto_render,
              }}
              onSubmit={(body) =>
                action.mutateAsync({
                  path: `/api/rooms/${id}/start`,
                  body,
                  success: "启动请求已接受，请留意录制状态",
                })
              }
              submit="开始录制"
            />
          )}
          {!compact && (
            <>
              {(room.platform || "bilibili") === "bilibili" && (
                <RoomDanmakuSettings room={room} />
              )}
              <FormDialog
                id={`room-${id}`}
                title="直播间设置"
                description={
                  running
                    ? "部分开关在录制期间锁定；服务端会保留原有校验。"
                    : "保存本房间的自动化开关与评分阈值。"
                }
                trigger={
                  <Button size="sm" variant="outline">
                    <Settings2 className="size-3" />
                    设置
                  </Button>
                }
                fields={roomFields.map((field) => ({
                  ...field,
                  disabled:
                    running &&
                    [
                      "schedule_enabled",
                      "auto_threshold_enabled",
                      "danmaku_sentiment_enabled",
                    ].includes(field.name),
                  description:
                    running &&
                    [
                      "schedule_enabled",
                      "auto_threshold_enabled",
                      "danmaku_sentiment_enabled",
                    ].includes(field.name)
                      ? "录制期间不可修改，其他设置仍可保存。"
                      : field.description,
                }))}
                initial={room}
                onSubmit={async (body) => {
                  await send(`/api/rooms/${id}`, "PATCH", body);
                  invalidate();
                }}
              />
              <FormDialog
                id={`room-words-${id}`}
                title="房间词典与高光偏好"
                trigger={
                  <Button size="sm" variant="ghost">
                    词典
                  </Button>
                }
                fields={[
                  {
                    name: "hotwords",
                    label: "热词",
                    type: "lines",
                    description: "每行一个，帮助转写识别人名与专有名词。",
                  },
                  {
                    name: "aliases",
                    label: "别名映射",
                    type: "json",
                    description: '使用键值映射，例如 {"原称呼":"标准称呼"}。',
                  },
                  {
                    name: "highlight_keywords",
                    label: "高光关键词",
                    type: "lines",
                  },
                  { name: "blocked_topics", label: "屏蔽主题", type: "lines" },
                  {
                    name: "highlight_scorer_mode",
                    label: "高光评分方式",
                    options: [
                      { label: "继承全局配置", value: "inherit" },
                      { label: "关闭插件评分", value: "off" },
                      { label: "影子评分", value: "shadow" },
                      { label: "主评分器", value: "champion" },
                    ],
                  },
                ]}
                initial={{
                  ...config,
                  highlight_scorer_mode:
                    config.highlight_scorer_mode || "inherit",
                }}
                onSubmit={async (body) => {
                  await send(`/api/rooms/${id}`, "PATCH", {
                    room_config: body,
                  });
                  invalidate();
                }}
              />
            </>
          )}
          <ConfirmButton
            title="删除直播间"
            description={`确认删除「${text(room.uploader_name || room.source_name, `直播间 #${id}`)}」？删除后停止开播守候并取消录制预约，已有录制、弹幕和切片仍会保留。重新添加同一房间可继续查看历史记录。`}
            confirm="确认删除"
            destructive
            disabled={running}
            onConfirm={() =>
              action.mutateAsync({
                path: `/api/rooms/${id}`,
                method: "DELETE",
                success: "直播间已删除，录制历史已保留",
              })
            }
          >
            <Trash2 className="size-3" />
            删除
          </ConfirmButton>
          {running && (
            <span className="text-xs text-muted-foreground">
              停止录制后可删除
            </span>
          )}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                size="icon"
                variant="ghost"
                className="ml-auto size-8"
                aria-label="更多直播间操作"
              >
                <MoreHorizontal className="size-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {!running && (
                <DropdownMenuItem
                  onClick={() =>
                    action.mutate({
                      path: `/api/rooms/${id}/arm-auto`,
                      success: "已恢复开播守候",
                    })
                  }
                >
                  恢复开播守候
                </DropdownMenuItem>
              )}
              <DropdownMenuItem
                onClick={() =>
                  action.mutate({
                    path: `/api/rooms/${id}/pause`,
                    success: "已暂停本房间的自动录制",
                  })
                }
              >
                暂停自动录制
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() =>
                  action.mutate({
                    path: `/api/rooms/${id}/resume`,
                    success: "继续录制请求已提交",
                  })
                }
              >
                继续录制
              </DropdownMenuItem>
              <DropdownMenuItem asChild>
                <Link to={`/sessions?room=${id}`}>查看场次时间线</Link>
              </DropdownMenuItem>
              {Boolean(room.auto_threshold_enabled) && (
                <DropdownMenuItem asChild>
                  <Link to={`/settings/automation?room=${id}`}>
                    查看阈值学习
                  </Link>
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </CardContent>
    </Card>
  );
}

export default function RoomsPage() {
  const query = useApi("/api/dashboard");
  const [search, setSearch] = useState("");
  const [grid, setGrid] = useState(true);
  const all = rows(obj(query.data).rooms);
  const filtered = all.filter((room) =>
    [title(room), room.uploader_name, room.source_id, room.room_id]
      .join(" ")
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  return (
    <Page
      eyebrow="CAPTURE / LIVE SOURCES"
      title="直播间"
      description="管理直播来源与录制节奏。把重复的工作交给自动化，把注意力留给内容。"
      actions={
        <>
          <Refresh refresh={query.refetch} busy={query.isFetching} />
          <AddRoom />
        </>
      }
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Input
          className="max-w-xs bg-card"
          placeholder="搜索主播、房间号或标题…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <div className="flex items-center gap-3">
          <span className="text-xs text-muted-foreground">
            {all.length} 个直播间 · {all.filter((room) => room.running).length}{" "}
            个正在录制
          </span>
          <div className="flex rounded-lg border bg-card p-0.5">
            <Button
              aria-label="卡片视图"
              size="icon"
              variant={grid ? "secondary" : "ghost"}
              className="size-7"
              onClick={() => setGrid(true)}
            >
              <LayoutGrid className="size-3.5" />
            </Button>
            <Button
              aria-label="列表视图"
              size="icon"
              variant={!grid ? "secondary" : "ghost"}
              className="size-7"
              onClick={() => setGrid(false)}
            >
              <List className="size-3.5" />
            </Button>
          </div>
        </div>
      </div>
      <QueryState query={query}>
        {filtered.length ? (
          <div
            className={
              grid ? "grid gap-5 md:grid-cols-2 2xl:grid-cols-3" : "grid gap-4"
            }
          >
            {filtered.map((room) => (
              <RoomCard key={text(room.id)} room={room} />
            ))}
          </div>
        ) : (
          <Empty
            title={search ? "没有匹配的直播间" : "从第一个直播间开始"}
            description={
              search
                ? "试试其他关键词，或清除搜索条件。"
                : "添加已获授权的直播间，即可开始录制，也可以设置开播后自动分析。"
            }
            action={!search && <AddRoom />}
          />
        )}
      </QueryState>
    </Page>
  );
}
