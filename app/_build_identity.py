import json
from importlib.metadata import PackageNotFoundError, distribution

from app._source_identity import RELEASE_VERSION, SOURCE_COMMIT, VERSION_LABEL

if not SOURCE_COMMIT:
    try:
        installed = distribution("bili-live-cut")
    except PackageNotFoundError:
        installed = None
    if installed is not None and (receipt := installed.read_text("blc-release.json")) is not None:
        identity = json.loads(receipt)
        RELEASE_VERSION = identity["RELEASE_VERSION"]
        VERSION_LABEL = identity["VERSION_LABEL"]
        SOURCE_COMMIT = identity["SOURCE_COMMIT"]
