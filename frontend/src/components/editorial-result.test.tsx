import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { EditorialResult } from "./editorial-result";

const base = {
  schema_version: "editorial-view-v1",
  title: "内容说明",
  summary: "已保存的摘要",
  explanation_state: "available",
  editorial_state: "failed",
  technical_score: null,
  hotspot_event_id: 81,
  candidate_id: 22,
  event_id: 17,
};

it("separates identities, unknown scores and actual model failure", () => {
  render(
    <EditorialResult
      value={{
        ...base,
        execution: {
          status: "failed",
          error: "empty_response",
          result: null,
          elapsed_s: 1.5,
          attempts: [
            {
              number: 1,
              provider_id: "route:analysis",
              model: "configured-model",
              reported_model: null,
              status: "failed",
              usage: {
                input_tokens: null,
                output_tokens: null,
                estimated_cost_usd: null,
              },
            },
          ],
        },
      }}
    />,
  );
  expect(screen.getByText("热点 #81")).toBeInTheDocument();
  expect(screen.getByText("候选 #22")).toBeInTheDocument();
  expect(screen.getByText("复核事件 #17")).toBeInTheDocument();
  expect(screen.getByText(/技术分数 未知 · 兴趣等级 未知/)).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("响应为空");
  expect(screen.queryByText("不建议剪辑")).not.toBeInTheDocument();
  expect(screen.getByText(/输入 token：未知/)).toBeInTheDocument();
});

it("keeps a real zero ordinal distinct from unknown and escapes evidence text", () => {
  const { container } = render(
    <EditorialResult
      value={{
        ...base,
        technical_score: 0,
        evidence: [
          {
            evidence_id: "asr:1",
            type: "asr",
            state: "available",
            timing_precision: "unknown",
            start_s: null,
            end_s: null,
            context_role: "unknown",
            excerpts: ["<script>alert('x')</script>"],
          },
        ],
        execution: {
          elapsed_s: 0.5,
          attempts: [],
          result: {
            verdict: "reject",
            interest_rating: 0,
            hook: "",
            reason: "信息不足",
            standalone: null,
            boundary_status: "uncertain",
            uncertainties: [],
            evidence_ids: ["asr:1"],
          },
        },
      }}
    />,
  );
  expect(
    screen.getByText(/技术分数 0.000 · 兴趣等级 0 \/ 4/),
  ).toBeInTheDocument();
  expect(screen.getByText(/语音转写 · 可用 · 时间未知/)).toBeInTheDocument();
  expect(screen.getByText(/无精确起止时间/)).toBeInTheDocument();
  expect(screen.getByText("<script>alert('x')</script>")).toBeInTheDocument();
  expect(container.querySelector("script")).toBeNull();
});

it("does not render absent or invalid contracts", () => {
  const { container } = render(<EditorialResult value={null} />);
  expect(container).toBeEmptyDOMElement();
});

it("keeps shared budget prior failures visible when the current judgment sends no request", () => {
  render(
    <EditorialResult
      value={{
        ...base,
        previous_attempts: ["explanation", "repair", "review"].map(
          (purpose, index) => ({
            number: index + 1,
            purpose,
            provider_id: "analysis",
            model: "deepseek-flash",
            status: "failed",
            diagnostic: {
              category: "validation",
              summary: `可信字段校验失败 ${index + 1}`,
            },
          }),
        ),
        execution: {
          status: "failed",
          error: "attempt_budget",
          result: null,
          elapsed_s: 0.01,
          attempt_offset: 3,
          attempts: [],
        },
      }}
    />,
  );
  expect(screen.getByText(/本次判断 0 次请求/)).toBeInTheDocument();
  expect(
    screen.getByText(/同一操作前序 3 次请求 · 已保存 3 条前序记录/),
  ).toBeInTheDocument();
  expect(screen.getAllByRole("note")).toHaveLength(3);
  expect(
    screen.getByText("同一操作前序请求 · 用途：内容解释"),
  ).toBeInTheDocument();
  expect(screen.queryByText("不建议剪辑")).not.toBeInTheDocument();
});

it("shows unknown usage when the optional usage object is absent", () => {
  render(
    <EditorialResult
      value={{
        ...base,
        execution: {
          status: "failed",
          error: "provider_error",
          result: null,
          elapsed_s: 0.5,
          attempts: [
            {
              number: 1,
              provider_id: "route:analysis",
              model: "configured-model",
              status: "failed",
            },
          ],
        },
      }}
    />,
  );
  expect(screen.getByText(/输入 token：未知/)).toBeInTheDocument();
  expect(screen.getByText(/估算费用（USD）：未知/)).toBeInTheDocument();
});

it("shows bounded service diagnostics and keeps failed judgments unresolved", () => {
  render(
    <EditorialResult
      value={{
        ...base,
        execution: {
          status: "failed",
          error: "provider_error",
          result: null,
          elapsed_s: 0.5,
          attempts: [
            {
              number: 1,
              provider_id: "analysis",
              model: "deepseek-flash",
              protocol: "json_object",
              status: "failed",
              diagnostic: {
                category: "http",
                summary: "服务拒绝了请求，请结合状态码和参数检查配置。",
                http_status: 400,
                provider_code: "invalid_request_error",
                parameter: "max_tokens",
                request_id: "request-123",
              },
            },
          ],
        },
      }}
    />,
  );
  expect(screen.getByText("响应格式：json_object")).toBeInTheDocument();
  expect(screen.getByRole("note")).toHaveTextContent("HTTP 状态：400");
  expect(screen.getByRole("note")).toHaveTextContent(
    "服务错误码：invalid_request_error",
  );
  expect(screen.getByRole("note")).toHaveTextContent("相关参数：max_tokens");
  expect(screen.getByRole("note")).toHaveTextContent(
    "服务请求标识：request-123",
  );
  expect(screen.queryByText("不建议剪辑")).not.toBeInTheDocument();
});
