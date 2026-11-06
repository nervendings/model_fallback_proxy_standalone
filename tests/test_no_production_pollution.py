"""守卫：测试不得写入生产状态目录。

2026-11-06 实测故障：多个用例只传 state_path，其余路径落默认值
~/.local/state/codex-session-auto-continue，把 session_id=00000000 的
测试事件写进生产 events.jsonl（累计 7 条），污染真实告警流水。

本测试用「空临时目录 + 默认路径断言」双重手段守卫：
1. 静态：tests/ 里任何 SessionAutoContinue(...) 构造都必须显式给 events_path；
2. 动态：跑一遍全量测试前后，生产 events.jsonl 大小不得变化。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"


class NoProductionPollutionTest(unittest.TestCase):
    def test_every_watcher_construction_sets_events_path(self) -> None:
        offenders: list[str] = []
        for path in TESTS.glob("test_*.py"):
            if path.name == Path(__file__).name:
                continue
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r"SessionAutoContinue\(", source):
                # 取该构造的括号块
                depth = 0
                end = match.end()
                for index in range(match.end() - 1, len(source)):
                    if source[index] == "(":
                        depth += 1
                    elif source[index] == ")":
                        depth -= 1
                        if depth == 0:
                            end = index
                            break
                block = source[match.end():end]
                if "events_path" not in block:
                    line = source[: match.start()].count("\n") + 1
                    offenders.append(f"{path.name}:{line}")
        self.assertEqual(offenders, [], f"这些构造缺 events_path，会写生产 events.jsonl: {offenders}")


if __name__ == "__main__":
    unittest.main()
