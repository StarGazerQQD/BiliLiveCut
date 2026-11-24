from pathlib import Path
from runpy import run_path

root = Path(__file__).resolve().parents[1]
if (root / ".git").exists():
    config = run_path(str(root / "packaging/portable/config/version_loader.py"))
    RELEASE_VERSION = config["RELEASE_VERSION"]
    VERSION_LABEL = config["VERSION_LABEL"]
    SOURCE_COMMIT = config["SOURCE_COMMIT_FULL"]
else:
    embedded = run_path(str(root / "app/_source_identity.py"))
    RELEASE_VERSION = embedded["RELEASE_VERSION"]
    VERSION_LABEL = embedded["VERSION_LABEL"]
    SOURCE_COMMIT = embedded["SOURCE_COMMIT"]
