"""P3 封面候选优化。

从成品视频抽取多帧封面,并按质量评分排序:
- 亮度适中的帧优先;
- 拉普拉斯方差检测模糊;
- 可选 OpenCV 人脸检测(需要 cv2 可用);
- 返回 Top N 封面路径及质量分数。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from app.core.config import settings


def extract_cover_candidates(
    video_path: str | Path,
    count: int = 5,
    out_dir: str | Path | None = None,
) -> list[dict]:
    """从视频中抽取多帧封面候选并按质量排序。

    :param video_path: 视频文件路径。
    :param count: 返回的封面数。
    :param out_dir: 输出目录(默认临时目录)。
    :returns: ``[{file_path, score, blur_score, brightness}]`` 列表(按 score 降序)。
    """
    vp = Path(video_path)
    if not vp.exists():
        logger.warning("视频文件不存在: {}", video_path)
        return []

    # 获取视频时长。
    duration_s = _probe_duration(vp)
    if duration_s <= 0:
        return []

    # 计算抽帧时间点(避开开头 1s 和结尾 1s)。
    usable = max(1, duration_s - 2)
    step = usable / (count * 3)  # 抽取 3x 候选帧再筛选
    timestamps = [1.0 + i * step for i in range(count * 3)]

    own_tmp = out_dir is None
    out = None
    try:
        out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="blc_covers_"))
    except Exception:
        logger.warning("无法创建封面输出目录,回退到临时目录")
        out = Path(tempfile.mkdtemp(prefix="blc_covers_"))
    out.mkdir(parents=True, exist_ok=True)

    try:
        candidates = []
        for i, ts in enumerate(timestamps):
            cover_path = out / f"cover_{i:03d}.jpg"
            try:
                subprocess.run(
                    [
                        settings.ffmpeg_path,
                        "-y",
                        "-v",
                        "quiet",
                        "-ss",
                        f"{ts:.3f}",
                        "-i",
                        str(vp),
                        "-vframes",
                        "1",
                        "-q:v",
                        "2",
                        str(cover_path),
                    ],
                    check=True,
                    timeout=10,
                )
                if cover_path.exists() and cover_path.stat().st_size > 1000:
                    blur = _detect_blur(cover_path)
                    brightness = _detect_brightness(cover_path)
                    face_score = _detect_face_score(cover_path)
                    blur_norm = max(0, min(1, blur / 500))
                    bright_dist = abs(brightness - 128) / 128
                    score = blur_norm * 0.5 + (1 - bright_dist) * 0.3 + face_score * 0.2
                    candidates.append(
                        {
                            "file_path": str(cover_path),
                            "score": round(score, 3),
                            "blur_score": round(blur, 1),
                            "brightness": round(brightness, 1),
                            "timestamp_s": round(ts, 1),
                        }
                    )
            except subprocess.CalledProcessError:
                continue

        # 按综合分降序,取 Top N。
        candidates.sort(key=lambda c: -c["score"])
        result = candidates[:count]

        # 若是自动创建的临时目录,将 Top 候选复制到持久化位置并清理临时目录。
        if own_tmp and result:
            from app.core.paths import clips_dir

            persistent = clips_dir() / "covers"
            persistent.mkdir(parents=True, exist_ok=True)
            for c in result:
                src = Path(c["file_path"])
                dst = persistent / src.name
                import shutil as _shutil

                _shutil.copy2(src, dst)
                c["file_path"] = str(dst)

        return result
    finally:
        if own_tmp:
            import shutil as _shutil2

            _shutil2.rmtree(out, ignore_errors=True)


def _probe_duration(video_path: Path) -> float:
    """用 ffprobe 获取时长。"""
    import json

    try:
        result = subprocess.run(
            [settings.ffprobe_path, "-v", "quiet", "-print_format", "json", "-show_format", str(video_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        info = json.loads(result.stdout)
        return float(info.get("format", {}).get("duration", 0))
    except Exception:
        return 0


def _detect_blur(image_path: Path) -> float:
    """用拉普拉斯方差检测模糊(值越大越清晰)。"""
    try:
        import cv2
        import numpy as np

        img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return 0
        return float(cv2.Laplacian(img, cv2.CV_64F).var())
    except ImportError:
        # 无 cv2 时,用 PIL(NumPy 回退)。
        try:
            import numpy as np
            from PIL import Image

            img = Image.open(image_path).convert("L")
            arr = np.array(img, dtype=np.float64)
            # 简单的边缘检测:用 sobel-like 差分。
            dx = np.diff(arr, axis=1)
            dy = np.diff(arr, axis=0)
            return float(np.var(np.abs(dx)) + np.var(np.abs(dy)))
        except ImportError:
            return 100  # 无图像库,返回中等值
        except Exception:
            return 0  # 文件损坏或不存在


def _detect_brightness(image_path: Path) -> float:
    """检测平均亮度(0-255)。"""
    try:
        import cv2

        img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return 128
        return float(img.mean())
    except ImportError:
        try:
            import numpy as np
            from PIL import Image

            img = Image.open(image_path).convert("L")
            return float(np.array(img).mean())
        except ImportError:
            return 128
        except Exception:
            return 128


def _detect_face_score(image_path: Path) -> float:
    """检测是否有人脸(0=无,1=有)。"""
    try:
        import cv2

        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        img = cv2.imread(str(image_path))
        if img is None:
            return 0
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.1, 4)
        return 1.0 if len(faces) > 0 else 0.0
    except Exception:
        return 0.5  # 无法检测时返回中性值
