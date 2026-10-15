"""完整结构化契约应绕过 Windows npm shim 的命令行长度限制。"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import agent_worker


def test_verified_npm_shim_uses_declared_entry_and_native_node(tmp_path):
    package = tmp_path / "node_modules/@tencent-ai/codebuddy-code"
    (package / "bin").mkdir(parents=True)
    entry = package / "bin/codebuddy"
    entry.write_text("// fixture", encoding="utf-8")
    (package / "package.json").write_text(json.dumps({"name": "@tencent-ai/codebuddy-code", "bin": {"codebuddy": "bin/codebuddy"}}), encoding="utf-8")
    shim = tmp_path / "codebuddy.cmd"
    shim.write_text('@"%dp0%\\node.exe" "%dp0%\\node_modules\\@tencent-ai\\codebuddy-code\\bin\\codebuddy" %*', encoding="utf-8")
    node = tmp_path / "node.exe"
    node.write_bytes(b"fixture")
    assert agent_worker.codebuddy_command_prefix([str(shim), "--effort", "high"]) == [str(node), str(entry), "--effort", "high"]


def test_custom_command_is_not_reinterpreted(tmp_path):
    shim = tmp_path / "custom-codebuddy.cmd"
    shim.write_text("custom wrapper", encoding="utf-8")
    command = [str(shim), "--custom"]
    assert agent_worker.codebuddy_command_prefix(command) == command
