import { Link, useSearchParams } from "react-router";
import { useLayoutEffect, useRef, useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronRight,
  LoaderCircle,
  RotateCcw,
  Save,
  Search,
  SlidersHorizontal,
} from "lucide-react";
import { toast } from "sonner";
import { ApiError, send, useApi, type Row } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { StorageRebuild } from "@/components/storage-rebuild";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  ConfirmButton,
  FormField,
  Page,
  QueryState,
} from "@/components/workbench";
import {
  SchemaFields,
  fieldSpec,
  normalizeSchema,
} from "@/components/schema-fields";
import { useDraft } from "@/lib/drafts";
import { obj, rows, text } from "@/lib/data";
import { cn } from "@/lib/utils";
import { settingsTopic } from "@/features/settings-catalog";
import {
  ConfigurationStatus,
  configurationSaveSummary,
} from "@/components/configuration-status";

export const settingGroups: Record<string, string> = {
  recording: "录制与自动化",
  asr: "语音转写",
  llm: "大模型与预算",
  trends: "网感采集",
  highlights: "高光与评分",
  review: "审核",
  output: "成片与编码",
  storage: "存储与清理",
  publishing: "上传与发布",
  notifications: "提醒与通知",
  system: "账号与系统",
};
const common = new Set(
  "recording_pipeline_enabled segment_duration_s stream_quality live_poll_interval_s recording_max_duration_s asr_primary asr_remote_base_url asr_remote_api_key asr_remote_model asr_remote_language asr_remote_response_format asr_remote_timeout_s asr_remote_chunk_s asr_task_max_concurrency transcript_llm_refine_enabled llm_daily_budget trend_enabled trend_schedule_enabled trend_schedule_start trend_schedule_end trend_schedule_interval_min highlight_threshold highlight_review_threshold highlight_auto_approve_threshold clip_subtitle clip_danmaku clip_danmaku_mode clip_danmaku_font_size clip_danmaku_color clip_danmaku_opacity clip_danmaku_duration_s clip_danmaku_max_lines clip_max_duration_s clip_video_crf clip_preset clip_video_encoder clip_hardware_quality clip_hardware_preset clip_loudnorm clip_remove_silence clip_vertical min_free_disk_gb storage_cleanup_enabled raw_retention_days biliup_enabled auto_upload auto_publish_threshold bilibili_cookie notify_on_clip notify_on_error notify_on_disk_alert".split(
    " ",
  ),
);
common.add("asr_remote_route_id");
common.add("storage_raw_dir");
common.add("storage_danmaku_dir");
const effects: Record<string, string> = {
  new_room: "新建房间时生效",
  next_task: "下一任务生效",
  next_recording: "下次录制生效",
  next_poll: "下次调度生效",
  next_shutdown: "下次停止时生效",
  next_claim: "下次领取审核时生效",
  immediate: "下一次读取生效",
  restart: "重启后生效",
  deployment: "启动配置",
};
type Change = { action: "value" | "clear" | "reset"; value?: unknown };

