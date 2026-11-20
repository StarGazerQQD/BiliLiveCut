import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { SessionDirectoryButton } from "./session-directories";

afterEach(() => vi.unstubAllGlobals());

it("selects the recorded directory by kind and shows its path when the host cannot open it", async () => {
  const user = userEvent.setup();
  const folders = [
    { kind: "raw", label: "原片", path: "F:/raw/session_3", available: true },
    {
      kind: "danmaku",
      label: "弹幕",
      path: "G:/chat/session_3",
      available: true,
    },
  ];
  const fetch = vi.fn(async (url: string, options: RequestInit) => {
    expect(options.method).toBe("POST");
    return Response.json({
      directories: folders,
      selected: url.endsWith("?kind=danmaku") ? "danmaku" : null,
      opened: false,
    });
  });
  vi.stubGlobal("fetch", fetch);
  render(<SessionDirectoryButton sessionId="42" />);
  expect(fetch).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "打开场次目录" }));
  expect(await screen.findByText(folders[0].path)).toBeVisible();
  await user.click(screen.getByRole("button", { name: "打开弹幕目录" }));
  expect(await screen.findByText(/服务主机未能打开文件管理器/)).toBeVisible();
  expect(fetch.mock.calls.map(([url]) => url)).toEqual([
    "/api/sessions/42/open-storage-directory",
    "/api/sessions/42/open-storage-directory?kind=danmaku",
  ]);
  expect(
    screen.getByRole("button", { name: "复制弹幕目录路径" }),
  ).toBeEnabled();
});
