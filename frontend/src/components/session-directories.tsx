import { useState } from "react";
import { FolderOpen } from "lucide-react";
import { toast } from "sonner";
import { send } from "@/api/client";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { copyText } from "@/lib/clipboard";

type Directories = components["schemas"]["SessionDirectories"];
type Kind = components["schemas"]["SessionDirectory"]["kind"];

export function SessionDirectoryButton({ sessionId }: { sessionId: string }) {
  const [result, setResult] = useState<Directories | null>(null);
  const [busy, setBusy] = useState(false);
  async function open(kind?: Kind) {
    setBusy(true);
    try {
      const response = await send<Directories>(
        `/api/sessions/${encodeURIComponent(sessionId)}/open-storage-directory${kind ? `?kind=${kind}` : ""}`,
        "POST",
      );
      setResult(response);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "无法读取场次目录");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button
        type="button"
        variant="outline"
        disabled={busy}
        onClick={() => void open()}
      >
        <FolderOpen className="size-4" /> 打开场次目录
      </Button>
      <Dialog
        open={result !== null}
        onOpenChange={(next) => {
          if (!next) setResult(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>场次 #{sessionId} 的存储目录</DialogTitle>
            <DialogDescription>
              {result?.opened
                ? "已在服务主机请求打开目录。"
                : result?.selected &&
                    result.directories.some(
                      (item) => item.kind === result.selected && item.available,
                    )
                  ? "服务主机未能打开文件管理器，可复制目录路径。"
                  : "选择服务主机上的目录，或复制路径。"}
            </DialogDescription>
          </DialogHeader>
          {result?.directories.map((item) => (
            <div key={item.kind} className="space-y-2">
              <p className="text-sm font-medium">
                {item.label}
                {!item.available && "（目录不可用）"}
              </p>
              <p className="break-all rounded-md bg-muted p-2 font-mono text-xs">
                {item.path}
              </p>
              <div className="flex gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={busy || !item.available}
                  onClick={() => void open(item.kind)}
                >
                  打开{item.label}目录
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  aria-label={`复制${item.label}目录路径`}
                  onClick={() =>
                    void copyText(item.path)
                      .then(() => toast.success("路径已复制"))
                      .catch(() => toast.error("请手动复制目录路径"))
                  }
                >
                  复制路径
                </Button>
              </div>
            </div>
          ))}
        </DialogContent>
      </Dialog>
    </>
  );
}