export default function ConfigurationPage() {
  const query = useApi("/api/settings/configuration", false);
  const modelQuery = useApi("/api/model-configuration", false);
  const cache = useQueryClient();
  const draft = useDraft<Row>("configuration", {});
  const [params, setParams] = useSearchParams();
  const [advanced, setAdvanced] = useState(!!params.get("q"));
  const group = params.get("group") || "all";
  function setGroup(value: string) {
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set("group", value);
      return next;
    });
  }
  const previousGroup = useRef(group);
  const [search, setSearch] = useState(params.get("q") || "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const view = draft.dirty ? obj(draft.value.view) : obj(query.data);
  const changes = obj(draft.value.changes) as Record<string, Change>;
  const eligible = rows(view.fields).filter(
    (field) => advanced || common.has(String(field.key)),
  );
  const visible = eligible.filter(
    (field) =>
      (group === "all" || !!search.trim() || field.group === group) &&
      [field.label, field.key, field.env, field.description]
        .join(" ")
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  const sections = Array.from(
    new Set(
      visible.map(
        (field) =>
          `${field.group}:${settingsTopic(String(field.key), String(field.group))}`,
      ),
    ),
  ).map((id) => {
    const [category, topic] = id.split(":");
    return {
      id,
      category,
      topic,
      fields: visible.filter(
        (field) =>
          field.group === category &&
          settingsTopic(String(field.key), category) === topic,
      ),
    };
  });
  useLayoutEffect(() => {
    if (previousGroup.current === group) return;
    previousGroup.current = group;
    window.scrollTo({ top: 0, left: 0, behavior: "instant" });
  }, [group]);
  function change(key: string, next: Change) {
    setError("");
    draft.update({ view, changes: { ...changes, [key]: next } });
  }
  async function reload() {
    const result = await query.refetch();
    if (result.data && draft.dirty) {
      draft.update({ view: result.data, changes });
      setConflict(false);
      toast.info("已读取最新配置；草稿已保留，请核对当前值后再保存");
    }
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!Object.keys(changes).length || saving) return;
    setSaving(true);
    setError("");
    try {
      const values: Row = {};
      const clear: string[] = [];
      const reset: string[] = [];
      for (const [key, entry] of Object.entries(changes)) {
        if (entry.action === "clear") clear.push(key);
        else if (entry.action === "reset") reset.push(key);
        else {
          const field = rows(view.fields).find((item) => item.key === key);
          const value = entry.value;
          if (field && (field.type === "integer" || field.type === "number")) {
            if (value === "" || !Number.isFinite(Number(value)))
              throw new Error(`${field.label}必须是有效数字`);
            values[key] = Number(value);
          } else if (field?.secret && value === "") continue;
          else
            values[key] = field?.schema
              ? normalizeSchema(obj(field.schema), value)
              : value;
        }
      }
      const saved = obj(
        await send("/api/settings/configuration", "PATCH", {
          revision: view.revision,
          values,
          clear,
          reset,
        }),
      );
      draft.reset();
      setConflict(false);
      await cache.invalidateQueries({ queryKey: ["api"] });
      const requested = new Set([...Object.keys(values), ...clear, ...reset]);
      toast.success(
        configurationSaveSummary(
          rows(saved.fields).filter((field) =>
            requested.has(String(field.key)),
          ),
        ),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
      setConflict(reason instanceof ApiError && reason.status === 409);
    } finally {
      setSaving(false);
    }
  }
  return (
    <Page
      eyebrow="PREFERENCES / CONFIGURATION"
      title="设置中心"
      description="先调整常用选项，再按需展开高级设置。每项参数都标明来源与生效时间。"
      actions={
        <Button
          variant="outline"
          onClick={() => void reload()}
          disabled={query.isFetching || saving}
        >
          <RotateCcw className="size-3.5" />
          读取最新配置
        </Button>
      }
    >
      <div className="settings-links">
        {[
          ["/settings/automation", "房间自动化与端口"],
          ["/settings/llm", "大模型服务商"],
          ["/settings/login", "账号登录"],
          ["/settings/notifications", "提醒与通知"],
          ["/settings/plugins", "插件"],
          ["/settings/subtitles", "字幕模板"],
          ["/settings/intros", "片头片尾"],
        ].map(([href, label]) => (
          <Link to={href} key={href}>
            {label}
            <ChevronRight className="size-3" />
          </Link>
        ))}
      </div>
      <QueryState query={query}>
        <div className="grid items-start gap-6 xl:grid-cols-[205px_1fr]">
          <aside className="settings-index hidden xl:block">
            <p className="mb-3 px-3 text-[10px] font-semibold tracking-wider text-muted-foreground">
              配置目录
            </p>
            {[["all", "全部参数"], ...Object.entries(settingGroups)].map(
              ([key, label]) => {
                const count = eligible.filter(
                  (field) => key === "all" || field.group === key,
                ).length;
                return (
                  <button
                    type="button"
                    key={key}
                    className={cn(
                      "settings-index-item",
                      group === key && "active",
                    )}
                    onClick={() => setGroup(key)}
                  >
                    <span>{label}</span>
                    <span className="text-xs tabular-nums opacity-60">
                      {count}
                    </span>
                  </button>
                );
              },
            )}
            <div className="mt-5 space-y-3 border-t px-3 pt-4">
              <div className="flex items-center justify-between">
                <Label htmlFor="advanced" className="text-xs">
                  显示高级设置
                </Label>
                <Switch
                  id="advanced"
                  checked={advanced}
                  onCheckedChange={setAdvanced}
                />
              </div>
              <p className="text-[11px] leading-5 text-muted-foreground">
                {advanced
                  ? "已显示完整配置。启动配置只能在部署环境中修改。"
                  : "默认显示常用配置。更多模型与编码参数可在高级设置中查看。"}
              </p>
            </div>
          </aside>
          <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border bg-card p-4 xl:hidden">
            <select
              aria-label="配置分类"
              className="native-select max-w-48"
              value={group}
              onChange={(event) => setGroup(event.target.value)}
            >
              {[["all", "全部参数"], ...Object.entries(settingGroups)].map(
                ([key, label]) => (
                  <option key={key} value={key}>
                    {label} ·{" "}
                    {
                      eligible.filter(
                        (field) => key === "all" || field.group === key,
                      ).length
                    }
                  </option>
                ),
              )}
            </select>
            <div className="flex items-center gap-2">
              <Label htmlFor="advanced-mobile" className="text-xs">
                显示高级设置
              </Label>
              <Switch
                id="advanced-mobile"
                checked={advanced}
                onCheckedChange={setAdvanced}
              />
            </div>
          </div>
          <form
            onSubmit={(event) => void save(event)}
            className="min-w-0 space-y-5"
          >
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="relative min-w-56 flex-1">
                <Search className="absolute top-2.5 left-3 size-4 text-muted-foreground" />
                <Input
                  className="bg-card pl-9"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="搜索参数名称、用途或环境变量…"
                />
              </div>
              <span className="text-xs text-muted-foreground">
                显示 {visible.length} / {eligible.length} 项
                {advanced ? "配置" : "常用配置"}
              </span>
            </div>
            {error && (
              <div role="alert" className="error-panel">
                <div className="flex-1">
                  <p className="font-medium">保存失败，草稿已保留</p>
                  <p className="mt-1 text-sm">{error}</p>
                </div>
                {conflict && (
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => void reload()}
                  >
                    读取最新值并核对
                  </Button>
                )}
              </div>
            )}
            <fieldset disabled={saving} className="space-y-8">
              {sections.map((section) => (
                <section key={section.id} className="space-y-4">
                  <div className="flex flex-wrap items-center justify-between gap-2 border-b pb-3">
                    <div>
                      <p className="text-xs text-primary">
                        {settingGroups[section.category] || section.category}
                      </p>
                      <h2 className="mt-1 font-semibold">{section.topic}</h2>
                    </div>
                    <Badge variant="secondary">
                      {section.fields.length} 项
                    </Badge>
                  </div>
                  <div className="space-y-4">
                    {section.fields.map((field) => {
                      const key = String(field.key);
                      const entry = changes[key];
                      const value =
                        entry?.action === "value" ? entry.value : field.value;
                      const spec = fieldSpec(
                        key,
                        { ...field, title: field.label },
                        field.type === "integer" || field.type === "number",
                      );
                      spec.label = text(field.label);
                      spec.description = undefined;
                      if (key === "asr_remote_route_id") {
                        const enabled = new Set(
                          rows(obj(modelQuery.data).connections)
                            .filter((item) => item.enabled)
                            .map((item) => item.id),
                        );
                        spec.options = [
                          { value: "", label: "使用下方独立 API 配置" },
                          ...rows(obj(modelQuery.data).routes)
                            .filter(
                              (item) =>
                                item.capability === "asr" &&
                                item.enabled &&
                                enabled.has(item.connection_id),
                            )
                            .map((item) => ({
                              value: String(item.id),
                              label: `${text(item.name)} · ${text(item.model)}`,
                            })),
                        ];
                        spec.disabled =
                          modelQuery.isPending || modelQuery.isError;
                        spec.description = modelQuery.isError
                          ? "模型路由读取失败，请刷新后重试；当前选择保持不变。"
                          : "先在模型连接与路由页面保存远程语音路由，再在这里选择；仅远程 API 主引擎会上传音频。";
                      }
                      if (field.secret) {
                        spec.type = "password";
                        spec.placeholder = field.configured
                          ? "已设置；留空保持原值"
                          : "未设置";
                      }
                      return (
                        <Card
                          key={key}
                          className={cn(
                            "gap-0 border-border/80 py-0 shadow-none",
                            entry && "border-primary/40",
                          )}
                        >
                          <CardContent className="space-y-3 p-5">
                            <div className="flex flex-wrap items-start justify-between gap-3">
                              <div className="min-w-0">
                                <h3 className="flex flex-wrap items-center gap-2 text-sm font-medium">
                                  {text(field.label)}
                                  {Boolean(field.secret) && (
                                    <Badge
                                      variant="outline"
                                      className="text-[10px]"
                                    >
                                      凭据
                                    </Badge>
                                  )}
                                  {entry && (
                                    <span className="text-[10px] text-primary">
                                      {entry.action === "reset"
                                        ? "将恢复默认"
                                        : entry.action === "clear"
                                          ? "将清空"
                                          : "未保存"}
                                    </span>
                                  )}
                                </h3>
                                <p className="mt-1 font-mono text-[10px] text-muted-foreground">
                                  {text(field.env || field.key)}
                                </p>
                              </div>
                              <Badge
                                variant="secondary"
                                className="text-[10px] font-normal"
                              >
                                {effects[String(field.effect)] ||
                                  text(field.effect)}
                              </Badge>
                            </div>
                            <p className="text-xs leading-6 text-muted-foreground">
                              {text(field.description, "")}
                            </p>
                            <ConfigurationStatus field={field} />
                            {field.editable === false ? (
                              <div className="rounded-lg bg-muted p-3 text-xs text-muted-foreground">
                                {text(field.reason, "仅可通过部署配置修改")} ·
                                当前值：
                                {field.secret
                                  ? field.configured
                                    ? "已设置"
                                    : "未设置"
                                  : text(field.value)}
                              </div>
                            ) : field.schema ? (
                              <SchemaFields
                                schema={obj(field.schema)}
                                value={value}
                                onChange={(next) =>
                                  change(key, { action: "value", value: next })
                                }
                              />
                            ) : (
                              <FormField
                                field={spec}
                                value={
                                  field.secret
                                    ? entry?.action === "value"
                                      ? entry.value
                                      : ""
                                    : value
                                }
                                onChange={(next) =>
                                  change(key, { action: "value", value: next })
                                }
                                disabled={saving}
                              />
                            )}
                            <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-3 text-[10px] text-muted-foreground">
                              <span>
                                当前来源：
                                {field.source === "web"
                                  ? "网页配置"
                                  : field.source === "environment"
                                    ? "部署环境"
                                    : "项目默认"}
                                {field.overridden ? " · 已覆盖部署值" : ""}
                              </span>
                              {field.editable !== false && (
                                <div className="flex gap-1">
                                  <Button
                                    type="button"
                                    variant="ghost"
                                    size="sm"
                                    className="h-6 text-[10px]"
                                    onClick={() =>
                                      change(key, { action: "reset" })
                                    }
                                  >
                                    <RotateCcw className="size-2.5" />
                                    恢复部署默认
                                  </Button>
                                  {Boolean(field.secret) && (
                                    <ConfirmButton
                                      title="明确清空凭据？"
                                      description="清空后会阻止回退到部署环境中的凭据，对应功能可能停止工作。"
                                      confirm="将凭据标记为清空"
                                      onConfirm={async () =>
                                        change(key, { action: "clear" })
                                      }
                                      destructive
                                    >
                                      清空凭据
                                    </ConfirmButton>
                                  )}
                                </div>
                              )}
                            </div>
                          </CardContent>
                        </Card>
                      );
                    })}
                  </div>
                </section>
              ))}
            </fieldset>
            {!visible.length && (
              <div className="rounded-xl border border-dashed p-12 text-center text-sm text-muted-foreground">
                没有匹配的配置项，试试调整分类、搜索或高级设置。
              </div>
            )}
            <div className="save-bar">
              <div className="flex items-center gap-2 text-sm">
                <SlidersHorizontal className="size-4 text-primary" />
                <span>
                  {Object.keys(changes).length
                    ? `${Object.keys(changes).length} 项修改尚未保存`
                    : "所有配置均已保存"}
                </span>
              </div>
              <div className="flex gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  disabled={saving || !draft.dirty}
                  onClick={() => {
                    draft.reset();
                    setError("");
                    setConflict(false);
                  }}
                >
                  放弃修改
                </Button>
                <Button
                  type="submit"
                  disabled={saving || !Object.keys(changes).length}
                >
                  {saving ? (
                    <LoaderCircle className="size-4 animate-spin" />
                  ) : draft.dirty ? (
                    <Save className="size-4" />
                  ) : (
                    <Check className="size-4" />
                  )}
                  保存修改
                </Button>
              </div>
            </div>
          </form>
          {(group === "all" || group === "storage") && <StorageRebuild />}
        </div>
      </QueryState>
    </Page>
  );
}
