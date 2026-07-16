/* ML high guang mo xing (V0.1.14.8-HL-Alpha) */

async function triggerMLLearn() {
  const btn = document.getElementById('btn-ml-dash-learn');
  if (btn) { btn.disabled = true; btn.textContent = 'Learning...'; }
  try {
    const r = await api('POST', '/api/ml/self-learn');
    if (r.success) {
      const m = r.metrics || {};
      showToast('ML learn done! iter#' + r.iteration + ' samples:' + r.n_samples + ' AUC:' + m.auc + ' F1:' + m.f1);
    } else {
      showToast('ML learn failed: ' + (r.error || 'unknown'));
    }
    loadMLStatus();
  } catch (e) { showToast('ML learn failed: ' + e.message); }
  finally { if (btn) { btn.disabled = false; btn.textContent = 'Learn'; } }
}

async function loadMLStatus() {
  try {
    const s = await api('GET', '/api/ml/status');
    const el = document.getElementById('ml-dash-status');
    if (el) {
      if (s.model_available) {
        const m = s.last_metrics || {};
        el.textContent = 'Model ready v' + s.iteration + ' AUC=' + (m.auc||0).toFixed(3) + ' F1=' + (m.f1||0).toFixed(3) + ' ' + s.n_total_samples + ' samples';
        el.style.color = 'var(--green)';
      } else {
        el.textContent = 'Not trained. Approve candidates then click Learn or use: python -m app.cli ml-learn';
        el.style.color = 'var(--yellow)';
      }
    }
    const versions = await api('GET', '/api/ml/versions');
    const vEl = document.getElementById('ml-version-list');
    if (vEl && versions.length) {
      vEl.innerHTML = versions.map(function(v) {
        var badge = v.is_champion ? 'CHAMPION' : (v.is_shadow ? 'Shadow' : 'Archived');
        var m = v.metrics || {};
        return '<div>' + badge + ' v' + v.version + ' AUC:' + (m.auc||0).toFixed(3) + ' F1:' + (m.f1||0).toFixed(3) + ' ' + v.n_samples + 'samples ' + (v.created_at||'').substring(0,16) + '</div>';
      }).join('');
    }
  } catch (e) {}
}

async function triggerMLAudit() {
  try {
    const r = await api('POST', '/api/ml/audit');
    if (r.drifted) {
      showToast('DRIFT ALERT! PSI=' + r.psi);
    } else {
      showToast('Model normal PSI=' + r.psi);
    }
  } catch (e) { showToast('Audit failed: ' + e.message); }
}

window.triggerMLLearn = triggerMLLearn;
window.loadMLStatus = loadMLStatus;
window.triggerMLAudit = triggerMLAudit;
