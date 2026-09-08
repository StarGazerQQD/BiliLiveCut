// BiliLiveCut \u8bbe\u7f6e:\u6a21\u578b\u3001\u8d26\u53f7\u3001\u65e5\u5fd7\u3001\u4efb\u52a1\u961f\u5217\u3001\u5b57\u5e55/\u7247\u5934\u7247\u5c3e\u6a21\u677f
import { $, api, toast, esc, badge, sessionTitle } from "./common.js";
import { loadConfiguration } from "./configuration.js";

// ----------------------------- \u591a\u5927\u6a21\u578b\u914d\u7f6e ----------------------------- //
let _llmDirty = false;
let _llmRevision = 0;
let _llmSaving = false;

function markLLMDirty() {
  _llmDirty = true;
  _llmRevision += 1;
  const status = $("#llm-status");
  if (status && !status.textContent.includes("\u672a\u4fdd\u5b58")) status.textContent += " \u00b7 \u6709\u672a\u4fdd\u5b58\u66f4\u6539";
}

function hasLLMDraft() {
  return _llmDirty || _llmSaving;
}

function llmRow(p = {}) {
  p = { ...p, id: p.id || crypto.randomUUID() };
  const keyPlaceholder = p.api_key_set ? "已配置（留空保持）" : "填写 API Key";
  return `<div class="item llm-row" data-id="${esc(p.id || "")}">
    <div class="settings-scoring">
      <label>名称<input class="llm-name" value="${esc(p.name || "")}" required /></label>
      <label>API 地址<input class="llm-base" placeholder="https://…/v1" value="${esc(p.base_url || "")}" required /></label>
      <label>模型 ID<input class="llm-model" value="${esc(p.model || "")}" required /></label>
      <label>API Key<input class="llm-key" type="password" autocomplete="new-password" placeholder="${keyPlaceholder}" /></label>
      <label>联网参数名<input class="llm-search" value="${esc(p.web_search_param || "")}" /></label>
      <label>优先级（小者优先）<input class="llm-priority" type="number" step="1" value="${p.priority ?? 100}" /></label>
      <label>输入价格（美元/百万 token）<input class="llm-price-input" type="number" min="0" step="any" value="${p.price_input_per_m ?? 0}" /></label>
      <label>输出价格（美元/百万 token）<input class="llm-price-output" type="number" min="0" step="any" value="${p.price_output_per_m ?? 0}" /></label>
    </div>
    <div class="row">
      <label class="chk"><input type="checkbox" class="llm-enabled" ${p.enabled === false ? "" : "checked"} /> 启用</label>
      <label class="chk"><input type="checkbox" class="llm-clear-key" /> 保存时明确清空密钥</label>
      <button class="llm-del" data-act="del-llm">删除服务商</button>
    </div>
    <p class="hint">价格为 0 表示不估算该部分费用；填写实际价格后每日预算才能按此估算。</p>
  </div>`;
}

async function loadLLM(force = false) {
  if (_llmDirty && !force) return;
  const revision = _llmRevision;
  const data = await api("GET", "/api/llm-providers");
  if (_llmDirty || _llmRevision !== revision) return;
  $("#llm-status").textContent = `\u5df2\u914d\u7f6e ${data.providers.length} \u4e2a \u00b7 \u53ef\u7528 ${data.active_count} \u4e2a(\u6309\u4f18\u5148\u7ea7\u4ece\u5c0f\u5230\u5927\u8c03\u7528)`;
  $("#llm-list").innerHTML = data.providers.length
    ? data.providers.map(llmRow).join("")
    : `<div class="empty">尚未配置服务商。点击“新增模型”，填写地址、模型和密钥后保存。</div>`;
  _llmDirty = false;
}

