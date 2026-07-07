"""构建 Payload 的快捷入口。"""
import sys

sys.path.insert(0, "packaging/portable")
sys.path.insert(0, ".")

from build_payload import build_payload

report = build_payload()
print("=== BUILD REPORT ===")
for k, v in report.items():
    print(f"  {k}: {v}")
print("=== BUILD SUCCESS ===")
