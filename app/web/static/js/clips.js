// BiliLiveCut \u6210\u54c1\u5207\u7247:\u5217\u8868\u3001\u53d1\u5e03\u3001\u52a0\u5165\u4e0a\u4f20\u961f\u5217\u3001\u62d2\u7edd
import { $, api, toast, esc, badge } from "./common.js";

// ----------------------------- \u6e32\u67d3:\u6210\u54c1\u5207\u7247 ----------------------------- //
async function loadClips() {
  const rows = await api("GET", "/api/clips?limit=50");
  $("#clips-list").innerHTML = rows.length ? rows.map((c) => `
    <div class="item">
      <div class="head">
        <div>
          <div class="title">${esc(c.title || "(\u65e0\u6807\u9898)")} ${badge(c.status)}</div>
          <div class="sub">#${c.id} \u00b7 ${c.duration_s ? c.duration_s.toFixed(0) + "s" : "-"} \u00b7 \u6807\u7b7e:${(c.tags || []).map(esc).join("\u3001")}</div>
        </div>
        <div class="actions">
          <button class="ok" onclick="publishClip(${c.id})">\u53d1\u5e03(\u7f6e ready)</button>
          <button onclick="enqueueClip(${c.id})">\u52a0\u5165\u4e0a\u4f20\u961f\u5217</button>
          <button class="danger" onclick="rejectClip(${c.id})">\u62d2\u7edd</button>
        </div>
      </div>
      <div class="txt sub">${esc(c.description || "")}</div>
      <video controls preload="none" poster="/api/clips/${c.id}/cover" src="/api/clips/${c.id}/video"></video>
    </div>`).join("") : `<div class="empty">\u6682\u65e0\u6210\u54c1\u5207\u7247\u3002</div>`;
}

async function publishClip(id) {
  try {
    const r = await api("POST", `/api/clips/${id}/publish`);
    toast(r.uploaded ? "\u5df2\u53d1\u5e03\u5e76\u8fdb\u5165\u4e0a\u4f20:" + r.task_status : "\u5df2\u7f6e ready \u5e76\u5bfc\u51fa\u6e05\u5355");
    loadClips();
  } catch (e) { toast(e.message); }
}

async function enqueueClip(id) {
  try {
    const r = await api("POST", `/api/clips/${id}/enqueue`);
    toast("\u4e0a\u4f20\u4f5c\u4e1a\u5df2\u63d0\u4ea4:" + r.job.id.substring(0, 8));
    const { loadUploads } = await import("./publishing.js");
    loadUploads();
  } catch (e) { toast(e.message); }
}

async function rejectClip(id) {
  try { await api("POST", `/api/clips/${id}/reject`); toast("\u5df2\u62d2\u7edd"); loadClips(); }
  catch (e) { toast(e.message); }
}

export { loadClips, publishClip, enqueueClip, rejectClip };
