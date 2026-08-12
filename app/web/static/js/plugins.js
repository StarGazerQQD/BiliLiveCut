// BiliLiveCut 插件中心：发现、启停与设置页入口
import { $, api, toast, esc, badge } from "./common.js";

const pendingPluginIds = new Set();
let pluginMutationRevision = 0;

function hasPluginMutationPending() {
  return pendingPluginIds.size > 0;
}

function pluginRow(plugin) {
  const state = plugin.loaded ? "running" : plugin.error ? "error" : "disabled";
  return `
    <div class="item plugin-item" data-plugin-id="${esc(plugin.id)}">
      <div class="head">
        <div>
          <div class="title">${esc(plugin.name)} <span class="muted">v${esc(plugin.version)}</span> ${badge(state)}</div>
          <div class="sub">${esc(plugin.description || "无说明")}</div>
          ${plugin.error ? `<div class="sub plugin-error">${esc(plugin.error)}</div>` : ""}
        </div>
        <div class="actions plugin-actions">
          <label class="switch-row">
            <input class="plugin-toggle" type="checkbox" ${plugin.enabled ? "checked" : ""} /> 启用
          </label>
          <button class="plugin-settings" ${plugin.loaded && plugin.has_settings ? "" : "disabled"}>进入设置</button>
        </div>
      </div>
    </div>`;
}

function bindPluginActions(root) {
  root.querySelectorAll(".plugin-item").forEach((item) => {
    const pluginId = item.dataset.pluginId;
    const toggle = item.querySelector(".plugin-toggle");
    const settings = item.querySelector(".plugin-settings");
    toggle.addEventListener("change", async () => {
      pendingPluginIds.add(pluginId);
      pluginMutationRevision += 1;
      toggle.disabled = true;
      try {
        await api("PATCH", `/api/plugins/${encodeURIComponent(pluginId)}`, { enabled: toggle.checked });
        toast(toggle.checked ? "插件已启用" : "插件已停用");
      } catch (error) {
        toggle.checked = !toggle.checked;
        toast("插件状态更新失败：" + error.message);
      } finally {
        pendingPluginIds.delete(pluginId);
        pluginMutationRevision += 1;
        await loadPlugins();
      }
    });
    settings.addEventListener("click", () => {
      window.location.href = `/plugins/${encodeURIComponent(pluginId)}/settings`;
    });
  });
}

async function loadPlugins() {
  const root = $("#plugins-list");
  if (!root) return;
  const revision = pluginMutationRevision;
  try {
    const data = await api("GET", "/api/plugins");
    if (pendingPluginIds.size > 0 || pluginMutationRevision !== revision) return;
    const errors = (data.scan_errors || []).map((error) => `
      <div class="item plugin-scan-error">
        <div class="title">无法读取 ${esc(error.directory)}</div>
        <div class="sub plugin-error">${esc(error.error)}</div>
      </div>`).join("");
    root.innerHTML = (data.plugins || []).length
      ? data.plugins.map(pluginRow).join("") + errors
      : `${errors}<div class="empty">插件目录中尚未发现有效的 plugin.json。</div>`;
    bindPluginActions(root);
  } catch (error) {
    if (pendingPluginIds.size > 0 || pluginMutationRevision !== revision) return;
    root.innerHTML = `<div class="empty">插件列表加载失败：${esc(error.message)}</div>`;
  }
}

async function refreshPlugins() {
  try {
    await api("POST", "/api/plugins/refresh");
    toast("插件目录已重新扫描");
    await loadPlugins();
  } catch (error) {
    toast("重新扫描失败：" + error.message);
  }
}

$("#btn-refresh-plugins")?.addEventListener("click", refreshPlugins);

export { loadPlugins, hasPluginMutationPending };
