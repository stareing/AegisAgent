"""Tests for the hooks subsystem.

Covers:
1. Hook models (HookPoint, HookMeta, HookContext, HookResult)
2. HookRegistry (register/unregister/resolve_chain/ordering)
3. HookExecutor (chain execution, DENY handling, timeout, failure policy)
4. Architecture guards (DTO-only, DENY only at deniable points, etc.)
5. Built-in hooks (ToolGuardHook, AuditNotifyHook, MemoryReviewHook)
6. Singleton management
"""

from __future__ import annotations

import asyncio

import pytest

from agent_framework.models.hook import (
    DENIABLE_HOOK_POINTS,
    HookCategory,
    HookContext,
    HookExecutionMode,
    HookFailurePolicy,
    HookMeta,
    HookPoint,
    HookResult,
    HookResultAction,
)
from agent_framework.hooks.protocol import AsyncHookProtocol, HookProtocol
from agent_framework.hooks.registry import HookRegistry
from agent_framework.hooks.executor import HookExecutor
from agent_framework.hooks.errors import (
    HookDeniedError,
    HookRegistrationError,
    HookTimeoutError,
)
from agent_framework.hooks.singleton import (
    get_hook_executor,
    get_hook_registry,
    reset_hook_singletons,
)
from agent_framework.hooks.builtin.tool_guard_hook import ToolGuardHook
from agent_framework.hooks.builtin.audit_hook import AuditNotifyHook
from agent_framework.hooks.builtin.memory_review_hook import MemoryReviewHook


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class AllowHook:
    """Simple hook that always allows."""

    def __init__(
        self,
        hook_id: str = "test.allow",
        hook_point: HookPoint = HookPoint.PRE_TOOL_USE,
        priority: int = 100,
        plugin_id: str = "test",
    ) -> None:
        self._meta = HookMeta(
            hook_id=hook_id,
            plugin_id=plugin_id,
            hook_point=hook_point,
            priority=priority,
        )

    @property
    def meta(self) -> HookMeta:
        return self._meta

    def execute(self, context: HookContext) -> HookResult:
        return HookResult(action=HookResultAction.ALLOW)


class DenyHook:
    """Hook that denies with a reason."""

    def __init__(
        self,
        hook_id: str = "test.deny",
        hook_point: HookPoint = HookPoint.PRE_TOOL_USE,
        priority: int = 100,
    ) -> None:
        self._meta = HookMeta(
            hook_id=hook_id,
            plugin_id="test",
            hook_point=hook_point,
            priority=priority,
        )

    @property
    def meta(self) -> HookMeta:
        return self._meta

    def execute(self, context: HookContext) -> HookResult:
        return HookResult(
            action=HookResultAction.DENY,
            message="Denied by test hook",
        )


class SlowHook:
    """Hook that sleeps longer than timeout."""

    def __init__(self, sleep_s: float = 5.0) -> None:
        self._meta = HookMeta(
            hook_id="test.slow",
            plugin_id="test",
            hook_point=HookPoint.PRE_TOOL_USE,
            timeout_ms=100,
        )
        self._sleep_s = sleep_s

    @property
    def meta(self) -> HookMeta:
        return self._meta

    def execute(self, context: HookContext) -> HookResult:
        import time
        time.sleep(self._sleep_s)
        return HookResult(action=HookResultAction.ALLOW)


class FailingHook:
    """Hook that raises an exception."""

    def __init__(
        self,
        failure_policy: HookFailurePolicy = HookFailurePolicy.WARN,
    ) -> None:
        self._meta = HookMeta(
            hook_id="test.failing",
            plugin_id="test",
            hook_point=HookPoint.PRE_TOOL_USE,
            failure_policy=failure_policy,
        )

    @property
    def meta(self) -> HookMeta:
        return self._meta

    def execute(self, context: HookContext) -> HookResult:
        raise RuntimeError("Hook execution failed")


