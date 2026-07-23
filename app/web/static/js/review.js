// BiliLiveCut \u5ba1\u6838:\u6279\u51c6/\u62d2\u7edd/\u5220\u9664\u5019\u9009\u7247\u6bb5
import { api, toast } from "./common.js";

async function approveCand(id) {
  toast("\u6b63\u5728\u63d0\u4ea4\u540e\u53f0\u51fa\u7247\u4f5c\u4e1a\u2026");
  try {
    const r = await api("POST", `/api/candidates/${id}/approve`);
    toast("\u51fa\u7247\u4f5c\u4e1a\u5df2\u63d0\u4ea4:" + r.job.id.substring(0, 8));
    const { loadCandidates } = await import("./candidates.js");
    loadCandidates();
  } catch (e) { toast("\u51fa\u7247\u5931\u8d25:" + e.message); }
}

async function rejectCand(id) {
  try {
    await api("POST", `/api/candidates/${id}/reject`);
    toast("\u5df2\u62d2\u7edd");
    const { loadCandidates } = await import("./candidates.js");
    loadCandidates();
  } catch (e) { toast(e.message); }
}

async function delCand(id) {
  try {
    await api("DELETE", `/api/candidates/${id}`);
    toast("\u5df2\u5220\u9664");
    const { loadCandidates } = await import("./candidates.js");
    loadCandidates();
  } catch (e) { toast(e.message); }
}

export { approveCand, rejectCand, delCand };