function collectLLM() {
  return [...document.querySelectorAll(".llm-row")].map((row, index) => {
    const read = selector => row.querySelector(selector);
    for (const selector of [".llm-name", ".llm-base", ".llm-model", ".llm-priority", ".llm-price-input", ".llm-price-output"]) {
      const control = read(selector);
      if (!control.value.trim() || (control.checkValidity && !control.checkValidity())) {
        control.focus?.();
        throw new Error(`第 ${index + 1} 个服务商有未填写或超出范围的字段；已定位，未保存任何行`);
      }
    }
    return {
      id: row.dataset.id || "", name: read(".llm-name").value.trim(),
      base_url: read(".llm-base").value.trim(), model: read(".llm-model").value.trim(),
      api_key: read(".llm-key").value, clear_api_key: read(".llm-clear-key").checked,
      web_search_param: read(".llm-search").value.trim(), priority: Number(read(".llm-priority").value),
      price_input_per_m: Number(read(".llm-price-input").value), price_output_per_m: Number(read(".llm-price-output").value),
      enabled: read(".llm-enabled").checked,
    };
  });
}

function renderLLMTestResults(results) {
  const target = $("#llm-test-results");
  if (!results.length) {
    target.innerHTML = '<span class="warn">\u6ca1\u6709\u53ef\u6d4b\u8bd5\u7684\u6a21\u578b\uff1a\u8bf7\u586b\u5199 base_url\u3001\u6a21\u578b\u548c API Key\uff0c\u5e76\u52fe\u9009\u300c\u542f\u7528\u300d\u3002</span>';
    return;
  }
  target.innerHTML = results.map((item) => `
    <div class="item" style="margin-top:6px">
      <b>${esc(item.name)}</b> \u00b7 ${item.ok ? '<span class="ok">\u8fde\u901a\u6210\u529f</span>' : '<span class="warn">\u8fde\u901a\u5931\u8d25</span>'}
      <div class="sub">${esc(item.detail || (item.ok ? "\u670d\u52a1\u5df2\u54cd\u5e94" : "\u672a\u8fd4\u56de\u9519\u8bef\u8be6\u60c5"))}</div>
    </div>`).join("");
}

// ----------------------------- \u6e32\u67d3:\u65e5\u5fd7 ----------------------------- //
async function loadLogs() {
  const rows = await api("GET", "/api/logs?limit=100");
  $("#logs-list").innerHTML = rows.length ? `<div class="card">${rows.map((l) => `
    <div class="log-line"><span class="lvl-${l.level}">[${esc(l.level)}]</span>
      ${esc(l.created_at || "")} ${esc(l.module || "")}:${esc(l.event || "")} \u2014 ${esc(l.message)}</div>`).join("")}</div>`
    : `<div class="empty">\u6682\u65e0 WARNING/ERROR \u65e5\u5fd7\u3002</div>`;
}

// ----------------------------- V0.1.6 \u6e32\u67d3:\u4efb\u52a1\u961f\u5217 ----------------------------- //
async function loadTasks() {
  try {
    const data = await api("GET", "/api/tasks?limit=40");
    const { tasks, stats } = data;
    const w = stats.worker || {};
    $("#task-stat-total").textContent = stats.total || 0;
    $("#task-stat-queued").textContent = (
      (stats.queued_for_transcription || 0) + (stats.queued_for_analysis || 0) + (stats.queued_for_render || 0)
    );
    $("#task-stat-active").textContent = (
      (w.transcribing || 0) + (w.analyzing || 0) + (w.rendering || 0)
    );
    $("#task-stat-failed").textContent = (stats.failed || 0) + (stats.transient_failed || 0);
    $("#task-stat-completed").textContent = stats.completed || 0;
    const el = $("#stat-tasks");
    if (el) el.textContent = stats.total || 0;

    $("#task-tbody").innerHTML = tasks.length ? tasks.map(t => `
      <tr>
        <td>${esc(t.id)}</td>
        <td title="${esc(t.room_title || "")}">${esc(t.source_label || "未知来源")}<br>${esc(sessionTitle(t))}<br><span class="muted">会话 #${esc(t.session_id)}</span></td>
        <td>${esc(t.segment_id)}</td>
        <td><span class="badge badge-${esc(t.stage.replace(/_/g,'-'))}">${esc(t.stage)}</span></td>
        <td>${t.attempts}/${t.max_retries}</td>
        <td>${t.processing_time_ms != null ? t.processing_time_ms : "-"}</td>
        <td title="${esc(t.last_error || "")}">${(t.last_error || "").substring(0,40)}</td>
        <td>${esc(t.created_at || "").substring(0,19)}</td>
        <td>
          ${t.stage === "failed" || t.stage === "cancelled"
            ? `<button class="small" onclick="retryTask(${t.id})">\u91cd\u8bd5</button>`
            : t.stage === "completed" || t.stage === "failed" || t.stage === "cancelled"
              ? "-"
              : `<button class="small danger" onclick="cancelTask(${t.id})">\u53d6\u6d88</button>`
          }
        </td>
      </tr>`).join("") : `<tr><td colspan="9" class="empty">暂无任务。</td></tr>`;
  } catch (e) { console.warn("\u52a0\u8f7d\u5931\u8d25:", e); }
}

