import { useState, type FormEvent } from "react";
import { send, useApi } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const emptySource = () => ({
  storage: "",
  database: "",
  original_storage: "",
  raw: "",
  danmaku: "",
  directory_maps: "",
});
type Source = ReturnType<typeof emptySource>;
type RebuildJob = {
  id: string;
  status: string;
  message?: string;
  error?: string;
  result?: {
    database: string;
    sessions: number;
    segments: number;
    messages: number;
    missing_files: number;
  };
};
const labels: Record<keyof Source, string> = {
  storage: "来源 storage 目录",
  database: "离线数据库文件（可选）",
  original_storage: "搬迁前的 storage 绝对路径（可选）",
  raw: "外置原片目录（可选）",
  danmaku: "外置弹幕目录（可选）",
  directory_maps: "多个旧目录的映射（可选，JSON 对象）",
};

export function StorageRebuild() {
  const [sources, setSources] = useState([emptySource()]);
  const [jobId, setJobId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const job = useApi<RebuildJob>(`/api/jobs/${jobId}`, 2000, !!jobId);
  const result = job.data?.result;
  const running =
    !!jobId &&
    !["succeeded", "cancelled", "failed"].includes(job.data?.status || "");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const response = await send<{ job: RebuildJob }>(
        "/api/settings/storage-rebuild",
        "POST",
        {
          sources: sources.map(({ directory_maps, ...source }) => ({
            ...Object.fromEntries(
              Object.entries(source)
                .map(([key, value]) => [key, value.trim()])
                .filter(([, value]) => !!value),
            ),
            directory_maps: directory_maps.trim()
              ? JSON.parse(directory_maps)
              : {},
          })),
        },
      );
      setJobId(response.job.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "提交失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <details className="rounded-xl border p-4">
      <summary className="cursor-pointer font-medium">
        从历史文件重建媒体库
      </summary>
      <form onSubmit={submit} className="mt-4 space-y-4">
        <p className="text-sm text-muted-foreground">
          填写服务主机上的离线副本路径。接受 Schema 5 和
          6。数据库留空时按场次文件恢复。 新库保存在当前 storage 的 rebuilds
          目录，原片和弹幕继续引用原位置。
        </p>
        <fieldset disabled={submitting || running} className="space-y-4">
          {sources.map((source, index) => (
            <fieldset
              key={index}
              className="grid gap-3 rounded-lg border p-3 sm:grid-cols-2"
            >
              <legend className="px-1 text-sm">来源 {index + 1}</legend>
              {(Object.keys(labels) as (keyof Source)[]).map((key) => (
                <label key={key} className="grid gap-1 text-sm">
                  {labels[key]}
                  <Input
                    value={source[key]}
                    required={key === "storage"}
                    onChange={(event) =>
                      setSources(
                        sources.map((item, i) =>
                          i === index
                            ? { ...item, [key]: event.target.value }
                            : item,
                        ),
                      )
                    }
                  />
                </label>
              ))}
              {sources.length > 1 && (
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() =>
                    setSources(sources.filter((_, i) => i !== index))
                  }
                >
                  移除此来源
                </Button>
              )}
            </fieldset>
          ))}
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => setSources([...sources, emptySource()])}
            >
              添加来源
            </Button>
            <Button type="submit">生成新库</Button>
          </div>
        </fieldset>
        <div role="status" className="break-all text-sm">
          {error ||
            job.data?.error ||
            job.error?.message ||
            (result
              ? `新库 ${result.database}。场次 ${result.sessions}，原片 ${result.segments}，弹幕 ${result.messages}，缺失文件 ${result.missing_files}。`
              : jobId
                ? job.data?.message || "正在读取作业状态…"
                : "")}
        </div>
      </form>
    </details>
  );
}
