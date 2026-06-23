import unittest
import sys
import types
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

import keel_matrix_bot
from keel_matrix_bot import (
    Approval,
    ApprovalMemory,
    KeelMatrixBot,
    approval_dedup_key,
    approval_notification_txn_id,
    format_approval_message,
    format_approvals_list,
    get_action_gerund,
    get_approval_action_url,
    get_deny_action_url,
    render_async_approval_action_page,
    send_matrix_message_with_event_id,
)


def make_bot():
    bot = KeelMatrixBot(
        homeserver="https://matrix.example",
        matrix_username="bot",
        matrix_password="secret",
        room_id="!room:example",
        keel_url="https://keel.example",
    )
    bot.user_id = "@bot:example"
    bot.access_token = "token"
    return bot


class FakeReaction:
    """Mimics nio.ReactionEvent's relevant fields."""

    def __init__(self, sender, event_id, reacts_to, key):
        self.sender = sender
        self.event_id = event_id
        self.reacts_to = reacts_to
        self.key = key


class FakeRoom:
    def __init__(self, room_id="!room:example"):
        self.room_id = room_id


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def sample_approval(
    identifier="namespace/deployment/app:1.2.3",
    approval_id="approval-1",
    current_version="1.2.2",
    new_version="1.2.3",
    repository_digest="",
):
    return Approval(
        id=approval_id,
        provider="kubernetes",
        identifier=identifier,
        message="New image available",
        current_version=current_version,
        new_version=new_version,
        created_at="2026-05-31T12:34:56Z",
        repository_name="docker.io/example/app",
        repository_tag="1.2.3",
        repository_digest=repository_digest,
    )


def loaded_memory():
    """Return an ApprovalMemory that skips PostgreSQL load/save for unit tests."""
    memory = ApprovalMemory()
    memory._loaded = True
    memory._save_state = lambda: None
    return memory


class ApprovalDedupKeyTests(unittest.TestCase):
    def test_dedup_key_is_independent_of_volatile_keel_id(self):
        a1 = sample_approval(approval_id="uuid-aaa")
        a2 = sample_approval(approval_id="uuid-bbb")  # Keel re-minted the id
        self.assertEqual(approval_dedup_key(a1), approval_dedup_key(a2))

    def test_dedup_key_changes_with_version_transition(self):
        a1 = sample_approval(current_version="1.2.2", new_version="1.2.3")
        a2 = sample_approval(current_version="1.2.3", new_version="1.2.4")
        self.assertNotEqual(approval_dedup_key(a1), approval_dedup_key(a2))

    def test_dedup_key_includes_digest_when_present(self):
        a1 = sample_approval(repository_digest="sha256:aaa")
        a2 = sample_approval(repository_digest="sha256:bbb")
        self.assertNotEqual(approval_dedup_key(a1), approval_dedup_key(a2))
        self.assertIn("sha256:aaa", approval_dedup_key(a1))

    def test_rotated_keel_id_is_not_renotified(self):
        memory = loaded_memory()
        first = sample_approval(approval_id="uuid-aaa")
        # First poll: brand-new logical update, should be notified.
        self.assertEqual([a.id for a in memory.get_new_approvals([first])], ["uuid-aaa"])
        memory.mark_as_notified([first])

        # Keel rotates the UUID for the SAME logical update.
        rotated = sample_approval(approval_id="uuid-bbb")
        self.assertTrue(memory.is_approved(rotated))
        self.assertEqual(memory.get_new_approvals([rotated]), [])

    def test_refresh_does_not_evict_logical_approval_when_id_rotates(self):
        memory = loaded_memory()
        first = sample_approval(approval_id="uuid-aaa")
        memory.mark_as_notified([first])

        # Reconcile against the same logical approval with a rotated id.
        rotated = sample_approval(approval_id="uuid-bbb")
        memory.refresh_from_approvals([rotated])

        # Still considered notified -> no duplicate notification.
        self.assertTrue(memory.is_approved(rotated))
        self.assertIn(approval_dedup_key(rotated), memory.notified_approvals)

    def test_refresh_evicts_genuinely_absent_approvals(self):
        memory = loaded_memory()
        gone = sample_approval(identifier="ns/old:1", current_version="1", new_version="2")
        memory.mark_as_notified([gone])
        memory.refresh_from_approvals([])  # nothing pending anymore
        self.assertEqual(memory.notified_approvals, set())

    def test_notification_txn_id_is_deterministic_and_order_independent(self):
        a = sample_approval(identifier="ns/a:1")
        b = sample_approval(identifier="ns/b:1")
        self.assertEqual(
            approval_notification_txn_id([a, b]),
            approval_notification_txn_id([b, a]),
        )
        self.assertEqual(
            approval_notification_txn_id(a),
            approval_notification_txn_id([a]),
        )
        self.assertNotEqual(
            approval_notification_txn_id([a]),
            approval_notification_txn_id([b]),
        )


