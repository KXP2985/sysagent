"""ModelRegistry 与模型 API 的单元测试。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentd.config import load_config  # noqa: E402
from agentd.models import ModelRegistry, _mask  # noqa: E402


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    cfg = load_config(ROOT / "config.example.yaml")
    cfg.data["data_dir"] = str(tmp)
    cfg.data["llm"]["provider"] = "ollama"

    reg = ModelRegistry(cfg)
    provs = reg.list_public()
    assert reg.active_id == "ollama"
    assert {p["id"] for p in provs} >= {"ollama", "openai-compat"}
    assert all("api_key" in p for p in provs)
    assert _mask("sk-abcdefg") == "***defg"

    # 添加云端模型
    e = reg.add("DeepSeek", "https://api.deepseek.com/v1", "sk-test123456", "deepseek-chat")
    assert e["id"].startswith("cloud-") and e["model"] == "deepseek-chat"
    listed = reg.list_public()
    mine = [p for p in listed if p["id"] == e["id"]][0]
    assert mine["api_key"] == "***3456" and mine["has_key"] and not mine["active"]

    # 切换默认
    reg.set_active(e["id"])
    assert reg.active_id == e["id"]
    kind, conf = reg.active_conf()
    assert kind == "openai_compatible" and conf["model"] == "deepseek-chat"
    p = reg.create_provider()
    assert p.model == "deepseek-chat"

    # 持久化重载
    reg2 = ModelRegistry(cfg)
    assert reg2.active_id == e["id"]
    assert any(x["id"] == e["id"] for x in reg2.list_public())

    # 删除后 active 回落
    assert reg2.remove(e["id"])
    assert reg2.active_id != e["id"]
    assert not reg2.remove(e["id"])  # 二次删除失败

    # 缺参拒绝
    try:
        reg2.add("bad", "", "", "")
        raise AssertionError("应拒绝空 base_url/model")
    except ValueError:
        pass

    print("ModelRegistry OK")


if __name__ == "__main__":
    main()