class AsyncAllowHook:
    """Async hook that allows."""

    def __init__(self, hook_id: str = "test.async_allow") -> None:
        self._meta = HookMeta(
            hook_id=hook_id,
            plugin_id="test",
            hook_point=HookPoint.PRE_TOOL_USE,
            execution_mode=HookExecutionMode.ASYNC,
        )

    @property
    def meta(self) -> HookMeta:
        return self._meta

    async def execute(self, context: HookContext) -> HookResult:
        await asyncio.sleep(0)
        return HookResult(action=HookResultAction.ALLOW)


class RecordingHook:
    """Hook that records calls for verification."""

    def __init__(
        self,
        hook_id: str = "test.recording",
        hook_point: HookPoint = HookPoint.RUN_START,
    ) -> None:
        self._meta = HookMeta(
            hook_id=hook_id,
            plugin_id="test",
            hook_point=hook_point,
        )
        self.calls: list[HookContext] = []

    @property
    def meta(self) -> HookMeta:
        return self._meta

    def execute(self, context: HookContext) -> HookResult:
        self.calls.append(context)
        return HookResult(action=HookResultAction.NOOP)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestHookModels:
    def test_hook_point_values(self) -> None:
        assert HookPoint.PRE_TOOL_USE.value == "tool.pre_use"
        assert HookPoint.RUN_START.value == "run.start"

    def test_deniable_hook_points(self) -> None:
        assert HookPoint.PRE_TOOL_USE in DENIABLE_HOOK_POINTS
        assert HookPoint.PRE_DELEGATION in DENIABLE_HOOK_POINTS
        assert HookPoint.MEMORY_PRE_RECORD in DENIABLE_HOOK_POINTS
        assert HookPoint.RUN_START not in DENIABLE_HOOK_POINTS
        assert HookPoint.RUN_FINISH not in DENIABLE_HOOK_POINTS

    def test_hook_meta_frozen(self) -> None:
        meta = HookMeta(hook_id="x", hook_point=HookPoint.RUN_START)
        with pytest.raises(Exception):
            meta.hook_id = "y"  # type: ignore[misc]

    def test_hook_context_defaults(self) -> None:
        ctx = HookContext()
        assert ctx.run_id is None
        assert ctx.payload == {}

    def test_hook_result_defaults(self) -> None:
        result = HookResult()
        assert result.action == HookResultAction.NOOP
        assert result.emitted_artifacts == []

    def test_hook_categories(self) -> None:
        assert HookCategory.COMMAND.value == "command"
        assert HookCategory.PROMPT.value == "prompt"
        assert HookCategory.AGENT.value == "agent"


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------

