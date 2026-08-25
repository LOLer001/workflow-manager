from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "scripts" / "orchestrator_hook.py"
SPEC = importlib.util.spec_from_file_location("workflow_manager_lean_policy", HOOK_PATH)
assert SPEC and SPEC.loader
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)


class LeanPolicyBoundaryTests(unittest.TestCase):
    def test_source_has_no_retired_generic_workflow_handlers(self) -> None:
        tree = ast.parse(HOOK_PATH.read_text(encoding="utf-8"))
        functions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertFalse(
            {
                "routing_context",
                "handle_coordination",
                "pressure_text",
                "runtime_route_escalation",
                "problem_correction",
                "problem_escalation",
                "handle_subagent_pretool",
            }
            & functions
        )

    def test_authorization_result_has_no_native_execution_policy(self) -> None:
        simple = HOOK.classify_prompt("修改 Parser.py 一个错字并运行已有单测")
        hard = HOOK.classify_prompt(
            "排查跨模块间歇性状态损坏，根因未知，迁移后完成回滚和集成验收"
        )
        for result in (simple, hard):
            for retired in (
                "label",
                "score",
                "recommended_agent_cap",
                "delegation_gate",
                "lane_signal",
                "workflow_shape",
                "execution_order",
                "future_token_range",
            ):
                self.assertNotIn(retired, result)
        self.assertEqual(simple["work_difficulty"], "simple")
        self.assertEqual(hard["work_difficulty"], "hard")

    def test_non_hard_context_explicitly_defers_to_native_codex(self) -> None:
        result = HOOK.classify_prompt("解释这个单文件解析器")
        context = HOOK.authorization_context(result)
        self.assertIn("Codex owns ordinary execution", context)
        self.assertNotIn("agent cap", context.lower())
        self.assertNotIn("pressure", context.lower())


if __name__ == "__main__":
    unittest.main()
