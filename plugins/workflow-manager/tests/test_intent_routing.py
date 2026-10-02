"""Behavioral regressions for execution intent versus supplied source material.

Examples are synthetic.  They exercise public routing and hook state behavior,
without depending on the implementation's internal classification view.
"""

from __future__ import annotations

import json
import unittest

import test_orchestrator_hook as existing


HOOK = existing.HOOK

DIMENSION_TABLE = (
    "维度 | 判断什么 | 检查点\n"
    "目标 | 明确目标 | 源码与 APK 的对应关系\n"
    "范围 | 影响范围 | 模块与架构边界\n"
    "风险 | 判断风险 | 生产部署、安全与不可逆外部动作\n"
    "依赖 | 分析依赖 | 编译、安装与设备验证\n"
    "证据 | 检查证据 | 未知根因与跨模块排查\n"
    "验收 | 明确验收 | 以参考为准复刻效果"
)


class IntentRoutingTests(unittest.TestCase):
    def assert_native(self, prompt: str) -> dict:
        route = HOOK.classify_prompt(prompt)
        self.assertNotEqual(route["work_difficulty"], "hard", prompt)
        self.assertEqual(route["model_profile"], "current", prompt)
        return route

    def test_unseparated_markdown_table_with_instruction_on_final_row(self) -> None:
        for table in (DIMENSION_TABLE, "\n".join("| " + row + " |" for row in DIMENSION_TABLE.splitlines())):
            for suffix in (
                " 用这6个维度吧 然后给我生成一个md文档",
                "\n用这6个维度吧，然后给我生成一个 Markdown 文档",
            ):
                with self.subTest(table=table, suffix=suffix):
                    route = self.assert_native(table + suffix)
                    self.assertEqual(route["phase_hints"], [])
                    self.assertFalse(HOOK.reference_requested(table + suffix))

    def test_document_requests_before_and_after_source_material(self) -> None:
        material = (
            "资料：排查未知根因的跨模块生产故障；编译源码，安装 APK，"
            "正式部署并验证；不可逆数据库迁移需安全审查。"
        )
        for request in (
            "给我生成一个 md 文档",
            "请生成 PDF 文档",
            "请整理成 Word 文档",
            "生成一个 LaTeX 文档",
            "整理成周报",
            "润色这份草稿",
            "把这些资料翻译成英文",
            "Write a Markdown document",
            "Generate a PDF report",
            "Create a Word document",
            "Prepare a LaTeX document",
            "Polish this draft",
            "Translate this source material into English",
        ):
            for prompt in (request + "：\n" + material, material + "\n" + request):
                with self.subTest(prompt=prompt):
                    route = self.assert_native(prompt)
                    self.assertEqual(route["phase_hints"], [])

    def test_chinese_adjacent_document_format_aliases_are_native(self) -> None:
        for kind in ("md", "MD", "PDF", "Word", "docx", "LaTeX", "PPT", "pptx"):
            for request in ("生成" + kind + "文件", "给我生成一个" + kind):
                prompt = DIMENSION_TABLE + "\n" + request
                with self.subTest(request=request):
                    route = self.assert_native(prompt)
                    self.assertEqual(route["phase_hints"], [])
                    self.assertFalse(HOOK.reference_requested(prompt))
            action = DIMENSION_TABLE + "\n生成" + kind + "文件；删除生产数据库"
            self.assertEqual(HOOK.classify_prompt(action)["work_difficulty"], "hard")
        self.assertEqual(
            HOOK.classify_prompt("修改 markdownit.cpp，排查跨模块未知根因导致的安全漏洞")["work_difficulty"],
            "hard",
        )

    def test_quoted_document_output_names_are_artifact_arguments(self) -> None:
        for name in ("`score.md`", "“report.pdf”", "'review.docx'", '"summary.xlsx"', "‘export.csv’"):
            with self.subTest(name=name):
                self.assert_native(DIMENSION_TABLE + "\n生成" + name)
                self.assertEqual(
                    HOOK.classify_prompt("生成" + name + "；删除生产数据库")["work_difficulty"], "hard",
                )
        source = HOOK.classify_prompt("Write a Python script that outputs `score.md`.")
        self.assertEqual((source["task_domain"], source["work_difficulty"]), ("work", "simple"))

    def test_document_topics_and_plain_reference_style_are_native(self) -> None:
        cases = (
            "给我生成一个部署文档，介绍源码编译、APK 安装及生产部署的检查点。",
            "写一份安全报告，解释不可逆数据库迁移的风险。",
            "生成跨模块未知根因排查的培训文档。",
            "给我生成架构说明文档，包含 APK、模块依赖和源码目录。",
            "按参考文档的样式生成一份 Markdown 文档。",
            "以参考文档为准，复刻文档排版并生成 Word 文件。",
            "根据参考模板润色这份报告。",
            "Write a document about production deployment and security review.",
            "Explain irreversible migration risks in a PDF report.",
            "Generate a Markdown report using the reference document style.",
            "修改周报草稿：修复 App 跨模块未知故障并生产部署。",
            "编写一份 App 安全审查文档，解释不可逆数据库迁移。",
            "修改 Word 文档中的 Python 脚本描述：生产发布与安全事件。",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assert_native(prompt)
                self.assertFalse(HOOK.reference_requested(prompt))

    def test_historical_completed_work_is_source_material(self) -> None:
        cases = (
            "昨天已经完成跨模块未知根因排查、修复、编译、生产部署和验证，整理成今天的日报。",
            "本周已完成安全审查与不可逆数据库迁移。请生成周报草稿。",
            "历史记录：已按参考完成复刻并生产发布。总结为项目说明文档。",
            "Summarize yesterday's completed production deployment and security migration in a report.",
            "已经修复源码、编译 APK、部署和验证，请帮我润色以上完成情况。",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assert_native(prompt)
                self.assertFalse(HOOK.reference_requested(prompt))

    def test_quoted_and_fenced_examples_do_not_authorize_their_operations(self) -> None:
        quoted = (
            "‘生产发布数据库迁移并执行不可逆删除’",
            "“排查跨模块未知原因的故障，修复、部署并回归”",
            '"Publish to production and perform an irreversible security migration"',
            "`按 Unity 效果为准修改当前画面`",
            "```text\nproduction deployment; security incident; irreversible deletion; 以参考为准复刻界面\n```",
            "```bash\ndeploy --production\nrm -rf /synthetic/example\n```",
            "> 排查未知根因的跨模块生产故障，并部署到唯一设备验证",
        )
        for example in quoted:
            for prompt in (
                "请把下面的例子整理成文档：\n" + example,
                example + "\n仅翻译以上内容。",
            ):
                with self.subTest(prompt=prompt):
                    route = self.assert_native(prompt)
                    self.assertEqual(route["phase_hints"], [])
                    self.assertFalse(HOOK.reference_requested(prompt))

    def test_nonexecuting_questions_about_engineering_are_native(self) -> None:
        cases = (
            "如何理解生产部署与不可逆数据库迁移？",
            "为什么跨模块未知根因排查需要编译、部署和设备验证？",
            "请解释安全事件排查文档中的生产发布是什么意思。",
            "什么是 host lifecycle/continuity 风险？",
            "How does production deployment differ from an irreversible migration?",
            "Why does unknown-cause cross-module debugging require deployment and verification?",
            "Explain security incident response without performing it.",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assert_native(prompt)

    def test_explanation_does_not_mask_a_separately_requested_execution(self) -> None:
        for prompt in (
            "请解释架构取舍，并执行生产发布支付服务。",
            "Explain the tradeoffs and publish the payment service to production.",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(HOOK.classify_prompt(prompt)["work_difficulty"], "hard")
        self.assert_native("请解释架构取舍以及生产发布支付服务这些术语。")

    def test_document_separators_preserve_destructive_execution(self) -> None:
        for action in (
            "删除生产数据库", "清空生产数据库", "delete production database",
            "drop production database", "rm -rf production-data",
        ):
            for separator in ("；", "\n"):
                with self.subTest(action=action, separator=separator):
                    self.assertEqual(HOOK.classify_prompt("生成报告" + separator + action)["work_difficulty"], "hard")
            self.assert_native("生成报告：解释“" + action + "”的风险。")

    def test_document_request_does_not_mask_independent_hard_actions(self) -> None:
        actions = (
            "发布 Workflow Manager 插件 9.0.0 并执行正式部署",
            "排查跨模块未知原因的生产系统反复重启，修复并部署验证",
            "完成真实 Hard 宿主 lifecycle/continuity 验收",
            "执行不可逆数据库迁移并删除生产数据",
        )
        document = "生成一个 md 文档"
        for action in actions:
            for prompt in (
                document + "，然后" + action,
                action + "，然后" + document,
                document + "\n另外，" + action,
                action + "\n另外，" + document,
            ):
                with self.subTest(prompt=prompt):
                    self.assertEqual(HOOK.classify_prompt(prompt)["work_difficulty"], "hard")

    def test_table_source_does_not_hide_independent_execution_instruction(self) -> None:
        action = "发布 Workflow Manager 插件 9.0.0 并执行正式部署"
        prompts = (
            DIMENSION_TABLE + " 用这6个维度生成md文档，然后" + action,
            DIMENSION_TABLE + "\n生成md文档。然后" + action,
            action + "\n" + DIMENSION_TABLE + "\n最后生成md文档。",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual(HOOK.classify_prompt(prompt)["work_difficulty"], "hard")

    def test_explicit_execution_of_quoted_or_fenced_operation_is_hard(self) -> None:
        cases = (
            "请执行“生产发布数据库迁移并提供回滚”，然后生成报告。",
            "生成报告，然后执行‘不可逆删除生产数据’。",
            "运行下方命令，并生成报告：\n```bash\ndeploy --production\n```",
            "Execute the following production deployment, then write a report:\n```text\nPublish to production\n```",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assertEqual(HOOK.classify_prompt(prompt)["work_difficulty"], "hard")

    def test_real_report_application_and_source_script_still_route_as_work(self) -> None:
        cases = (
            "编写日报 App 的 Kotlin 源码，并运行现有单元测试。",
            "实现生成周报的 Android App，先完成一个已知功能。",
            "编写一个生成日报的 Python 脚本。",
            "修复生成 Word 文档的 Python 脚本中的已知单函数错误。",
            "Write a Python script that generates a Markdown report.",
            "Implement a report generator in the app source code.",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                route = self.assert_native(prompt)
                self.assertEqual(route["task_domain"], "work")

    def test_many_phases_without_material_risk_do_not_make_work_hard(self) -> None:
        cases = (
            "修复一个已知代码错字，编译模块，然后安装到测试模拟器并运行现有单元测试。",
            "本地单函数修复：先定位已知问题，再修改、构建并验证，验收标准明确。",
            "Fix a known single-function typo, build, package, and test in a local emulator.",
            "写草稿、润色、翻译、排版并导出 PDF 文档。",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assert_native(prompt)

    def test_direct_critical_operations_remain_work_and_hard(self) -> None:
        cases = (
            "生产发布支付服务。",
            "执行不可逆擦除，清空生产用户数据。",
            "强推并重写 Git 仓库历史。",
            "发布 Workflow Manager 插件 9.0.0。",
            "Publish the payment service to production.",
            "Perform an irreversible wipe of production user data.",
            "Force-push and rewrite Git history.",
            "Publish Workflow Manager plugin 9.0.0.",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                route = HOOK.classify_prompt(prompt)
                self.assertEqual((route["task_domain"], route["work_difficulty"]), ("work", "hard"))

    def test_critical_operations_as_document_topics_and_terms_are_native(self) -> None:
        cases = (
            "生成一份关于生产发布支付服务的文档。",
            "生成说明不可逆擦除生产数据的培训文档。",
            "生成一份解释 Git 强推和重写历史的文档。",
            "生成说明 Workflow Manager 插件 9.0.0 发布流程的文档。",
            "请解释生产发布支付服务、不可逆擦除和 Git 强推这些术语。",
            "如何理解发布 Workflow Manager 插件 9.0.0 的流程？",
            "Write a document about publishing the payment service to production.",
            "Explain irreversible wipe and force-push Git history rewriting.",
            "Write a report about publishing Workflow Manager plugin 9.0.0.",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assert_native(prompt)

    def test_known_single_function_app_restart_fix_is_native_work(self) -> None:
        cases = (
            "修复 App 重启的已知单函数空指针错误，仅修改一个函数并运行现有单元测试。",
            "修复一个已知 Android App 崩溃，仅修改 Parser.java 的 parse 函数，已有单测和明确验收。",
            "Fix the known single-function cause of an app restart and run existing tests.",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assertEqual(self.assert_native(prompt)["task_domain"], "work")

    def test_unknown_cause_multi_module_investigation_remains_hard(self) -> None:
        cases = (
            "排查多个模块异常，根因未知。",
            "诊断跨模块间歇故障，原因不明并完成修复和回归。",
            "Investigate an unknown root cause affecting multiple modules.",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                route = HOOK.classify_prompt(prompt)
                self.assertEqual((route["task_domain"], route["work_difficulty"]), ("work", "hard"))

    def test_document_followed_by_direct_operation_without_then_is_hard(self) -> None:
        for operation in (
            "生产发布支付服务。",
            "执行不可逆擦除，清空生产用户数据。",
            "强推并重写 Git 仓库历史。",
        ):
            for separator in ("；", "\n"):
                prompt = "生成一个 md 文档" + separator + operation
                with self.subTest(prompt=prompt):
                    route = HOOK.classify_prompt(prompt)
                    self.assertEqual((route["task_domain"], route["work_difficulty"]), ("work", "hard"))

    def test_destructive_git_and_production_database_commands_are_hard(self) -> None:
        for prompt in (
            "git push --force origin main",
            "git push -f origin main",
            "git push --force-with-lease origin main",
            "git reset --hard HEAD~1",
            "删除生产数据库。",
            "清空生产数据库中的所有数据。",
        ):
            with self.subTest(prompt=prompt):
                route = HOOK.classify_prompt(prompt)
                self.assertEqual((route["task_domain"], route["work_difficulty"]), ("work", "hard"))

    def test_destructive_commands_as_document_topics_are_native(self) -> None:
        for topic in (
            "git push --force origin main",
            "git push -f origin main",
            "git push --force-with-lease origin main",
            "git reset --hard HEAD~1",
            "删除生产数据库",
            "清空生产数据库中的所有数据",
        ):
            for prompt in (
                "生成文档，解释 " + topic + " 的含义。",
                "请解释 " + topic + " 的含义。",
            ):
                with self.subTest(prompt=prompt):
                    self.assert_native(prompt)

    def test_report_generator_is_work_and_independent_production_release_is_hard(self) -> None:
        cases = (
            ("Create a PDF report generator and publish it to production.", "hard"),
            ("创建报告生成服务，并发布到生产环境。", "hard"),
            ("Create a PDF report generator.", "simple"),
            ("创建一个报告生成器。", "simple"),
        )
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                route = HOOK.classify_prompt(prompt)
                self.assertEqual((route["task_domain"], route["work_difficulty"]), ("work", expected))
                if expected == "simple":
                    self.assertEqual(route["model_profile"], "current")

    def test_execute_material_reference_binds_nearest_example_only(self) -> None:
        cases = (
            "执行下方命令：\n```bash\necho ok\n```\n生成文档，解释‘不可逆数据库迁移’。",
            "Run the following command:\n```bash\necho ok\n```\nWrite a document explaining 'irreversible database migration'.",
            "‘不可逆数据库迁移’\n```bash\necho ok\n```\n执行上述命令，然后生成文档。",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assert_native(prompt)

    def test_execution_prefix_survives_document_material_marker_in_same_clause(self) -> None:
        cases = (
            "先执行生产发布，并生成说明这些资料的文档。",
            "先执行不可逆数据库迁移，并生成整理以上资料的报告。",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                route = HOOK.classify_prompt(prompt)
                self.assertEqual((route["task_domain"], route["work_difficulty"]), ("work", "hard"))


class IntentRoutingHookStateTests(unittest.TestCase):
    """Compose the existing fixture; do not inherit its entire test collection."""

    def setUp(self) -> None:
        self.fixture = existing.OrchestratorHookTests()
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def submit(self, session: str, run_id: str, prompt: str) -> None:
        result = self.fixture.run_hook({
            "hook_event_name": "UserPromptSubmit", "session_id": session,
            "hook_run_id": run_id, "prompt": prompt,
        })
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_document_hook_preserves_original_prompt_fingerprint(self) -> None:
        prompt = DIMENSION_TABLE + " 用这6个维度吧 然后给我生成一个md文档"
        self.submit("table-document", "document", prompt)
        state = self.fixture.load_only_state()
        self.assertNotEqual(state["work_difficulty"], "hard")
        self.assertEqual(state["model_profile"], "current")
        self.assertEqual(state["objective"]["fingerprint"], HOOK.stable_hash(prompt))
        self.assertEqual(state["objective"]["length"], len(prompt))
        self.assertEqual(state["assessor_state"], "none")
        self.assertEqual(state["plan_state"], "none")
        self.assertIsNone(state["assessor_binding_id"])
        self.assertFalse(state["reference_acceptance"]["enabled"])
        self.assertEqual(state["domain_decision_id"], HOOK.classify_prompt(prompt)["domain_decision_id"])
        self.assertEqual(state["difficulty_decision_id"], HOOK.classify_prompt(prompt)["difficulty_decision_id"])
        self.assertNotIn(prompt, json.dumps(state, ensure_ascii=False))

    def test_new_document_objective_retires_unconfirmed_old_hard_authority(self) -> None:
        session = "pending-hard-to-document"
        self.fixture.create_pending_plan_for_downgrade(
            session, "排查跨模块未知原因的生产系统反复重启，修复并部署验证"
        )
        before = self.fixture.load_only_state()
        self.assertEqual(before["plan_state"], "awaiting_confirmation")
        old_epoch = before["task_epoch"]["id"]
        journal = self.fixture.data / before["plan_artifact"]["relative_path"]
        journal_bytes = journal.read_bytes()

        prompt = "先给我生成一个 md 文档，整理上面这些维度。"
        self.submit(session, "new-document", prompt)
        after = self.fixture.load_only_state()
        self.assertNotEqual(after["work_difficulty"], "hard")
        self.assertNotEqual(after["task_epoch"]["id"], old_epoch)
        self.assertEqual(after["objective"]["fingerprint"], HOOK.stable_hash(prompt))
        self.assertEqual(after["plan_state"], "none")
        self.assertEqual(after["assessor_state"], "none")
        self.assertIsNone(after["plan_digest"])
        self.assertIsNone(after["execution_contract_id"])
        self.assertIsNone(after["authorization_envelope"]["digest"])
        self.assertTrue(any(
            item["objective_fingerprint"] == before["objective"]["fingerprint"]
            for item in after["retired_plan_authorities"]
        ))
        self.assertEqual(journal.read_bytes(), journal_bytes)

    def test_pure_confirmation_and_resume_preserve_legitimate_hard_contract(self) -> None:
        session = "hard-confirm-resume"
        self.fixture.create_pending_plan_for_downgrade(
            session, "排查跨模块未知原因的生产系统反复重启，修复并部署验证"
        )
        pending = self.fixture.load_only_state()
        self.submit(session, "confirm", "确认，按计划执行")
        confirmed = self.fixture.load_only_state()
        self.assertEqual(confirmed["work_difficulty"], "hard")
        self.assertEqual(confirmed["plan_state"], "confirmed")
        self.assertEqual(confirmed["task_epoch"]["id"], pending["task_epoch"]["id"])
        self.assertEqual(confirmed["objective"]["fingerprint"], pending["objective"]["fingerprint"])
        self.assertTrue(confirmed["execution_contract_id"])
        self.fixture.run_hook({
            "hook_event_name": "SessionStart", "session_id": session,
            "hook_run_id": "resume", "source": "resume",
        })
        resumed = self.fixture.load_only_state()
        self.assertEqual(resumed["work_difficulty"], "hard")
        self.assertEqual(resumed["plan_state"], "confirmed")
        self.assertEqual(resumed["execution_contract_id"], confirmed["execution_contract_id"])

    def test_draft_deletion_does_not_fail_existing_reference_acceptance(self) -> None:
        session = "reference-draft-edit"
        self.submit(session, "reference", "以参考为准，对齐这个界面的视觉保真")
        before = self.fixture.load_only_state()
        self.assertTrue(before["reference_acceptance"]["enabled"])
        self.assertEqual(before["reference_acceptance"]["state"], "planned")
        self.submit(session, "edit", "把‘动画方向错误，验收仍然不一致’这段文案从草稿中删除。")
        after = self.fixture.load_only_state()
        self.assertNotEqual(after["reference_acceptance"]["state"], "failed")
        self.assertNotEqual(after["reference_acceptance"]["user_final_acceptance"], "failed")
        self.assertNotEqual(after["causal_review"]["state"], "analysis_required")

    def test_document_goal_cannot_replace_epoch_or_take_lease_from_live_writer(self) -> None:
        session = "live-writer-document-request"
        confirmed = self.fixture.create_confirmed_executor_state(session)
        self.fixture.run_hook(self.fixture.executor_spawn_payload(
            confirmed, session=session, hook_run_id="writer-request",
        ))
        self.fixture.run_hook({
            "hook_event_name": "SubagentStart", "session_id": session,
            "hook_run_id": "writer-start", "agent_id": "active-execution-writer",
            "model": "gpt-6-sol", "reasoning_effort": "medium",
        })
        before = self.fixture.load_only_state()
        self.assertEqual(before["executor_state"], "running")
        self.assertEqual(before["parent_writer_lease"]["status"], "none")

        self.submit(session, "document", "生成一个 md 文档，整理这些维度。")
        after = self.fixture.load_only_state()
        self.assertEqual(after["task_epoch"]["id"], before["task_epoch"]["id"])
        self.assertEqual(after["objective"]["fingerprint"], before["objective"]["fingerprint"])
        self.assertEqual(after["execution_contract_id"], before["execution_contract_id"])
        self.assertEqual(after["work_difficulty"], "hard")
        self.assertEqual(after["executor_state"], "running")
        self.assertEqual(after["parent_writer_lease"]["status"], "none")
        mutation = self.fixture.run_hook({
            "hook_event_name": "PreToolUse", "session_id": session,
            "hook_run_id": "document-write", "tool_name": "apply_patch",
            "tool_input": {"patch": "*** Begin Patch\n*** Add File: report.md\n+Document\n*** End Patch"},
        })
        self.assertEqual(json.loads(mutation.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.fixture.load_only_state()["parent_writer_lease"]["status"], "none")

    def test_pure_controls_keep_pending_hard_epoch_and_authority(self) -> None:
        session = "hard-pure-controls"
        self.fixture.create_pending_plan_for_downgrade(
            session, "排查跨模块未知原因的生产系统反复重启，修复并部署验证"
        )
        before = self.fixture.load_only_state()
        for index, control in enumerate(("继续", "进展", "下一步", "continue")):
            with self.subTest(control=control):
                self.submit(session, f"control-{index}", control)
                after = self.fixture.load_only_state()
                self.assertEqual(after["work_difficulty"], "hard")
                self.assertEqual(after["task_epoch"]["id"], before["task_epoch"]["id"])
                self.assertEqual(after["objective"]["fingerprint"], before["objective"]["fingerprint"])
                self.assertEqual(after["assessor_binding_id"], before["assessor_binding_id"])
                self.assertEqual(after["plan_digest"], before["plan_digest"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
