import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { SessionSummary } from "./session-summary";

it("keeps a failed verification draft readable without presenting it as verified", () => {
  render(
    <SessionSummary
      value={{
        status: "failed",
        summary: null,
        draft_summary: "完整的全场草稿正文",
        verification: {
          status: "failed",
          completed_nodes: 2,
          total_nodes: 3,
          error: "核验格式未通过",
        },
      }}
    />,
  );
  expect(screen.getByRole("article", { name: "总结草稿" })).toHaveTextContent(
    "完整的全场草稿正文",
  );
  expect(screen.getByRole("alert")).toHaveTextContent("核验格式未通过");
  expect(screen.queryByText("核验通过")).not.toBeInTheDocument();
  expect(screen.getByText("2 / 3 个节点")).toBeInTheDocument();
});

it("shows the final summary only after verification and renders model text safely", () => {
  const body = "<img src=x onerror=alert(1)>\n第二段原文";
  const { container } = render(
    <SessionSummary
      value={{
        status: "ready",
        summary: body,
        draft_summary: "旧草稿不应重复显示",
        verification: {
          status: "verified",
          completed_nodes: 1,
          total_nodes: 1,
        },
      }}
    />,
  );
  expect(screen.getByRole("article", { name: "已核验总结" })).toHaveTextContent(
    "<img src=x onerror=alert(1)>",
  );
  expect(container.querySelector("img")).toBeNull();
  expect(screen.queryByText("旧草稿不应重复显示")).not.toBeInTheDocument();
  expect(screen.getByText("核验通过")).toBeInTheDocument();
});

it.each([
  ["recording", "录制结束后生成整场总结"],
  ["waiting", "等待转写与分析完成"],
  ["pending", "等待生成或继续处理"],
  ["unconfigured", "请在模型设置中启用整场总结路由，待处理请求会继续。"],
])("keeps %s separate from a generation failure", (status, label) => {
  render(<SessionSummary value={{ status }} />);
  expect(screen.getByRole("status")).toHaveTextContent(label);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("shows a generation failure without inventing a draft", () => {
  render(
    <SessionSummary value={{ status: "failed", error: "模型没有返回正文" }} />,
  );
  expect(screen.getByRole("alert")).toHaveTextContent("模型没有返回正文");
  expect(screen.queryByRole("article")).not.toBeInTheDocument();
});

it("keeps a legacy ready summary readable without assuming verification", () => {
  render(
    <SessionSummary value={{ status: "ready", summary: "历史总结正文" }} />,
  );
  expect(screen.getByRole("article", { name: "总结正文" })).toHaveTextContent(
    "历史总结正文",
  );
  expect(screen.getByText("核验状态未知")).toBeInTheDocument();
  expect(screen.queryByText("核验通过")).not.toBeInTheDocument();
});
