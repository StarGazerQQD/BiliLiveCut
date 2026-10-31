import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { expect, it } from "vitest";
import { RoomCard } from "./rooms";

it.each([
  ["stopped", "stopped", "已停止", "已暂停"],
  ["force_stopped", "force_stopped", "已强制停止", "已暂停"],
  ["paused", "manual_paused", "已暂停", "已停止"],
])("distinguishes explicit %s control", (recording, monitor, label, other) => {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <RoomCard
          room={{
            id: 1,
            running: false,
            recording_state: recording,
            monitor: { state: monitor },
          }}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(screen.getByText(label)).toBeInTheDocument();
  expect(screen.queryByText(other)).not.toBeInTheDocument();
});

it.each([
  ["reconnecting", "重连中"],
  ["stopping", "正在停止"],
  ["finalizing", "收尾中"],
])("keeps the active recorder's %s state", (state, label) => {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <RoomCard room={{ id: 1, running: true, recording_state: state }} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(screen.getByText(label)).toBeInTheDocument();
  expect(screen.queryByText("录制中")).not.toBeInTheDocument();
});

it.each([
  ["cooldown", "冷却后恢复"],
  ["action_required", "需要处理"],
  ["manual_paused", "已暂停"],
])("shows the %s reason without claiming to record", (state, label) => {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <RoomCard
          room={{
            id: 1,
            running: false,
            recording_state: "error",
            recording_message: "保留的录制失败原因",
            monitor: { state, retry_in_s: 12 },
          }}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(screen.getByText(label)).toBeInTheDocument();
  expect(screen.getByText("保留的录制失败原因")).toBeInTheDocument();
  if (state === "cooldown") {
    expect(
      screen.getByText("约 12 秒后复核直播状态并尝试恢复"),
    ).toBeInTheDocument();
  }
});
