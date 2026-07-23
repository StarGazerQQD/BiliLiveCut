// BiliLiveCut \u53d1\u5e03:\u4e0a\u4f20\u961f\u5217\u3001\u4e0a\u4f20\u5f00\u5173\u3001\u901a\u77e5\u8f6e\u8be2
import { $, api, toast, esc, badge } from "./common.js";

let switchesDirty = false;
let lastNotifyId = 0;

// ----------------------------- \u6e32\u67d3:\u4e0a\u4f20\u961f\u5217 ----------------------------- //
async function loadUploads() {
  const s = await api("GET", "/api/settings");
  if (!switchesDirty) {
    $("#sw-biliup").checked = s.biliup_enabled;
    $("#sw-auto").checked = s.auto_upload;
  }
  $("#clips-dir-path").textContent = s.clips_dir;
  $("#upload-hint").textContent = s.upload_active
    ? "\u4e0a\u4f20\u6a21\u5757:\u5df2\u5f00\u542f(biliup)\u3002" + (s.biliup_cmd_configured ? "" : " \u4f46\u672a\u914d\u7f6e BILIUP_UPLOAD_CMD,\u4e0a\u4f20\u4f1a\u5b89\u5168\u5931\u8d25\u3002")
    : "\u4e0a\u4f20\u6a21\u5757:\u5df2\u5173\u95ed\u3002\u76f4\u64ad\u7ed3\u675f\u5c06\u5f39\u51fa\u5207\u7247\u76ee\u5f55,\u6210\u54c1\u4ec5\u5bfc\u51fa\u5f85\u4e0a\u4f20\u6e05\u5355\u3002";

  const rows = await api("GET", "/api/uploads?limit=50");
  $("#uploads-list").innerHTML = rows.length ? rows.map((t) => `
    <div class="item">
      <div class="head">
        <div>
          <div class="title">\u4efb\u52a1 #${t.id} \u00b7 clip ${t.clip_id} ${badge(t.status)}</div>
          <div class="sub">\u4e0a\u4f20\u5668 ${esc(t.uploader)} \u00b7 \u5c1d\u8bd5 ${t.attempts} \u6b21 ${t.remote_id ? "\u00b7 " + esc(t.remote_id) : ""}</div>
          ${t.last_error ? `<div class="sub" style="color:var(--red)">${esc(t.last_error)}</div>` : ""}
        </div>
        <div class="actions">
          <button onclick="retryUpload(${t.id})">\u91cd\u8bd5</button>
        </div>
      </div>
    </div>`).join("") : `<div class="empty">\u6682\u65e0\u4e0a\u4f20\u4efb\u52a1\u3002</div>`;
}

async function saveSwitch() {
  switchesDirty = true;
  try {
    await api("PATCH", "/api/settings", {
      biliup_enabled: $("#sw-biliup").checked,
      auto_upload: $("#sw-auto").checked,
    });
    toast("\u5df2\u4fdd\u5b58\u4e0a\u4f20\u5f00\u5173");
  } catch (e) { toast(e.message); }
  finally { switchesDirty = false; }
  loadUploads();
}

async function retryUpload(id) {
  try { const r = await api("POST", `/api/uploads/${id}/retry`); toast("\u91cd\u8bd5\u4f5c\u4e1a\u5df2\u63d0\u4ea4:" + r.job.id.substring(0, 8)); loadUploads(); }
  catch (e) { toast(e.message); }
}

// ----------------------------- \u901a\u77e5(\u76f4\u64ad\u7ed3\u675f\u5f39\u76ee\u5f55\u7b49) ----------------------------- //
async function pollNotifications() {
  try {
    const rows = await api("GET", `/api/notifications?since_id=${lastNotifyId}`);
    for (const n of rows) {
      lastNotifyId = Math.max(lastNotifyId, n.id);
      toast(n.message);
      if (n.data && n.data.clips_dir) {
        console.info("\u5207\u7247\u76ee\u5f55:", n.data.clips_dir);
      }
    }
  } catch (e) { console.warn("\u52a0\u8f7d\u5931\u8d25:", e); }
}

// \u5f00\u5173\u4e8b\u4ef6
$("#sw-biliup").addEventListener("change", saveSwitch);
$("#sw-auto").addEventListener("change", saveSwitch);

$("#btn-open-dir").addEventListener("click", async () => {
  try { const r = await api("POST", "/api/open-clips-dir"); toast("\u5df2\u6253\u5f00:" + r.clips_dir); }
  catch (e) { toast(e.message); }
});

export { loadUploads, saveSwitch, retryUpload, pollNotifications, lastNotifyId };
