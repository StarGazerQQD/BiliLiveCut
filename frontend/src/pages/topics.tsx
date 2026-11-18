import { copyText } from "@/lib/clipboard";
import { useState } from "react";
import { Link, useParams } from "react-router";
import { ArrowDown, ArrowUp, FolderKanban } from "lucide-react";
import { toast } from "sonner";
import { send, useAction, useApi, type Row } from "@/api/client";
import { useDraft } from "@/lib/drafts";
import { obj, rows, text, number, duration, sessionSource } from "@/lib/data";
import {
  Page,
  QueryState,
  Empty,
  Refresh,
  FormDialog,
  ConfirmButton,
  FormField,
  Status,
} from "@/components/workbench";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Evidence } from "./review";

export function TopicsPage() {
  const query = useApi("/api/topics", 10000);
  const action = useAction();
  return (
    <Page
      eyebrow="PRODUCE / COLLECTIONS"
      title="主题与合集"
      description="将关联片刻组织成一个主题，再为合集安排章节、顺序与文案。"
      actions={<Refresh refresh={query.refetch} />}
    >
      <QueryState query={query}>
        {rows(obj(query.data).topics).length ? (
          <div className="grid gap-5 md:grid-cols-2 xl:grid-cols-3">
            {rows(obj(query.data).topics).map((topic) => (
              <Card key={text(topic.id)}>
                <CardContent className="space-y-4">
                  <div className="flex justify-between">
                    <FolderKanban className="size-6 text-primary" />
                    <Status value={topic.status || "draft"} />
                  </div>
                  <h2 className="font-semibold">
                    {text(topic.title, `主题 #${topic.id}`)}
                  </h2>
                  <p className="line-clamp-3 text-sm leading-7 text-muted-foreground">
                    {text(topic.summary, "暂无主题摘要")}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    场次 #{text(topic.session_id)} ·{" "}
                    {sessionSource(obj(topic.source))} ·{" "}
                    {number(topic.event_count || topic.clip_count)} 个事件
                  </p>
                  <div className="flex flex-wrap gap-2">
                    <Button asChild>
                      <Link to={`/collection/${topic.id}`}>编辑合集</Link>
                    </Button>
                    <FormDialog
                      id={`topic-${topic.id}`}
                      title="编辑主题"
                      trigger={<Button variant="outline">主题设置</Button>}
                      fields={[
                        { name: "title", label: "标题", required: true },
                        { name: "summary", label: "摘要", type: "textarea" },
                        {
                          name: "keywords_json",
                          label: "关键词 JSON 数组",
                          type: "textarea",
                        },
                        {
                          name: "is_collection",
                          label: "作为合集",
                          type: "boolean",
                        },
                        { name: "status", label: "状态" },
                      ]}
                      initial={topic}
                      onSubmit={(body) =>
                        action.mutateAsync({
                          path: `/api/topics/${topic.id}`,
                          method: "PATCH",
                          body,
                        })
                      }
                    />
                    <FormDialog
                      id={`topic-merge-${topic.id}`}
                      title="合并到其他主题"
                      description="当前主题的事件将并入目标主题，请核对目标编号。"
                      trigger={<Button variant="ghost">合并</Button>}
                      fields={[
                        {
                          name: "target_id",
                          label: "目标主题 ID",
                          type: "number",
                          min: 1,
                          required: true,
                        },
                      ]}
                      onSubmit={(body) =>
                        action.mutateAsync({
                          path: "/api/topics/merge",
                          body: {
                            source_id: topic.id,
                            target_id: body.target_id,
                          },
                        })
                      }
                    />
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        ) : (
          <Empty
            title="还没有主题"
            description="在场次时间线中运行主题聚类，然后回到这里编辑合集。"
            action={
              <Button asChild>
                <Link to="/sessions">查看场次</Link>
              </Button>
            }
          />
        )}
      </QueryState>
    </Page>
  );
}
export function CollectionPage() {
  const { id = "" } = useParams();
  const path = `/collection/api/${id}`;
  const query = useApi<Row>(path, 10000);
  const draft = useDraft<Row[] | null>(`collection-${id}`, null);
  const action = useAction();
  const [selected, setSelected] = useState<number[]>([]);
  const [cards, setCards] = useState(true);
  const [copy, setCopy] = useState<unknown>(null);
  const events = draft.value ?? rows(query.data?.events);
  const eventId = (entry: Row) => number(entry.id || entry.event_id);
  function move(index: number, direction: number) {
    const next = [...events];
    [next[index], next[index + direction]] = [
      next[index + direction],
      next[index],
    ];
    draft.update(next);
  }
  async function save() {
    await action.mutateAsync({
      path: `${path}/reorder`,
      body: {
        event_ids: events.map(eventId),
        chapter_titles: Object.fromEntries(
          events.map((entry) => [
            String(eventId(entry)),
            text(entry.chapter_title || entry.title, ""),
          ]),
        ),
      },
    });
    draft.reset();
  }
  return (
    <Page
      title={text(obj(query.data?.topic).title, `合集 #${id}`)}
      description="调整章节顺序，检查时间重叠后提交渲染。排序与章节修改仅在保存后生效。"
      actions={
        <>
          <Button asChild variant="outline">
            <Link to="/topics">返回主题</Link>
          </Button>
          <Button
            disabled={!draft.dirty || action.isPending}
            onClick={() => void save().catch(() => {})}
          >
            保存顺序与章节
          </Button>
        </>
      }
    >
      <QueryState query={query}>
        <div className="flex flex-wrap items-center gap-3">
          <span className="mr-auto text-sm text-muted-foreground">
            {sessionSource(obj(obj(query.data?.topic).source))} · 共{" "}
            {events.length} 段 · {duration(query.data?.total_duration_s)}
          </span>
          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={cards}
              onCheckedChange={(next) => setCards(!!next)}
            />
            包含章节卡
          </label>
          <ConfirmButton
            title="渲染当前合集？"
            description="将先保存当前章节与顺序，再提交后台渲染任务。"
            disabled={!events.length || action.isPending}
            onConfirm={async () => {
              await save();
              await action.mutateAsync({
                path: `${path}/render`,
                body: {
                  event_ids: events.map(eventId),
                  chapter_titles: events.map((entry) =>
                    text(entry.chapter_title || entry.title, ""),
                  ),
                  include_chapter_cards: cards,
                },
              });
            }}
          >
            渲染合集
          </ConfirmButton>
          <Button
            variant="outline"
            disabled={action.isPending}
            onClick={() =>
              void send(`${path}/copywriter`, "POST")
                .then(setCopy)
                .catch((error: Error) => toast.error(error.message))
            }
          >
            生成合集文案
          </Button>
        </div>
        {rows(query.data?.overlaps).length > 0 && (
          <div className="error-panel">
            <div>
              <p className="mb-2 font-medium">检测到时间重叠</p>
              <Evidence value={query.data?.overlaps} />
            </div>
          </div>
        )}
        {selected.length > 0 && (
          <ConfirmButton
            title="拆分选中的事件？"
            description="这些事件将从当前主题提取到新主题。"
            onConfirm={async () => {
              await action.mutateAsync({
                path: `/api/topics/${id}/split`,
                body: { event_ids: selected },
              });
              setSelected([]);
              draft.reset();
            }}
          >
            拆分 {selected.length} 个事件为新主题
          </ConfirmButton>
        )}
        <div className="space-y-4">
          {events.map((entry, index) => (
            <Card key={eventId(entry)}>
              <CardContent className="space-y-4">
                <div className="flex items-start gap-4">
                  <Checkbox
                    aria-label={`选择事件 ${eventId(entry)}`}
                    checked={selected.includes(eventId(entry))}
                    onCheckedChange={(next) =>
                      setSelected(
                        next
                          ? [...selected, eventId(entry)]
                          : selected.filter(
                              (value) => value !== eventId(entry),
                            ),
                      )
                    }
                  />
                  <span className="grid size-9 place-items-center rounded-lg bg-primary/8 text-sm font-semibold text-primary">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  <div className="flex-1">
                    <h2 className="text-sm font-semibold">
                      {text(
                        entry.title || entry.reason,
                        `事件 #${eventId(entry)}`,
                      )}
                    </h2>
                    <p className="mt-2 text-xs text-muted-foreground">
                      {duration(entry.duration_s)}
                    </p>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="向上移动"
                    disabled={index === 0}
                    onClick={() => move(index, -1)}
                  >
                    <ArrowUp className="size-4" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="向下移动"
                    disabled={index === events.length - 1}
                    onClick={() => move(index, 1)}
                  >
                    <ArrowDown className="size-4" />
                  </Button>
                </div>
                <FormField
                  field={{
                    name: `chapter-${eventId(entry)}`,
                    label: "章节标题",
                  }}
                  value={entry.chapter_title || ""}
                  onChange={(next) =>
                    draft.update(
                      events.map((event, i) =>
                        i === index ? { ...event, chapter_title: next } : event,
                      ),
                    )
                  }
                />
                <div className="flex gap-2">
                  {entry.candidate_id ? (
                    <Button asChild size="sm" variant="outline">
                      <Link to={`/review/${entry.candidate_id}`}>打开审片</Link>
                    </Button>
                  ) : null}
                  <ConfirmButton
                    title="从主题移除此事件？"
                    description="事件本身和已生成的成品保留。请先保存其他排序修改。"
                    disabled={draft.dirty}
                    onConfirm={() =>
                      action.mutateAsync({
                        path: `/api/topics/${id}/events/${eventId(entry)}`,
                        method: "DELETE",
                      })
                    }
                  >
                    移出主题
                  </ConfirmButton>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
        {!events.length && (
          <Empty
            title="这个主题还没有事件"
            description="可以按事件 ID 添加同场事件。"
          />
        )}
        <FormDialog
          id={`topic-add-${id}`}
          title="添加事件"
          trigger={<Button variant="outline">按事件 ID 添加</Button>}
          fields={[
            {
              name: "event_id",
              label: "事件 ID",
              type: "number",
              min: 1,
              required: true,
            },
          ]}
          onSubmit={(body) =>
            action.mutateAsync({
              path: `/api/topics/${id}/events/${body.event_id}`,
            })
          }
        />
        {draft.dirty && (
          <Button variant="ghost" onClick={() => draft.reset()}>
            放弃章节修改
          </Button>
        )}
        {copy !== null && (
          <Card>
            <CardContent className="space-y-3">
              <h2 className="font-semibold">合集文案</h2>
              <Evidence value={copy} />
              <Button
                variant="outline"
                onClick={() =>
                  void copyText(
                    typeof copy === "string"
                      ? copy
                      : JSON.stringify(copy, null, 2),
                  )
                    .then(() => toast.success("文案已复制"))
                    .catch(() => toast.error("无法访问剪贴板，请手动复制"))
                }
              >
                复制文案
              </Button>
            </CardContent>
          </Card>
        )}
      </QueryState>
    </Page>
  );
}