class TestHookRegistry:
    def test_register_and_list(self) -> None:
        reg = HookRegistry()
        hook = AllowHook()
        reg.register(hook)
        assert reg.count == 1
        metas = reg.list_hooks()
        assert len(metas) == 1
        assert metas[0].hook_id == "test.allow"

    def test_duplicate_registration_raises(self) -> None:
        reg = HookRegistry()
        reg.register(AllowHook(hook_id="dup"))
        with pytest.raises(HookRegistrationError, match="Duplicate"):
            reg.register(AllowHook(hook_id="dup"))

    def test_unregister(self) -> None:
        reg = HookRegistry()
        reg.register(AllowHook())
        assert reg.count == 1
        reg.unregister("test.allow")
        assert reg.count == 0

    def test_unregister_nonexistent_is_noop(self) -> None:
        reg = HookRegistry()
        reg.unregister("nonexistent")  # Should not raise

    def test_filter_by_hook_point(self) -> None:
        reg = HookRegistry()
        reg.register(AllowHook(hook_id="a", hook_point=HookPoint.PRE_TOOL_USE))
        reg.register(AllowHook(hook_id="b", hook_point=HookPoint.RUN_START))
        metas = reg.list_hooks(hook_point=HookPoint.PRE_TOOL_USE)
        assert len(metas) == 1
        assert metas[0].hook_id == "a"

    def test_filter_by_plugin_id(self) -> None:
        reg = HookRegistry()
        reg.register(AllowHook(hook_id="a", plugin_id="plugin_a"))
        reg.register(AllowHook(hook_id="b", plugin_id="plugin_b"))
        metas = reg.list_hooks(plugin_id="plugin_a")
        assert len(metas) == 1

    def test_resolve_chain_ordering(self) -> None:
        """Chain must be stable: priority → plugin_id → hook_id."""
        reg = HookRegistry()
        reg.register(AllowHook(hook_id="c", priority=100, plugin_id="z"))
        reg.register(AllowHook(hook_id="a", priority=50, plugin_id="a"))
        reg.register(AllowHook(hook_id="b", priority=100, plugin_id="a"))

        chain = reg.resolve_chain(HookPoint.PRE_TOOL_USE)
        ids = [h.meta.hook_id for h in chain]
        assert ids == ["a", "b", "c"]

    def test_disabled_hooks_excluded_from_chain(self) -> None:
        reg = HookRegistry()
        reg.register(AllowHook(hook_id="enabled"))
        # Create a disabled hook
        disabled = AllowHook(hook_id="disabled")
        disabled._meta = HookMeta(
            hook_id="disabled",
            plugin_id="test",
            hook_point=HookPoint.PRE_TOOL_USE,
            enabled=False,
        )
        reg.register(disabled)
        chain = reg.resolve_chain(HookPoint.PRE_TOOL_USE)
        assert len(chain) == 1
        assert chain[0].meta.hook_id == "enabled"

    def test_clear(self) -> None:
        reg = HookRegistry()
        reg.register(AllowHook())
        reg.clear()
        assert reg.count == 0


# ---------------------------------------------------------------------------
# Executor tests
# ---------------------------------------------------------------------------

