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

    def test_is_final_sink(self):
        """全链最后一站：不能有 children，且其它根节点都应指向它。"""
        self.assertEqual(self.cfg["router"][SLUG]["children"], [],
                         "最后一站不能有下级，否则它不是终点")
        # 全局兜底 * 也是无 child 的终点节点，单独校验；其余 sink 仍只能是它
        sinks = [k for k, v in self.cfg["router"].items()
                 if not v.get("children") and k != "*"]
        self.assertEqual(sinks, [SLUG], "应当只有这一个 sink：%s" % sinks)
        catchAll = self.cfg["router"].get("*")
        self.assertIsNotNone(catchAll, "router.json 缺少全局兜底 * 节点")
        self.assertEqual(catchAll["provider"], "ark-official")
        self.assertEqual(catchAll.get("remote_model"), SLUG)

    def test_former_sinks_now_point_to_it(self):
        for name in ("glm-5-3", "zai-org/glm-5.3", "zai-org/glm-5.3-flash"):
            self.assertIn(SLUG, self.cfg["router"][name]["children"],
                          "%s 应把 %s 作为最后兜底" % (name, SLUG))

    def test_is_last_in_auto_downgrade_chain(self):
        """自动降级链（auto-continue 用的 model_chain）末位必须也是它。

        两套机制并存：DAG 管请求层降级，model_chain 管会话续跑/切档。
        「最后一站」要在两处同时成立，否则一方会在别的模型上停住。
        """
        import json as _json
        policy = _json.loads(
            (Path.home() / ".codex" / "model_policy.json").read_text(encoding="utf-8"))
        chain = policy.get("fallback") or []
        self.assertTrue(chain, "model_policy.fallback 不能为空")
        self.assertEqual(chain[-1], SLUG, "降级链末位应为 %s，实际 %s" % (SLUG, chain[-1]))
        self.assertEqual(len(chain), len(set(chain)), "降级链不应有重复模型")

    def test_every_gateway_has_health_path(self):
        """体检路径必须逐渠道显式配置（2026-11-16 四个渠道全红的根因）。"""
        for name, provider in self.cfg["providers"].items():
            self.assertTrue(provider.get("health_path"),
                            "%s 缺 health_path，体检会因拼错路径误报 404" % name)


if __name__ == "__main__":
    unittest.main()
