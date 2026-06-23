import os
import sys
import types
import unittest
from unittest.mock import patch

httpx_stub = types.ModuleType("httpx")
httpx_stub.RequestError = Exception
httpx_stub.AsyncClient = object
sys.modules.setdefault("httpx", httpx_stub)

nio_stub = types.ModuleType("nio")
nio_stub.AsyncClient = object
nio_stub.RoomMessageText = object
nio_stub.ReactionEvent = object
sys.modules.setdefault("nio", nio_stub)

from keel_matrix_bot import ApprovalMemory
import keel_matrix_bot


class FakePool:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


class ApprovalMemoryPostgresTests(unittest.TestCase):
    def test_database_config_requires_host_user_and_password(self):
        memory = ApprovalMemory()

        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(memory._database_config_from_env())

        with patch.dict(os.environ, {"POSTGRES_HOST": "postgres"}, clear=True):
            self.assertIsNone(memory._database_config_from_env())

    def test_database_config_uses_postgres_environment(self):
        memory = ApprovalMemory()

        with patch.dict(
            os.environ,
            {
                "POSTGRES_HOST": "postgres.example.svc.cluster.local",
                "POSTGRES_PORT": "15432",
                "POSTGRES_DB": "keel_state",
                "POSTGRES_USER": "keel_bot",
                "POSTGRES_PASSWORD": "secret",
            },
            clear=True,
        ):
            config = memory._database_config_from_env()

        self.assertEqual(config["host"], "postgres.example.svc.cluster.local")
        self.assertEqual(config["port"], 15432)
        self.assertEqual(config["database"], "keel_state")
        self.assertEqual(config["user"], "keel_bot")
        self.assertEqual(config["password"], "secret")
        self.assertEqual(config["server_settings"]["application_name"], "keel-matrix-bot")

    def test_postgres_state_is_required_when_loading(self):
        memory = ApprovalMemory()

        with patch.object(keel_matrix_bot, "asyncpg", object()):
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "PostgreSQL state storage is required"):
                    memory._ensure_loaded()

    def test_apply_state_restores_sets_and_dicts(self):
        memory = ApprovalMemory()

        memory._apply_state(
            {
                "notified_approvals": ["approval-1"],
                "approval_timestamps": {"approval-1": "2026-06-05T12:00:00"},
                "approval_identifiers": {"approval-1": "deployment/app/app:latest"},
                "release_notes_urls": {"app": "https://example.com/releases"},
                "auto_approve_targets": ["app"],
                "auto_approve_failures": {"approval-2": {"reason": "404"}},
            }
        )

        self.assertEqual(memory.notified_approvals, {"approval-1"})
        self.assertEqual(memory.auto_approve_targets, {"app"})
        self.assertEqual(memory.approval_timestamps["approval-1"], "2026-06-05T12:00:00")
        self.assertEqual(memory.approval_identifiers["approval-1"], "deployment/app/app:latest")
        self.assertEqual(memory.release_notes_urls["app"], "https://example.com/releases")
        self.assertEqual(memory.auto_approve_failures["approval-2"]["reason"], "404")

    def test_legacy_id_based_rows_load_without_error_and_do_not_match(self):
        """Old persisted state was keyed by Keel's volatile UUID id. After the
        switch to stable dedup keys those rows still load fine (schema is
        unchanged) but simply never match a live approval, so they are harmless
        and get reconciled away on the next refresh."""
        memory = ApprovalMemory()
        memory._apply_state(
            {
                "notified_approvals": ["legacy-uuid-1234"],
                "approval_identifiers": {"legacy-uuid-1234": "deployment/app/app:latest"},
            }
        )

        memory._loaded = True
        memory._save_state = lambda: None

        live = keel_matrix_bot.Approval(
            id="fresh-uuid-9999",
            provider="kubernetes",
            identifier="deployment/app/app:latest",
            message="m",
            current_version="1.0.0",
            new_version="1.0.1",
            created_at="2026-06-23T00:00:00Z",
        )

        # Legacy id row must not suppress the genuinely new logical update.
        self.assertFalse(memory.is_approved(live))
        # Reconciliation drops the orphaned legacy entry.
        memory.refresh_from_approvals([live])
        self.assertNotIn("legacy-uuid-1234", memory.notified_approvals)

    def test_apply_state_restores_reaction_targets(self):
        memory = ApprovalMemory()
        memory._apply_state(
            {"reaction_targets": {"$evt:1": "deployment/app/app:latest"}}
        )
        self.assertEqual(memory.reaction_targets["$evt:1"], "deployment/app/app:latest")

    def test_reaction_target_mapping_is_persisted(self):
        memory = ApprovalMemory()
        memory._loaded = True
        saved = {}
        memory._save_state = lambda: saved.update(
            {"reaction_targets": dict(memory.reaction_targets)}
        )

        memory.record_reaction_target("$evt:1", "deployment/app/app:latest")

        # Mapping is in the persisted snapshot, so it survives a pod restart.
        self.assertEqual(saved["reaction_targets"]["$evt:1"], "deployment/app/app:latest")
        self.assertEqual(memory.get_reaction_target("$evt:1"), "deployment/app/app:latest")

    def test_save_state_includes_reaction_targets(self):
        memory = ApprovalMemory()
        memory._loaded = True
        memory.reaction_targets = {"$evt:1": "deployment/app/app:latest"}
        captured = {}
        memory._run_db_with_pool = lambda op: None
        # Intercept the dict that _save_state builds by monkeypatching _save_all_to_db
        original = memory._save_all_to_db

        async def capture(pool, data):
            captured.update(data)

        memory._save_all_to_db = capture
        # _save_state passes a lambda to _run_db_with_pool; run it directly here.
        import asyncio as _asyncio

        def run(op):
            _asyncio.run(op(None))

        memory._run_db_with_pool = run
        memory._save_state()

        self.assertIn("reaction_targets", captured)
        self.assertEqual(captured["reaction_targets"]["$evt:1"], "deployment/app/app:latest")

    def test_reset_state_clears_all_persistent_fields(self):
        memory = ApprovalMemory()
        memory.notified_approvals = {"approval-1"}
        memory.approval_timestamps = {"approval-1": "now"}
        memory.approval_identifiers = {"approval-1": "deployment/app/app:latest"}
        memory.release_notes_urls = {"app": "https://example.com"}
        memory.auto_approve_targets = {"app"}
        memory.auto_approve_failures = {"approval-1": {"reason": "error"}}
        memory.reaction_targets = {"$evt:1": "deployment/app/app:latest"}

        memory._reset_state()

        self.assertEqual(memory.notified_approvals, set())
        self.assertEqual(memory.approval_timestamps, {})
        self.assertEqual(memory.approval_identifiers, {})
        self.assertEqual(memory.release_notes_urls, {})
        self.assertEqual(memory.auto_approve_targets, set())
        self.assertEqual(memory.auto_approve_failures, {})
        self.assertEqual(memory.reaction_targets, {})


class ApprovalMemoryPostgresAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_db_with_pool_works_inside_running_event_loop(self):
        memory = ApprovalMemory()

        async def fake_with_pool(operation):
            return await operation("pool")

        async def operation(pool):
            return f"used-{pool}"

        with patch.object(memory, "_with_db_pool", new=fake_with_pool):
            result = memory._run_db_with_pool(operation)

        self.assertEqual(result, "used-pool")

    async def test_load_all_from_db_returns_keyed_state(self):
        memory = ApprovalMemory()

        class Pool:
            async def fetch(self, sql):
                self.sql = sql
                return [
                    {"key": "notified_approvals", "value": ["approval-1"]},
                    {"key": "release_notes_urls", "value": {"app": "https://example.com"}},
                ]

        data = await memory._load_all_from_db(Pool())

        self.assertEqual(data["notified_approvals"], ["approval-1"])
        self.assertEqual(data["release_notes_urls"]["app"], "https://example.com")

    async def test_load_all_from_db_decodes_asyncpg_jsonb_strings(self):
        memory = ApprovalMemory()

        class Pool:
            async def fetch(self, sql):
                return [
                    {"key": "approval_identifiers", "value": '{"approval-1": "deployment/app/app:latest"}'},
                    {"key": "auto_approve_targets", "value": '["app"]'},
                ]

        data = await memory._load_all_from_db(Pool())

        self.assertEqual(data["approval_identifiers"], {"approval-1": "deployment/app/app:latest"})
        self.assertEqual(data["auto_approve_targets"], ["app"])

    async def test_save_all_to_db_upserts_json_values(self):
        memory = ApprovalMemory()
        pool = FakePool()

        await memory._save_all_to_db(
            pool,
            {
                "notified_approvals": ["approval-1"],
                "release_notes_urls": {"app": "https://example.com"},
            },
        )

        self.assertEqual(len(pool.executed), 2)
        self.assertIn("$2::jsonb", pool.executed[0][0])
        self.assertEqual(pool.executed[0][1][0], "notified_approvals")
        self.assertEqual(pool.executed[0][1][1], '["approval-1"]')
        self.assertEqual(pool.executed[1][1][0], "release_notes_urls")
        self.assertEqual(pool.executed[1][1][1], '{"app": "https://example.com"}')


if __name__ == "__main__":
    unittest.main()
