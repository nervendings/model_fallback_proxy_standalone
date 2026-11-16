"""回归：本地新增的 DeepSeek V4.1 Flash 路由节点不能丢。

为什么锁这条（2026-11-16）：该模型是本机唯一走「方舟官方 /api/v3 按量计费」的
通道，靠 router.json 的节点定义生效，代码里没有任何引用。节点一旦被删或被改成
别的 provider，表现是「codex -p ark41 仍然能跑」，但实际悄悄走了 sub2api 的
deepseek-flash —— 用户可以接受降级，但不该在不知情时被换模型。
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from model_fallback_proxy import router_engine as engine  # noqa: E402

SLUG = "deepseek-v4-1-flash-261017"


class Ark41RouteTest(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads(
            (ROOT / "configs/router.json").read_text(encoding="utf-8"))

    def test_bundled_config_is_valid(self):
        self.assertEqual(engine.validateConfig(self.cfg), "")

    def test_node_exists_and_prefers_ark_official(self):
        node = self.cfg["router"].get(SLUG)
        self.assertIsNotNone(node, "router.json 缺少 %s 节点" % SLUG)
        self.assertEqual(node["provider"], "ark-official")
        self.assertEqual(node.get("remote_model") or SLUG, SLUG)

    def test_provider_points_to_official_api(self):
        provider = self.cfg["providers"]["ark-official"]
        self.assertEqual(provider["base_url"],
                         "https://ark.cn-beijing.volces.com/api/v3")
        self.assertEqual(provider["auth_mode"], "bearer")
        # 必须是专用 key，不能和 coding plan 共用 ARK_API_KEY
        self.assertEqual(provider["api_key_env"], "ARK_AI_API_KEY")

    def test_falls_back_to_company_chain(self):
        children = self.cfg["router"][SLUG]["children"]
        self.assertIn("deepseek-flash", children,
                      "方舟抖动时必须能回落公司链路，否则会话直接中断")

    def test_not_in_auto_downgrade_chain(self):
        """不进 CODEX_FALLBACK_MODEL_CHAIN：新模型不该改变现有档位策略。"""
        env = ROOT / "configs" / "model_chain.env"
        if not env.exists():
            self.skipTest("无 model_chain.env")
        self.assertNotIn(SLUG, env.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