async function retryTask(id) {
  try { await api("POST", `/api/tasks/${id}/retry`); toast("\u4efb\u52a1\u5df2\u91cd\u65b0\u5165\u961f"); loadTasks(); }
  catch (e) { toast("\u91cd\u8bd5\u5931\u8d25:" + e.message); }
}

async function cancelTask(id) {
  try { await api("POST", `/api/tasks/${id}/cancel`); toast("\u4efb\u52a1\u5df2\u53d6\u6d88"); loadTasks(); }
  catch (e) { toast("\u53d6\u6d88\u5931\u8d25:" + e.message); }
}

// ----------------------------- \u8d26\u53f7\u7ba1\u7406 ----------------------------- //
async function loadCookieStatus() {
  try {
    const info = await api("GET", "/api/cookie-status");
    const hint = $("#cookie-hint");
    if (info.has_cookie) {
      hint.innerHTML = `\u5df2\u767b\u5f55 \u00b7 UID: <b>${esc(info.uid || "?")}</b>`;
      hint.className = "hint ok";
    } else {
      hint.textContent = "尚未登录；公开弹幕可匿名采集，需要登录态的画质请先登录。";
      hint.className = "hint warn";
    }
    hint.style.display = "";
  } catch (e) { console.warn("\u52a0\u8f7d\u5931\u8d25:", e); }
}

let _loginPolling = null;
async function doLogin() {
  if ($("#btn-login").disabled) return;
  const btn = $("#btn-login");
  const status = $("#login-status");
  btn.disabled = true;
  status.textContent = "\u6b63\u5728\u542f\u52a8\u6d4f\u89c8\u5668\u2026";
  try {
    const resp = await api("POST", "/api/login");
    const taskId = resp.task_id;
    if (_loginPolling) clearTimeout(_loginPolling);
    const poll = async () => {
      try {
        const s = await api("GET", `/api/login/status?task_id=${taskId}`);
        status.textContent = {
          starting: "\u6b63\u5728\u542f\u52a8\u6d4f\u89c8\u5668\u2026",
          installing_browser: "\u672a\u627e\u5230 Chrome\uff0c\u6b63\u5728\u4e0b\u8f7d Playwright Chromium\u2026",
          waiting: "\u8bf7\u5728\u5f39\u51fa\u7a97\u53e3\u4e2d\u5b8c\u6210\u767b\u5f55\u2026",
        }[s.status] || s.status;
        if (s.status === "done") {
          clearTimeout(_loginPolling);
          _loginPolling = null;
          status.textContent = "\u767b\u5f55\u6210\u529f\uff01Cookie \u5df2\u81ea\u52a8\u4fdd\u5b58\u3002";
          btn.disabled = false;
          toast("Bilibili \u767b\u5f55\u6210\u529f");
          await loadCookieStatus();
          await loadConfiguration(true);
        } else if (s.error) {
          clearTimeout(_loginPolling);
          _loginPolling = null;
          status.textContent = "\u767b\u5f55\u5931\u8d25: " + esc(s.error);
          btn.disabled = false;
        }
      } catch (e) {
        clearTimeout(_loginPolling);
        _loginPolling = null;
        status.textContent = "\u72b6\u6001\u67e5\u8be2\u5f02\u5e38: " + esc(e.message);
        btn.disabled = false;
      }
      if (btn.disabled) _loginPolling = setTimeout(poll, 2000);
    };
    _loginPolling = setTimeout(poll, 2000);
  } catch (e) {
    status.textContent = "\u542f\u52a8\u5931\u8d25: " + esc(e.message);
    btn.disabled = false;
  }
}

