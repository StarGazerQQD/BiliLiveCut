// BiliLiveCut \u5019\u9009\u5ba1\u6838:\u5217\u8868\u3001\u6279\u91cf\u64cd\u4f5c
import { $, $$, api, toast, esc, badge } from "./common.js";
import { approveCand, rejectCand, delCand } from "./review.js";

// ----------------------------- \u6e32\u67d3:\u5019\u9009\u5ba1\u6838 ----------------------------- //
async function loadCandidates() {
  const status = $("#cand-filter").value;
  const rows = await api("GET", `/api/candidates?limit=50${status ? "&status=" + status : ""}`);
  $("#candidates-list").innerHTML = rows.length ? rows.map((c) => `
    <div class="item">
      <div class="head">
        <div style="display:flex;align-items:center;gap:6px">
          <input type="checkbox" class="cand-check" data-id="${c.id}" />
          <div>
            <div class="title">\u5019\u9009 #${c.id} \u00b7 \u5206\u6570 ${c.highlight_score} ${badge(c.status)}</div>
            <div class="sub">\u89c4\u5219 ${c.rule_score} / LLM ${c.llm_score} \u00b7 ${esc(c.reason || "")}</div>
          </div>
        </div>
        <div class="actions">
          <a class="ok btn-link" href="/review/${c.id}" target="_blank" style="text-decoration:none;color:inherit">\ud83c\udfac \u5ba1\u7247</a>
          <button class="ok" onclick="approveCand(${c.id})">\u6279\u51c6\u5e76\u51fa\u7247</button>
          <button class="danger" onclick="rejectCand(${c.id})">\u62d2\u7edd</button>
          <button onclick="delCand(${c.id})">\u5220\u9664</button>
        </div>
      </div>
      <div class="score-bar"><span style="width:${Math.round(c.highlight_score * 100)}%"></span></div>
    </div>`).join("") : `<div class="empty">\u6682\u65e0\u5019\u9009\u3002</div>`;

  bindBatchCheckboxes();
}

// V0.1.8 P0:\u6279\u91cf\u64cd\u4f5c
function getCheckedCandidates() {
  return [...$$(".cand-check:checked")].map(cb => parseInt(cb.dataset.id));
}

function bindBatchCheckboxes() {
  $$(".cand-check").forEach(cb => {
    cb.addEventListener("change", () => {
      const checked = getCheckedCandidates();
      $("#batch-count").textContent = checked.length ? `\u5df2\u9009 ${checked.length} \u4e2a` : "";
      $("#select-all-candidates").checked = checked.length && checked.length === $$(".cand-check").length;
    });
  });
}

async function batchAction(action) {
  const ids = getCheckedCandidates();
  if (!ids.length) { toast("\u8bf7\u5148\u52fe\u9009\u5019\u9009"); return; }
  const label = { approve: "\u6279\u51c6", reject: "\u62d2\u7edd", publish: "\u53d1\u5e03", delete: "\u5220\u9664" }[action] || action;
  if (!confirm(`\u786e\u8ba4\u6279\u91cf${label} ${ids.length} \u4e2a\u5019\u9009?`)) return;
  const r = await api("POST", "/api/candidates/batch", { candidate_ids: ids, action });
  toast(`\u6210\u529f ${r.success.length} \u4e2a${r.failed.length ? `, \u5931\u8d25 ${r.failed.length} \u4e2a` : ""}`);
  loadCandidates();
}

// \u4e8b\u4ef6\u7ed1\u5b9a
$("#select-all-candidates").addEventListener("change", (e) => {
  const checked = e.target.checked;
  $$(".cand-check").forEach(cb => { cb.checked = checked; cb.dispatchEvent(new Event("change")); });
  $("#batch-count").textContent = checked ? `\u5df2\u9009 ${$$(".cand-check").length} \u4e2a` : "";
});

$("#btn-batch-approve").addEventListener("click", () => batchAction("approve"));
$("#btn-batch-reject").addEventListener("click", () => batchAction("reject"));
$("#btn-batch-publish").addEventListener("click", () => batchAction("publish"));

$("#cand-filter").addEventListener("change", loadCandidates);

export { loadCandidates, getCheckedCandidates, bindBatchCheckboxes, batchAction };