class ApprovalActionMessageTests(unittest.TestCase):
    def test_approval_message_includes_approve_and_deny_links(self):
        approval = sample_approval()

        message = format_approval_message(approval, "https://bot.example")

        self.assertIn(
            "[Approve app](https://bot.example/approve?identifier=namespace%2Fdeployment%2Fapp%3A1.2.3)",
            message,
        )
        self.assertIn(
            "[Deny approval app](https://bot.example/deny?identifier=namespace%2Fdeployment%2Fapp%3A1.2.3)",
            message,
        )
        self.assertIn("`approve namespace/deployment/app:1.2.3`", message)
        self.assertIn("`reject namespace/deployment/app:1.2.3`", message)

    def test_approvals_list_includes_deny_link_for_each_approval(self):
        message = format_approvals_list([sample_approval()], approve_base_url="https://bot.example")

        self.assertIn("[Approve app](https://bot.example/approve?identifier=namespace%2Fdeployment%2Fapp%3A1.2.3)", message)
        self.assertIn("[Deny approval app](https://bot.example/deny?identifier=namespace%2Fdeployment%2Fapp%3A1.2.3)", message)

    def test_deny_action_url_uses_public_deny_route_and_encoded_identifier(self):
        self.assertEqual(
            get_deny_action_url("ns/app with space:1", "https://bot.example/"),
            "https://bot.example/deny?identifier=ns%2Fapp%20with%20space%3A1",
        )
        self.assertEqual(
            get_approval_action_url("ns/app:1", "reject", "https://bot.example"),
            "https://bot.example/deny?identifier=ns%2Fapp%3A1",
        )

    def test_deny_page_posts_to_deny_api(self):
        page = render_async_approval_action_page("ns/app:1", "deny")

        self.assertIn("<title>Deny approval Update</title>", page)
        self.assertIn('/api/deny?identifier=ns%2Fapp%3A1', page)
        self.assertIn("Submitting deny approval", page)

    def test_approval_action_gerunds_are_spelled_correctly(self):
        self.assertEqual(get_action_gerund("approve"), "Approving")
        self.assertEqual(get_action_gerund("reject"), "Rejecting")
        self.assertNotEqual(get_action_gerund("approve"), "Approve" + "ing")


class HttpApprovalActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_deny_request_maps_to_keel_reject_action(self):
        bot = KeelMatrixBot(
            homeserver="https://matrix.example",
            matrix_username="bot",
            matrix_password="secret",
            room_id="!room:example",
            keel_url="https://keel.example",
        )
        calls = []

        async def fake_handle(room_id, identifier, action):
            calls.append((room_id, identifier, action))

        bot.handle_approve_reject = fake_handle

        async def fake_fetch_pending_approvals(*args, **kwargs):
            return [sample_approval("ns/app:1")], {"status": 200}

        with patch.object(
            keel_matrix_bot,
            "fetch_pending_approvals",
            new=fake_fetch_pending_approvals,
        ):
            status_code, message = await bot.process_http_approval_action_request(" ns/app:1 ", "deny")

        self.assertEqual(status_code, 202)
        self.assertEqual(calls, [("!room:example", "ns/app:1", "reject")])
        self.assertIn("Deny approval request submitted", message)


class SendMessageEventIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_captures_event_id_from_response(self):
        class FakeAsyncClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def put(self, url, json=None, headers=None):
                return FakeResponse(200, {"event_id": "$evt:example"})

        with patch.object(keel_matrix_bot.httpx, "AsyncClient", FakeAsyncClient):
            success, event_id = await send_matrix_message_with_event_id(
                "https://matrix.example", "@bot:example", "token", "!room:example", "hi"
            )

        self.assertTrue(success)
        self.assertEqual(event_id, "$evt:example")

    async def test_send_returns_none_event_id_on_failure(self):
        class FakeAsyncClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def put(self, url, json=None, headers=None):
                return FakeResponse(403, {}, text="forbidden")

        with patch.object(keel_matrix_bot.httpx, "AsyncClient", FakeAsyncClient):
            success, event_id = await send_matrix_message_with_event_id(
                "https://matrix.example", "@bot:example", "token", "!room:example", "hi"
            )

        self.assertFalse(success)
        self.assertIsNone(event_id)


class ReactionMappingTests(unittest.TestCase):
    def test_mapping_resolves_identifier(self):
        memory = loaded_memory()
        memory.record_reaction_target("$evt:1", "ns/app:1")
        self.assertEqual(memory.get_reaction_target("$evt:1"), "ns/app:1")
        self.assertIsNone(memory.get_reaction_target("$missing"))

    def test_mapping_persists_through_state_roundtrip(self):
        memory = loaded_memory()
        memory.record_reaction_target("$evt:1", "ns/app:1")
        data = {
            "reaction_targets": memory.reaction_targets,
        }
        restored = ApprovalMemory()
        restored._apply_state(data)
        self.assertEqual(restored.reaction_targets.get("$evt:1"), "ns/app:1")

    def test_mapping_is_bounded(self):
        memory = loaded_memory()
        for i in range(600):
            memory.record_reaction_target(f"$evt:{i}", f"ns/app:{i}")
        self.assertLessEqual(len(memory.reaction_targets), 500)


class ReactionHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Each test gets an isolated, in-memory ApprovalMemory so the global
        # state (and PostgreSQL) is never touched.
        self.memory = loaded_memory()
        self._patcher = patch.object(keel_matrix_bot, "approval_memory", self.memory)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    async def test_thumbs_up_calls_handle_approve_reject_with_approve(self):
        bot = make_bot()
        self.memory.record_reaction_target("$msg:1", "ns/app:1")
        calls = []

        async def fake_handle(room_id, identifier, action):
            calls.append((room_id, identifier, action))

        bot.handle_approve_reject = fake_handle

        await bot.on_reaction(
            FakeRoom(),
            FakeReaction("@user:example", "$react:1", "$msg:1", "👍"),
        )

        self.assertEqual(calls, [("!room:example", "ns/app:1", "approve")])

    async def test_thumbs_down_maps_to_reject(self):
        bot = make_bot()
        self.memory.record_reaction_target("$msg:1", "ns/app:1")
        calls = []

        async def fake_handle(room_id, identifier, action):
            calls.append((room_id, identifier, action))

        bot.handle_approve_reject = fake_handle

        await bot.on_reaction(
            FakeRoom(),
            FakeReaction("@user:example", "$react:1", "$msg:1", "👎"),
        )

        self.assertEqual(calls, [("!room:example", "ns/app:1", "reject")])

    async def test_unknown_emoji_is_ignored(self):
        bot = make_bot()
        self.memory.record_reaction_target("$msg:1", "ns/app:1")
        calls = []

        async def fake_handle(room_id, identifier, action):
            calls.append((room_id, identifier, action))

        bot.handle_approve_reject = fake_handle

        await bot.on_reaction(
            FakeRoom(),
            FakeReaction("@user:example", "$react:1", "$msg:1", "🎉"),
        )

        self.assertEqual(calls, [])

    async def test_self_reaction_is_ignored(self):
        bot = make_bot()
        self.memory.record_reaction_target("$msg:1", "ns/app:1")
        calls = []

        async def fake_handle(room_id, identifier, action):
            calls.append((room_id, identifier, action))

        bot.handle_approve_reject = fake_handle

        await bot.on_reaction(
            FakeRoom(),
            FakeReaction(bot.user_id, "$react:1", "$msg:1", "👍"),
        )

        self.assertEqual(calls, [])

    async def test_mapping_miss_falls_back_to_single_pending_approval(self):
        bot = make_bot()
        # No reaction_targets recorded -> mapping miss; exactly one pending approval.
        calls = []

        async def fake_handle(room_id, identifier, action):
            calls.append((room_id, identifier, action))

        bot.handle_approve_reject = fake_handle

        async def fake_fetch(*args, **kwargs):
            return [sample_approval("ns/only:1")], {"status": 200}

        with patch.object(keel_matrix_bot, "fetch_pending_approvals", new=fake_fetch):
            await bot.on_reaction(
                FakeRoom(),
                FakeReaction("@user:example", "$unknownmsg", "$unknownmsg", "👍"),
            )

        self.assertEqual(calls, [("!room:example", "ns/only:1", "approve")])

    async def test_mapping_miss_with_ambiguous_pending_is_ignored(self):
        bot = make_bot()
        calls = []

        async def fake_handle(room_id, identifier, action):
            calls.append((room_id, identifier, action))

        bot.handle_approve_reject = fake_handle

        async def fake_fetch(*args, **kwargs):
            return [sample_approval("ns/a:1"), sample_approval("ns/b:1")], {"status": 200}

        with patch.object(keel_matrix_bot, "fetch_pending_approvals", new=fake_fetch):
            await bot.on_reaction(
                FakeRoom(),
                FakeReaction("@user:example", "$unknownmsg", "$unknownmsg", "👍"),
            )

        self.assertEqual(calls, [])

    async def test_duplicate_reaction_event_is_processed_once(self):
        bot = make_bot()
        self.memory.record_reaction_target("$msg:1", "ns/app:1")
        calls = []

        async def fake_handle(room_id, identifier, action):
            calls.append((room_id, identifier, action))

        bot.handle_approve_reject = fake_handle

        reaction = FakeReaction("@user:example", "$react:dup", "$msg:1", "👍")
        await bot.on_reaction(FakeRoom(), reaction)
        await bot.on_reaction(FakeRoom(), reaction)

        self.assertEqual(len(calls), 1)


class ApprovalMessageReactionHintTests(unittest.TestCase):
    def test_message_mentions_emoji_reactions(self):
        message = format_approval_message(sample_approval(), "https://bot.example")
        self.assertIn("React 👍 to approve", message)
        self.assertIn("👎 to reject", message)


if __name__ == "__main__":
    unittest.main()