async function clearCookie() {
  if ($("#btn-login").disabled) return toast("请先完成当前登录，再清空 Cookie。");
  if (!confirm("清空已保存 Cookie？需要登录态的取流功能将受影响，公开弹幕仍可匿名采集。")) return;
  try {
    await api("POST", "/api/login/clear");
    toast("Cookie 已清空。");
    await loadCookieStatus(); await loadConfiguration(true);
  } catch (error) { toast("清空失败：" + error.message); }
}

// ----------------------------- \u5b57\u5e55\u6a21\u677f(V0.1.8 P0) ----------------------------- //
async function loadTemplates() {
  const rows = await api("GET", "/api/templates");
  $("#templates-list").innerHTML = rows.length ? rows.map((t) => `
    <div class="item">
      <div class="head">
        <div>
          <div class="title">${esc(t.name)} ${t.is_default ? badge("\u9ed8\u8ba4") : ""}</div>
          <div class="sub">${esc(t.font_name || "")} ${t.font_size}px \u00b7 \u8f6e\u5ed3${t.outline} \u00b7 \u9634\u5f71${t.shadow} \u00b7 \u6bcf\u884c${t.max_chars_per_line}\u5b57</div>
        </div>
        <div class="actions">
          <button class="ok" onclick="exportTemplate(${t.id})">\u5bfc\u51fa .ass</button>
          <button onclick="detTempl(${t.id})">\u5220\u9664</button>
        </div>
      </div>
    </div>`).join("") : `<div class="empty">\u6682\u65e0\u6a21\u677f\u3002\u53ef\u5bfc\u5165 .ass \u6587\u4ef6\u6216\u65b0\u5efa\u9ed8\u8ba4\u6a21\u677f\u3002</div>`;
}

function exportTemplate(id) { window.open(`/api/templates/${id}/export`, "_blank"); }

async function detTempl(id) {
  if (!confirm("\u786e\u8ba4\u5220\u9664?")) return;
  try { await api("DELETE", `/api/templates/${id}`); loadTemplates(); }
  catch (e) { toast(e.message); }
}

// ----------------------------- \u7247\u5934\u7247\u5c3e\u6a21\u677f(V0.1.8 P1.2) ----------------------------- //
async function loadIntroTemplates() {
  const rows = await api("GET", "/api/intro-templates");
  $("#intro-templates-list").innerHTML = rows.length ? rows.map((t) => `
    <div class="item">
      <div class="head">
        <div>
          <div class="title">${esc(t.name)} ${t.is_default ? badge("\u9ed8\u8ba4") : ""}</div>
          <div class="sub">
            \u7247\u5934 ${t.intro_enabled ? esc(t.intro_text) + " (" + t.intro_duration_s + "s)" : "\u7981\u7528"}
            / \u7247\u5c3e ${t.outro_enabled ? esc(t.outro_text) + " (" + t.outro_duration_s + "s)" : "\u7981\u7528"}
          </div>
        </div>
        <div class="actions">
          <button onclick="detIntro(${t.id})">\u5220\u9664</button>
        </div>
      </div>
    </div>`).join("") : `<div class="empty">\u6682\u65e0\u6a21\u677f\u3002</div>`;
}

async function detIntro(id) {
  if (!confirm("\u786e\u8ba4\u5220\u9664?")) return;
  try { await api("DELETE", `/api/intro-templates/${id}`); loadIntroTemplates(); }
  catch (e) { toast(e.message); }
}

