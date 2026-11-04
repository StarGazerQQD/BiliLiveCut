import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";
import { DanmakuPage } from "./library";

vi.mock("@/api/auth", () => ({
  authHeaders: () => ({}),
  useAuth: () => ({ ready: true, authenticated: true }),
  expireAuth: vi.fn(),
  refreshAuth: vi.fn(),
}));
afterEach(() => vi.unstubAllGlobals());

it("pages old sessions and returns to the same page without loading all history", async () => {
  const requests: URL[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) => {
      const url = new URL(path, "http://localhost");
      requests.push(url);
      expect(url.pathname).toBe("/api/danmaku");
      const scoped = url.searchParams.get("session_id");
      const offset = Number(url.searchParams.get("session_offset"));
      const id = scoped || (offset === 0 ? "30" : "1");
      return new Response(
        JSON.stringify({
          available: true,
          total: 100,
          recent: [{ content: `弹幕${id}`, ts: "2026-10-25T00:00:00Z" }],
          sessions: [
            {
              session_id: id,
              count: 0,
              intensity: 0,
              evidence: { state: "failed" },
              source_label: "来源",
              session_title: "场次标题",
            },
          ],
          session_pagination: {
            offset,
            limit: 12,
            total: 13,
            has_more: offset === 0,
          },
        }),
        { headers: { "Content-Type": "application/json" } },
      );
    }),
  );
  const user = userEvent.setup();
  const client = new QueryClient();
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DanmakuPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(await screen.findByText("弹幕30")).toBeInTheDocument();
  expect(screen.getByText("失败")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "下一页" }));
  expect(await screen.findByText("弹幕1")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "查看本场弹幕" }));
  await waitFor(() =>
    expect(
      requests.some((url) => url.searchParams.get("session_id") === "1"),
    ).toBe(true),
  );
  await user.click(screen.getByRole("button", { name: "← 返回全部场次" }));
  expect(screen.getByText("第 2 页")).toBeInTheDocument();
  expect(screen.getByText("弹幕1")).toBeInTheDocument();
  expect(
    requests.every(
      (url) =>
        url.searchParams.get("session_limit") === "12" &&
        url.searchParams.get("limit") === "200",
    ),
  ).toBe(true);
  view.unmount();
  client.clear();
});
