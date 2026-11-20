import subprocess
import sys
from importlib.metadata import version


def main(tag: str, commit: str) -> None:
    """在安装目录之外核对分发包、CLI 和源码提交。"""
    from app._build_identity import SOURCE_COMMIT

    release = tag.removeprefix("v").lower()
    base, sequence = release.split("-rc.")
    expected_package_version = f"{base}rc{sequence}"
    expected_label = f"{tag} ({commit[:8]})"
    output = subprocess.check_output([sys.executable, "-I", "-m", "app.cli", "version"], text=True).strip()
    installed = version("bili-live-cut")
    print(f"installed={installed} source={SOURCE_COMMIT!r} cli={output!r}", flush=True)
    if installed != expected_package_version:
        raise SystemExit(f"分发包版本不匹配，期望 {expected_package_version}")
    if SOURCE_COMMIT != commit:
        raise SystemExit(f"源码提交不匹配，期望 {commit}")
    if f"BiliLiveCut {release} / {expected_label}" not in output.splitlines():
        raise SystemExit(f"CLI 版本标识不匹配，期望 {expected_label}")


if __name__ == "__main__":
    main(*sys.argv[1:])
