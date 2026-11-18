import { describe, expect, it } from "vitest";
import { duration, sessionSource, time } from "./data";

describe("session source labels", () => {
  it("distinguishes rooms with the same name and session title", () => {
    const session = { uploader_name: " 同名主播 ", session_title: "周末直播" };
    expect(sessionSource({ ...session, room_id: 12 })).toBe(
      "主播：同名主播 · 房间 12",
    );
    expect(
      sessionSource({
        ...session,
        platform: "example",
        source_id: "stream-b",
        room_id: 12,
      }),
    ).toBe("主播：同名主播 · example · 房间 stream-b");
  });
  it("preserves missing streamer names and local imports", () => {
    expect(
      sessionSource({
        session_title: "片段标题",
        source_label: "房间标题",
        room_db_id: 99,
        room_id: 2,
      }),
    ).toBe("主播未记录 · 房间 2");
    expect(
      sessionSource({ platform: "local", session_title: "我的视频" }),
    ).toBe("本地录播");
    expect(sessionSource({ platform: "local", uploader_name: "原作者" })).toBe(
      "本地录播 · 主播：原作者",
    );
  });
});

describe("session times in GMT+8", () => {
  it.each([
    "2026-10-20T14:39:08Z",
    "2026-10-20T14:39:08",
    "2026-10-20T22:39:08+08:00",
    "2026-10-20T07:39:08-07:00",
  ])(
    "formats %s as the same instant without adding the offset twice",
    (value) => {
      expect(time(value, "GMT+8")).toBe("2026/10/20 22:39:08 GMT+8");
    },
  );

  it("crosses the date boundary in GMT+8", () => {
    expect(time("2026-10-20T17:00:00Z", "GMT+8")).toBe(
      "2026/10/21 01:00:00 GMT+8",
    );
  });

  it("keeps the local formatter available for other pages", () => {
    const value = "2026-10-20T14:39:08Z";
    expect(time(value)).toBe(
      new Intl.DateTimeFormat("zh-CN", {
        dateStyle: "short",
        timeStyle: "medium",
      }).format(new Date(value)),
    );
  });

  it("does not add a timezone label to absent or invalid dates", () => {
    expect(time(null, "GMT+8")).toBe("—");
    expect(time("invalid", "GMT+8")).toBe("invalid");
    expect(duration(774)).toBe("12:54");
  });
});
