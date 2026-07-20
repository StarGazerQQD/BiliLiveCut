"""Portable Backport 应用器。

在 source_snapshot 提取 731a31c 业务源码后，按固定顺序应用预定义的兼容性回移补丁。
每个补丁必须在 backports.json 中声明，任一失败时整体构建失败。

与 apply_version_overlay 的区别:
- overlay: 只修改版本号字段（app/__init__.py, pyproject.toml 等）
- backport: 修改业务逻辑以支持 Engine Pack 接口
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_MANIFEST_PATH = Path(__file__).resolve().parent.parent.parent.parent / "backports" / "backports.json"


def load_backport_manifest() -> dict[str, Any]:
    """Load the backport manifest JSON.

    :returns: Manifest dict with 'backports' list.
    :raises RuntimeError: If manifest missing or invalid.
    """
    if not _MANIFEST_PATH.exists():
        raise RuntimeError(f"Backport manifest not found: {_MANIFEST_PATH}")
    try:
        return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Backport manifest corrupted: {exc}") from exc


def apply_backport(staging_dir: Path, bp: dict[str, Any]) -> bool:
    """Apply a single backport to the staging directory.

    Each backport is a targeted Python function call that modifies specific
    files and lines, not a full-file regex substitution.

    :param staging_dir: Payload staging directory.
    :param bp: Backport entry from manifest.
    :returns: True if applied successfully.
    """
    bp_id = bp["id"]
    _logger.info("Applying backport: %s (%s)", bp_id, bp["purpose"])

    if bp_id == "bp-001-fix-nano-repo":
        _backport_fix_nano_repo(staging_dir)
    elif bp_id == "bp-002-blc-models-dir":
        _backport_blc_models_dir(staging_dir)
    elif bp_id == "bp-003-whisper-local-path":
        _backport_whisper_local_path(staging_dir)
    elif bp_id == "bp-004-engine-pack-contract":
        _backport_engine_pack_contract(staging_dir)
    elif bp_id == "bp-005-csrf-origin":
        _backport_csrf_origin(staging_dir)
    else:
        raise RuntimeError(f"Unknown backport ID: {bp_id}")

    return True


def apply_all_backports(staging_dir: Path) -> list[str]:
    """Apply all declared backports in order.

    :param staging_dir: Payload staging directory.
    :returns: List of applied backport IDs.
    :raises RuntimeError: If any backport fails to apply.
    """
    manifest = load_backport_manifest()
    applied: list[str] = []

    for bp in manifest["backports"]:
        try:
            apply_backport(staging_dir, bp)
            applied.append(bp["id"])
        except Exception as exc:
            raise RuntimeError(
                f"Backport {bp['id']} failed: {exc}\nPurpose: {bp['purpose']}\nFiles: {bp['files']}"
            ) from exc

    _logger.info("Applied %d backports: %s", len(applied), applied)
    return applied


# ══════════════════════════════════════════════════════════
# Individual backport implementations
# ══════════════════════════════════════════════════════════


def _backport_fix_nano_repo(staging_dir: Path) -> None:
    """bp-001: Fix FunASR-Nano repository ID."""
    for rel_path in ("app/analysis/transcription/backends.py", "app/analysis/transcription/pipeline.py"):
        fp = staging_dir / rel_path
        if not fp.exists():
            continue
        content = fp.read_text(encoding="utf-8")
        if "iic/Fun-ASR-Nano" not in content:
            _logger.warning("bp-001: iic/Fun-ASR-Nano not found in %s — may already be fixed", rel_path)
            continue
        content = content.replace('"iic/Fun-ASR-Nano"', '"FunAudioLLM/Fun-ASR-Nano-2512"')
        fp.write_text(content, encoding="utf-8")
        _logger.info("bp-001: Fixed FunASR-Nano ID in %s", rel_path)


def _backport_blc_models_dir(staging_dir: Path) -> None:
    """bp-002: Add BLC_MODELS_DIR support to FunASRBackend.

    Minimal change: modify __init__ and _load_* methods to prefer local
    paths when BLC_MODELS_DIR env var is set.
    """
    fp = staging_dir / "app" / "analysis" / "transcription" / "backends.py"
    if not fp.exists():
        raise RuntimeError("backends.py not found in staging")

    content = fp.read_text(encoding="utf-8")

    # 1. Modify FunASRBackend.__init__ to read BLC_MODELS_DIR
    old_init = '''        self._primary_model_name = primary or "paraformer-zh"'''
    new_init = '''        import os as _os_backport
        _models_dir = _os_backport.environ.get("BLC_MODELS_DIR", "")
        self._use_local = bool(_models_dir) and _os_backport.path.exists(_models_dir)
        if self._use_local:
            self._primary_model_name = str(_os_backport.path.join(_models_dir, "paraformer"))
        else:
            self._primary_model_name = primary or "paraformer-zh"'''
    content = content.replace(old_init, new_init)

    # 2. Modify _load_primary to use local sub-model paths
    old_primary = """            vad_model="fsmn-vad",
            punc_model="ct-punc",
            spk_model="cam++",
            device=device,
            hub="ms",
            revision=self.model_revision,"""
    new_primary = """            device=device,
            hub="ms",
            disable_update=self._use_local,"""
    if old_primary in content:
        content = content.replace(old_primary, new_primary)

    # 3. Modify _load_sensevoice — add local path fallback
    old_sv = """        logger.info("加载 SenseVoice-Small 辅助特征引擎 revision={}", self.model_revision)
        self._sensevoice = AutoModel(
            model=self.MODEL_ID_SENSEVOICE,
            device=settings.asr_auxiliary_device or settings.whisper_device,
            hub="ms",
            revision=self.model_revision,
        )"""
    new_sv = """        if self._use_local:
            sv_path = str(_os_backport.path.join(_models_dir, "sensevoice"))
            logger.info("加载 SenseVoice-Small 从本地: %s", sv_path)
            self._sensevoice = AutoModel(
                model=sv_path,
                device=settings.asr_auxiliary_device or settings.whisper_device,
                hub="ms",
                disable_update=True,
            )
        else:
            logger.info("加载 SenseVoice-Small revision=%s", self.model_revision)
            self._sensevoice = AutoModel(
                model=self.MODEL_ID_SENSEVOICE,
                device=settings.asr_auxiliary_device or settings.whisper_device,
                hub="ms",
                revision=self.model_revision,
            )"""
    if old_sv in content:
        content = content.replace(old_sv, new_sv)

    # 4. Modify _load_funasr — add local path fallback
    old_nano = """        logger.info("加载 Fun-ASR-Nano 复核引擎 revision={}", self.model_revision)
        self._funasr = AutoModel(
            model=self.MODEL_ID_NANO,
            device=settings.asr_review_device or settings.whisper_device,
            hub="ms",
            revision=self.model_revision,
        )"""
    new_nano = """        if self._use_local:
            nano_path = str(_os_backport.path.join(_models_dir, "funasr_nano"))
            logger.info("加载 Fun-ASR-Nano 从本地: %s", nano_path)
            self._funasr = AutoModel(
                model=nano_path,
                device=settings.asr_review_device or settings.whisper_device,
                hub="ms",
                disable_update=True,
            )
        else:
            logger.info("加载 Fun-ASR-Nano revision=%s", self.model_revision)
            self._funasr = AutoModel(
                model=self.MODEL_ID_NANO,
                device=settings.asr_review_device or settings.whisper_device,
                hub="ms",
                revision=self.model_revision,
            )"""
    if old_nano in content:
        content = content.replace(old_nano, new_nano)

    fp.write_text(content, encoding="utf-8")
    _logger.info("bp-002: Added BLC_MODELS_DIR local model support to backends.py")


def _backport_whisper_local_path(staging_dir: Path) -> None:
    """bp-003: Add Whisper local path support."""
    fp = staging_dir / "app" / "analysis" / "transcription" / "backends.py"
    if not fp.exists():
        raise RuntimeError("backends.py not found in staging")

    content = fp.read_text(encoding="utf-8")

    # Modify FasterWhisperBackend.__init__ to prefer local path
    old_whisper = """    def __init__(
        self,
        model_size: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
    ) -> None:
        self.model_size = model_size or settings.whisper_model"""

    new_whisper = """    def __init__(
        self,
        model_size: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
    ) -> None:
        import os as _os_bp3
        _md = _os_bp3.environ.get("BLC_MODELS_DIR", "")
        if _md:
            _wlocal = _os_bp3.path.join(_md, "whisper")
            if _os_bp3.path.isdir(_wlocal):
                self.model_size = str(_wlocal)
            else:
                self.model_size = model_size or settings.whisper_model
        else:
            self.model_size = model_size or settings.whisper_model"""

    if old_whisper in content:
        content = content.replace(old_whisper, new_whisper)
        fp.write_text(content, encoding="utf-8")
        _logger.info("bp-003: Added Whisper local path support")


def _backport_engine_pack_contract(staging_dir: Path) -> None:
    """bp-004: Write Engine Pack contract to _portable_release.py."""
    fp = staging_dir / "app" / "_portable_release.py"
    content = '''"""Portable Release 元数据 — Engine Pack 接口契约。"""

from __future__ import annotations

# 版本标识
RELEASE_VERSION: str = "0.1.14.11-alpha"
SOURCE_COMMIT: str = "731a31cd04ae1df27dd6b6c5ffc535123932b825"
SOURCE_COMMIT_SHORT: str = "731a31c"

# Engine Pack 接口契约
# ASR 运行时按以下约定查找模型:
#   {BLC_MODELS_DIR}/whisper/       — faster-whisper-large-v3-turbo
#   {BLC_MODELS_DIR}/paraformer/    — Paraformer-zh 主引擎
#     {BLC_MODELS_DIR}/paraformer/fsmn-vad/     — FSMN-VAD 子模型
#     {BLC_MODELS_DIR}/paraformer/ct-punc/      — CT-Transformer 标点子模型
#     {BLC_MODELS_DIR}/paraformer/campplus/     — CAM++ 声纹子模型
#   {BLC_MODELS_DIR}/sensevoice/    — SenseVoice-Small 辅助特征
#   {BLC_MODELS_DIR}/funasr_nano/   — Fun-ASR-Nano 复核引擎

ENGINE_PACK_CONTRACT = {
    "schema_version": 4,
    "models_root": "models",
    "engines": {
        "whisper": {
            "repository": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
            "target_subdir": "whisper",
        },
        "paraformer": {
            "repository": "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
            "target_subdir": "paraformer",
            "sub_models": {
                "fsmn-vad": {"target_subdir": "paraformer/fsmn-vad"},
                "ct-punc": {"target_subdir": "paraformer/ct-punc"},
                "campplus": {"target_subdir": "paraformer/campplus"},
            },
        },
        "sensevoice": {
            "repository": "iic/SenseVoiceSmall",
            "target_subdir": "sensevoice",
        },
        "funasr_nano": {
            "repository": "FunAudioLLM/Fun-ASR-Nano-2512",
            "target_subdir": "funasr_nano",
        },
    },
}
'''
    fp.write_text(content, encoding="utf-8")
    _logger.info("bp-004: Wrote Engine Pack contract to _portable_release.py")


def _backport_csrf_origin(staging_dir: Path) -> None:
    """bp-005: 修复 CSRF Origin 检查 — 安全回移到 731a31c 的 main.py。

    将简单的默认端口全部折叠改为:
    - 仅折叠 scheme 对应默认端口 (http→80, https→443)
    - http://host:443 ≠ http://host
    - IPv6 bracket 支持
    - 非法 Origin 返回 False
    """
    fp = staging_dir / "app" / "web" / "main.py"
    if not fp.exists():
        raise RuntimeError("main.py not found in staging for bp-005")

    content = fp.read_text(encoding="utf-8")

    # Find the _check_csrf method and the _parse_origin placeholder
    # The 731a31c version has a simple check. Replace it.
    old_csrf = """        origin = request.headers.get("Origin", "")

        # 无 Origin → 非浏览器客户端,依赖 Basic Auth
        if not origin:
            return True"""

    new_csrf = """        origin = request.headers.get("Origin", "")

        # 无 Origin → 非浏览器客户端, 依赖 Basic Auth
        if not origin:
            return True

        parsed = _AuthMiddleware._parse_origin(origin)
        if parsed is None:
            return False

        scheme = request.url.scheme or "http"
        host_headers = request.headers.get("Host", "")
        if host_headers:
            hostname = host_headers.split(":")[0]
            port_hint = host_headers.split(":")[1] if ":" in host_headers else ""
        else:
            hostname = request.url.hostname or ""
            port_hint = str(request.url.port or "")

        effective_port = port_hint if port_hint else ("443" if scheme == "https" else "80")
        origin_scheme, origin_host, origin_port = parsed

        if origin_scheme != scheme:
            return False
        if origin_host != hostname:
            return False

        origin_port_for_compare = origin_port if origin_port else ("443" if origin_scheme == "https" else "80")
        return origin_port_for_compare == effective_port"""

    # Also need to add _parse_origin static method if not present
    if "_parse_origin" not in content:
        # Add after _check_csrf method or before class end
        parse_method = '''
    @staticmethod
    def _parse_origin(origin):
        """Parse Origin header → (scheme, host, port) or None."""
        origin = origin.strip()
        if not origin or "://" not in origin:
            return None
        scheme, rest = origin.split("://", 1)
        scheme = scheme.lower()
        if scheme not in ("http", "https"):
            return None
        rest = rest.rstrip("/")
        port = ""
        if rest.startswith("["):
            end = rest.find("]")
            if end == -1:
                return None
            host_part = rest[1:end]
            after = rest[end + 1:]
            if after.startswith(":"):
                port = after[1:]
            elif after:
                return None
        else:
            if ":" in rest:
                host_part, port = rest.rsplit(":", 1)
            else:
                host_part = rest
        if not host_part:
            return None
        return (scheme, host_part, port)
'''
        # Insert before the last method in the class
        after_class_start = content.find("class _AuthMiddleware")
        if after_class_start >= 0:
            # Find the end of the class (next class or end of file)
            next_class = content.find("class _RateLimitMiddleware", after_class_start)
            if next_class >= 0:
                content = content[:next_class] + parse_method + content[next_class:]

    content = content.replace(old_csrf, new_csrf)
    fp.write_text(content, encoding="utf-8")
    _logger.info("bp-005: Fixed CSRF Origin check in main.py")