class TestHookExecutor:
    @pytest.mark.asyncio
    async def test_empty_chain(self) -> None:
        reg = HookRegistry()
        executor = HookExecutor(reg)
        results = await executor.execute_chain(
            HookPoint.RUN_START, HookContext()
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_allow_chain(self) -> None:
        reg = HookRegistry()
        reg.register(AllowHook())
        executor = HookExecutor(reg)
        results = await executor.execute_chain(
            HookPoint.PRE_TOOL_USE, HookContext()
        )
        assert len(results) == 1
        assert results[0].action == HookResultAction.ALLOW

    @pytest.mark.asyncio
    async def test_deny_at_deniable_point(self) -> None:
        reg = HookRegistry()
        reg.register(DenyHook())
        executor = HookExecutor(reg)
        with pytest.raises(HookDeniedError, match="Denied"):
            await executor.execute_chain(
                HookPoint.PRE_TOOL_USE, HookContext()
            )

    @pytest.mark.asyncio
    async def test_deny_at_non_deniable_point_becomes_noop(self) -> None:
        """DENY at RUN_START (non-deniable) should become NOOP."""
        reg = HookRegistry()
        reg.register(DenyHook(hook_point=HookPoint.RUN_START))
        executor = HookExecutor(reg)
        results = await executor.execute_chain(
            HookPoint.RUN_START, HookContext()
        )
        assert len(results) == 1
        assert results[0].action == HookResultAction.NOOP

    @pytest.mark.asyncio
    async def test_async_hook(self) -> None:
        reg = HookRegistry()
        reg.register(AsyncAllowHook())
        executor = HookExecutor(reg)
        results = await executor.execute_chain(
            HookPoint.PRE_TOOL_USE, HookContext()
        )
        assert len(results) == 1
        assert results[0].action == HookResultAction.ALLOW

    @pytest.mark.asyncio
    async def test_timeout_with_warn_policy(self) -> None:
        reg = HookRegistry()
        hook = SlowHook(sleep_s=5.0)
        reg.register(hook)
        executor = HookExecutor(reg)
        results = await executor.execute_chain(
            HookPoint.PRE_TOOL_USE, HookContext()
        )
        assert len(results) == 1
        assert results[0].action == HookResultAction.NOOP

    @pytest.mark.asyncio
    async def test_failure_policy_ignore(self) -> None:
        reg = HookRegistry()
        reg.register(FailingHook(failure_policy=HookFailurePolicy.IGNORE))
        executor = HookExecutor(reg)
        results = await executor.execute_chain(
            HookPoint.PRE_TOOL_USE, HookContext()
        )
        assert len(results) == 1
        assert results[0].action == HookResultAction.NOOP

    @pytest.mark.asyncio
    async def test_failure_policy_warn(self) -> None:
        reg = HookRegistry()
        reg.register(FailingHook(failure_policy=HookFailurePolicy.WARN))
        executor = HookExecutor(reg)
        results = await executor.execute_chain(
            HookPoint.PRE_TOOL_USE, HookContext()
        )
        assert len(results) == 1
        assert results[0].error_code == "HOOK_EXECUTION_FAILED"

    @pytest.mark.asyncio
    async def test_failure_policy_fail_closed(self) -> None:
        reg = HookRegistry()
        reg.register(FailingHook(failure_policy=HookFailurePolicy.FAIL_CLOSED))
        executor = HookExecutor(reg)
        with pytest.raises(RuntimeError, match="fail_closed"):
            await executor.execute_chain(
                HookPoint.PRE_TOOL_USE, HookContext()
            )

    @pytest.mark.asyncio
    async def test_chain_order_preserved(self) -> None:
        """Multiple hooks execute in priority order."""
        reg = HookRegistry()
        calls: list[str] = []

        class OrderHook:
            def __init__(self, hid: str, priority: int) -> None:
                self._meta = HookMeta(
                    hook_id=hid, plugin_id="test",
                    hook_point=HookPoint.RUN_FINISH, priority=priority,
                )

            @property
            def meta(self) -> HookMeta:
                return self._meta

            def execute(self, context: HookContext) -> HookResult:
                calls.append(self._meta.hook_id)
                return HookResult(action=HookResultAction.NOOP)

        reg.register(OrderHook("third", 300))
        reg.register(OrderHook("first", 100))
        reg.register(OrderHook("second", 200))

        executor = HookExecutor(reg)
        await executor.execute_chain(HookPoint.RUN_FINISH, HookContext())
        assert calls == ["first", "second", "third"]

    @pytest.mark.asyncio
    async def test_deny_stops_chain(self) -> None:
        """After DENY, subsequent hooks should not execute."""
        reg = HookRegistry()
        calls: list[str] = []

        class TrackHook:
            def __init__(self, hid: str, priority: int, deny: bool = False) -> None:
                self._meta = HookMeta(
                    hook_id=hid, plugin_id="test",
                    hook_point=HookPoint.PRE_TOOL_USE, priority=priority,
                )
                self._deny = deny

            @property
            def meta(self) -> HookMeta:
                return self._meta

            def execute(self, context: HookContext) -> HookResult:
                calls.append(self._meta.hook_id)
                if self._deny:
                    return HookResult(action=HookResultAction.DENY, message="stop")
                return HookResult(action=HookResultAction.ALLOW)

        reg.register(TrackHook("before", 10))
        reg.register(TrackHook("denier", 50, deny=True))
        reg.register(TrackHook("after", 100))

        executor = HookExecutor(reg)
        with pytest.raises(HookDeniedError):
            await executor.execute_chain(HookPoint.PRE_TOOL_USE, HookContext())

        assert calls == ["before", "denier"]
        assert "after" not in calls

    @pytest.mark.asyncio
    async def test_hook_result_includes_hook_id(self) -> None:
        reg = HookRegistry()
        reg.register(AllowHook(hook_id="my.hook"))
        executor = HookExecutor(reg)
        results = await executor.execute_chain(
            HookPoint.PRE_TOOL_USE, HookContext()
        )
        assert results[0].hook_id == "my.hook"


# ---------------------------------------------------------------------------
# Architecture guard tests
# ---------------------------------------------------------------------------

class TestArchitectureGuards:
    def test_hook_context_does_not_accept_mutable_state(self) -> None:
        """HookContext payload is a dict — not SessionState or AgentState."""
        ctx = HookContext(payload={"key": "value"})
        assert isinstance(ctx.payload, dict)
        # Verify no reference to forbidden types in payload
        from agent_framework.models.session import SessionState
        from agent_framework.models.agent import AgentState
        assert not isinstance(ctx.payload, (SessionState, AgentState))

    def test_all_deniable_points_are_pre_hooks(self) -> None:
        """DENY should only be available at pre-execution hook points."""
        for hp in DENIABLE_HOOK_POINTS:
            assert "pre" in hp.value or hp == HookPoint.CONTEXT_PRE_BUILD

    def test_hook_meta_is_frozen(self) -> None:
        meta = HookMeta(hook_id="x", hook_point=HookPoint.RUN_START)
        with pytest.raises(Exception):
            meta.priority = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Built-in hook tests
# ---------------------------------------------------------------------------

class TestToolGuardHook:
    def test_allow_normal_args(self) -> None:
        hook = ToolGuardHook()
        ctx = HookContext(payload={
            "tool_name": "read_file",
            "arguments": {"path": "/tmp/test.txt"},
            "tool_tags": ["filesystem"],
        })
        result = hook.execute(ctx)
        assert result.action == HookResultAction.ALLOW

    def test_deny_oversized_args(self) -> None:
        hook = ToolGuardHook(max_argument_chars=100)
        ctx = HookContext(payload={
            "tool_name": "write_file",
            "arguments": {"content": "x" * 200},
            "tool_tags": [],
        })
        result = hook.execute(ctx)
        assert result.action == HookResultAction.DENY
        assert "exceed" in (result.message or "").lower()

    def test_request_confirmation_dangerous_tags(self) -> None:
        hook = ToolGuardHook()
        ctx = HookContext(payload={
            "tool_name": "shell",
            "arguments": {"command": "ls"},
            "tool_tags": ["destructive"],
        })
        result = hook.execute(ctx)
        assert result.action == HookResultAction.REQUEST_CONFIRMATION


class TestAuditNotifyHook:
    def test_produces_audit_data(self) -> None:
        hook = AuditNotifyHook()
        ctx = HookContext(
            run_id="run-123",
            agent_id="agent-1",
            payload={"success": True},
        )
        result = hook.execute(ctx)
        assert result.action == HookResultAction.NOOP
        assert result.audit_data is not None
        assert result.audit_data["run_id"] == "run-123"

    def test_notify_callback_called(self) -> None:
        records: list[dict] = []
        hook = AuditNotifyHook(
            notify_callback=lambda r: records.append(r),
            hook_id="test.audit",
        )
        ctx = HookContext(run_id="r1", payload={"x": "y"})
        hook.execute(ctx)
        assert len(records) == 1
        assert records[0]["run_id"] == "r1"

    def test_notify_callback_failure_ignored(self) -> None:
        def bad_callback(r: dict) -> None:
            raise RuntimeError("boom")

        hook = AuditNotifyHook(
            notify_callback=bad_callback,
            hook_id="test.audit_fail",
        )
        result = hook.execute(HookContext())
        assert result.action == HookResultAction.NOOP  # No exception


class TestMemoryReviewHook:
    def test_allow_normal_content(self) -> None:
        hook = MemoryReviewHook()
        ctx = HookContext(payload={
            "content": "Remember that the user prefers dark mode",
            "tags": ["preference"],
        })
        result = hook.execute(ctx)
        assert result.action == HookResultAction.ALLOW

    def test_deny_oversized_content(self) -> None:
        hook = MemoryReviewHook(max_content_length=50)
        ctx = HookContext(payload={
            "content": "x" * 100,
            "tags": [],
        })
        result = hook.execute(ctx)
        assert result.action == HookResultAction.DENY

    def test_deny_too_many_tags(self) -> None:
        hook = MemoryReviewHook(max_tags=3)
        ctx = HookContext(payload={
            "content": "ok",
            "tags": ["a", "b", "c", "d"],
        })
        result = hook.execute(ctx)
        assert result.action == HookResultAction.DENY

    def test_deny_sensitive_content(self) -> None:
        hook = MemoryReviewHook()
        ctx = HookContext(payload={
            "content": "The password=s3cr3t123 for the database",
            "tags": [],
        })
        result = hook.execute(ctx)
        assert result.action == HookResultAction.DENY
        assert "sensitive" in (result.message or "").lower()


# ---------------------------------------------------------------------------
# Singleton tests
# ---------------------------------------------------------------------------

class TestHookSingletons:
    def test_get_hook_registry_returns_same_instance(self) -> None:
        reset_hook_singletons()
        r1 = get_hook_registry()
        r2 = get_hook_registry()
        assert r1 is r2

    def test_get_hook_executor_returns_same_instance(self) -> None:
        reset_hook_singletons()
        e1 = get_hook_executor()
        e2 = get_hook_executor()
        assert e1 is e2

    def test_reset_creates_new_instances(self) -> None:
        r1 = get_hook_registry()
        reset_hook_singletons()
        r2 = get_hook_registry()
        assert r1 is not r2


# ---------------------------------------------------------------------------
# HookSubsystem instance-level tests
# ---------------------------------------------------------------------------

class TestHookSubsystem:
    def test_instance_isolation(self) -> None:
        """Two HookSubsystem instances have independent registries."""
        from agent_framework.hooks.singleton import HookSubsystem
        hs1 = HookSubsystem()
        hs2 = HookSubsystem()
        hs1.registry.register(AllowHook(hook_id="only_in_hs1"))
        assert hs1.registry.count == 1
        assert hs2.registry.count == 0

    def test_executor_uses_own_registry(self) -> None:
        from agent_framework.hooks.singleton import HookSubsystem
        hs = HookSubsystem()
        assert hs.executor._registry is hs.registry


# ---------------------------------------------------------------------------
# Frozen context tests
# ---------------------------------------------------------------------------

class TestFrozenContext:
    def test_payload_deep_copied(self) -> None:
        """Modifying original dict should not affect HookContext payload."""
        original = {"key": [1, 2, 3]}
        ctx = HookContext(payload=original)
        original["key"].append(4)
        assert ctx.payload["key"] == [1, 2, 3]

    def test_context_is_frozen(self) -> None:
        ctx = HookContext(run_id="test")
        with pytest.raises(Exception):
            ctx.run_id = "changed"  # type: ignore[misc]

    def test_instructions_loaded_hook_point_exists(self) -> None:
        assert HookPoint.INSTRUCTIONS_LOADED.value == "instructions.loaded"


class TestSkillRouterRemove:
    """SkillRouter.remove_skill must work for plugin lifecycle."""

    def test_remove_existing(self) -> None:
        from agent_framework.agent.skill_router import SkillRouter
        from agent_framework.models.agent import Skill
        router = SkillRouter()
        router.register_skill(Skill(skill_id="test", name="Test", description="d"))
        assert router.get_skill("test") is not None
        assert router.remove_skill("test") is True
        assert router.get_skill("test") is None

    def test_remove_nonexistent(self) -> None:
        from agent_framework.agent.skill_router import SkillRouter
        router = SkillRouter()
        assert router.remove_skill("nope") is False


# ---------------------------------------------------------------------------
# Interpreter tests
# ---------------------------------------------------------------------------

class TestHookResultInterpreter:
    def test_noop_results(self) -> None:
        from agent_framework.hooks.interpreter import interpret_hook_results
        results = [
            HookResult(action=HookResultAction.NOOP),
            HookResult(action=HookResultAction.ALLOW),
        ]
        outcome = interpret_hook_results(HookPoint.PRE_TOOL_USE, results)
        assert outcome.should_proceed is True
        assert not outcome.needs_confirmation
        assert outcome.modifications == {}

    def test_modify_whitelisted_field(self) -> None:
        from agent_framework.hooks.interpreter import interpret_hook_results
        results = [
            HookResult(
                action=HookResultAction.MODIFY,
                modified_payload={"sanitized_arguments": {"safe": True}},
            ),
        ]
        outcome = interpret_hook_results(HookPoint.PRE_TOOL_USE, results)
        assert "sanitized_arguments" in outcome.modifications
        assert outcome.modifications["sanitized_arguments"] == {"safe": True}

    def test_modify_blocked_non_whitelisted_field(self) -> None:
        from agent_framework.hooks.interpreter import interpret_hook_results
        results = [
            HookResult(
                action=HookResultAction.MODIFY,
                modified_payload={"tool_name": "hacked"},
            ),
        ]
        outcome = interpret_hook_results(HookPoint.PRE_TOOL_USE, results)
        assert "tool_name" not in outcome.modifications

    def test_request_confirmation(self) -> None:
        from agent_framework.hooks.interpreter import interpret_hook_results
        results = [
            HookResult(
                action=HookResultAction.REQUEST_CONFIRMATION,
                message="Please confirm this operation",
            ),
        ]
        outcome = interpret_hook_results(HookPoint.PRE_TOOL_USE, results)
        assert outcome.needs_confirmation is True
        assert "confirm" in outcome.confirmation_reason.lower()

    def test_emit_artifact(self) -> None:
        from agent_framework.hooks.interpreter import interpret_hook_results
        results = [
            HookResult(
                action=HookResultAction.EMIT_ARTIFACT,
                emitted_artifacts=[{"type": "report", "name": "audit.json"}],
            ),
        ]
        outcome = interpret_hook_results(HookPoint.PRE_TOOL_USE, results)
        assert len(outcome.emitted_artifacts) == 1
        assert outcome.emitted_artifacts[0]["name"] == "audit.json"

    def test_audit_data_collected(self) -> None:
        from agent_framework.hooks.interpreter import interpret_hook_results
        results = [
            HookResult(
                action=HookResultAction.NOOP,
                audit_data={"event": "tool_guard_check"},
            ),
            HookResult(
                action=HookResultAction.ALLOW,
                audit_data={"event": "policy_check"},
            ),
        ]
        outcome = interpret_hook_results(HookPoint.PRE_TOOL_USE, results)
        assert len(outcome.audit_records) == 2

    def test_memory_pre_record_whitelist(self) -> None:
        from agent_framework.hooks.interpreter import interpret_hook_results
        results = [
            HookResult(
                action=HookResultAction.MODIFY,
                modified_payload={
                    "content": "sanitized content",  # whitelisted
                    "memory_id": "hacked",  # NOT whitelisted
                },
            ),
        ]
        outcome = interpret_hook_results(HookPoint.MEMORY_PRE_RECORD, results)
        assert "content" in outcome.modifications
        assert "memory_id" not in outcome.modifications

    def test_multiple_modify_last_writer_wins(self) -> None:
        from agent_framework.hooks.interpreter import interpret_hook_results
        results = [
            HookResult(
                action=HookResultAction.MODIFY,
                modified_payload={"sanitized_arguments": {"v": 1}},
            ),
            HookResult(
                action=HookResultAction.MODIFY,
                modified_payload={"sanitized_arguments": {"v": 2}},
            ),
        ]
        outcome = interpret_hook_results(HookPoint.PRE_TOOL_USE, results)
        assert outcome.modifications["sanitized_arguments"] == {"v": 2}