// ----------------------------- \u4e8b\u4ef6\u7ed1\u5b9a ----------------------------- //
$("#btn-add-llm").addEventListener("click", () => {
  const list = $("#llm-list");
  if (list.querySelector(".empty")) list.innerHTML = "";
  list.insertAdjacentHTML("beforeend", llmRow({ base_url: "https://api.deepseek.com/v1", model: "deepseek-chat", web_search_param: "enable_search", priority: 100 }));
  markLLMDirty();
});
$("#llm-list").addEventListener("click", (e) => {
  if (e.target.dataset.act === "del-llm") {
    e.target.closest(".llm-row").remove();
    markLLMDirty();
  }
});
$("#llm-list").addEventListener("input", markLLMDirty);
$("#llm-list").addEventListener("change", markLLMDirty);
$("#btn-save-llm").addEventListener("click", async () => {
  if (_llmSaving) return;
  _llmSaving = true;
  $("#btn-save-llm").disabled = true;
  try {
    const revision = _llmRevision;
    const r = await api("PUT", "/api/llm-providers", { providers: collectLLM() });
    toast(`\u5df2\u4fdd\u5b58 ${r.providers.length} \u4e2a\u6a21\u578b`);
    if (_llmRevision === revision) {
      _llmDirty = false;
      await loadLLM(true);
    }
  } catch (e) { $("#llm-status").textContent = "保存失败：" + e.message; toast("\u4fdd\u5b58\u5931\u8d25:" + e.message); }
  finally { _llmSaving = false; $("#btn-save-llm").disabled = false; }
});
$("#btn-test-llm").addEventListener("click", async () => {
  toast("\u6d4b\u8bd5\u4e2d,\u8bf7\u7a0d\u5019\u2026");
  try {
    const revision = _llmRevision;
    const r = await api("POST", "/api/llm-providers/test", { providers: collectLLM() });
    if (revision !== _llmRevision) { $("#llm-test-results").textContent = "测试期间配置已修改，请重新测试当前草稿。"; return; }
    renderLLMTestResults(r.results);
    if (!r.results.length) return toast("\u65e0\u53ef\u7528\u6a21\u578b(\u9700\u5df2\u542f\u7528\u4e14\u914d\u7f6e key)");
    const ok = r.results.filter((x) => x.ok).map((x) => x.name);
    const bad = r.results.filter((x) => !x.ok).map((x) => x.name);
    toast(`\u53ef\u7528:${ok.join("\u3001") || "\u65e0"}${bad.length ? " \u00b7 \u5931\u8d25:" + bad.join("\u3001") : ""}`);
  } catch (e) {
    $("#llm-test-results").innerHTML = `<span class="warn">\u8bf7\u6c42\u5931\u8d25\uff1a${esc(e.message)}</span>`;
    toast("\u6d4b\u8bd5\u5931\u8d25:" + e.message);
  }
});

$("#btn-add-template").addEventListener("click", async () => {
  try { await api("POST", "/api/templates"); toast("\u9ed8\u8ba4\u6a21\u677f\u5df2\u521b\u5efa"); loadTemplates(); }
  catch (e) { toast(e.message); }
});

$("#btn-import-ass").addEventListener("click", () => {
  document.getElementById("file-import-ass").click();
});

document.getElementById("file-import-ass").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch("/api/templates/import/ass", { method: "POST", body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "\u5bfc\u5165\u5931\u8d25");
    toast(`\u6210\u529f\u5bfc\u5165 ${data.imported.length} \u4e2a\u6837\u5f0f`);
    loadTemplates();
  } catch (err) {
    toast("\u5bfc\u5165\u5931\u8d25: " + err.message);
  }
  e.target.value = "";
});

$("#btn-add-intro").addEventListener("click", async () => {
  try { await api("POST", "/api/intro-templates"); toast("\u9ed8\u8ba4\u6a21\u677f\u5df2\u521b\u5efa"); loadIntroTemplates(); }
  catch (e) { toast(e.message); }
});

document.addEventListener("DOMContentLoaded", () => {
  const btnLogin = $("#btn-login");
  const btnClear = $("#btn-clear-cookie");
  if (btnLogin) btnLogin.addEventListener("click", doLogin);
  if (btnClear) btnClear.addEventListener("click", clearCookie);
});

export { loadLLM, collectLLM, llmRow, loadLogs, loadTasks, retryTask, cancelTask, loadCookieStatus, doLogin, clearCookie, loadTemplates, exportTemplate, detTempl, loadIntroTemplates, detIntro, hasLLMDraft };
