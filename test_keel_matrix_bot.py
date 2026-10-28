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
    format_voter_line,
    get_action_gerund,
    get_approval_action_url,
    get_deny_action_url,
    parse_approvals,
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


class ApprovalVoteStateTests(unittest.TestCase):
    def test_parse_approvals_captures_vote_state(self):
        approvals = parse_approvals([{
            "id": "uuid-1",
            "provider": "kubernetes",
            "identifier": "ns/app:1",
            "message": "New image available",
            "currentVersion": "1",
            "newVersion": "2",
            "createdAt": "2026-05-31T12:34:56Z",
            "votesRequired": 1,
            "votesReceived": 1,
            "rejected": False,
            "voters": {"@bot:example": "2026-05-31T12:35:00.123456789Z"},
            "event": {"repository": {"name": "example/app", "tag": "2"}},
        }])
        self.assertEqual(len(approvals), 1)
        approved = approvals[0]
        self.assertEqual(approved.votes_required, 1)
        self.assertEqual(approved.votes_received, 1)
        self.assertTrue(approved.is_fully_voted())
        self.assertFalse(approved.needs_action())
        self.assertEqual(approved.status_label(), "Approved")
        self.assertIn("@bot:example", approved.voters)

    def test_parse_approvals_defaults_to_pending_without_vote_fields(self):
        approvals = parse_approvals([{
            "id": "uuid-2",
            "identifier": "ns/app:1",
            "message": "m",
            "currentVersion": "1",
            "newVersion": "2",
        }])
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0].votes_received, 0)
        self.assertTrue(approvals[0].needs_action())
        self.assertEqual(approvals[0].status_label(), "Pending")

    def test_rejected_approval_does_not_need_action(self):
        approval = sample_approval()
        approval.rejected = True
        self.assertFalse(approval.needs_action())
        self.assertFalse(approval.is_fully_voted())
        self.assertEqual(approval.status_label(), "Rejected")

    def test_partial_votes_still_need_action(self):
        approval = sample_approval()
        approval.votes_required = 2
        approval.votes_received = 1
        self.assertTrue(approval.needs_action())
        self.assertEqual(approval.status_label(), "Pending")

    def test_voter_line_lists_voters_and_latest_vote_time(self):
        approval = sample_approval()
        approval.voters = {"@bot:example": "2026-05-31T12:35:00.123456789Z"}
        line = format_voter_line(approval)
        self.assertIn("@bot:example", line)
        self.assertIn("2026-05-31T12:35:00", line)
        self.assertEqual(format_voter_line(sample_approval()), "")


class ResolvedApprovalMessageTests(unittest.TestCase):
    def test_approved_approval_message_has_no_action_links(self):
        approval = sample_approval()
        approval.votes_received = 1
        approval.voters = {"@bot:example": "2026-05-31T12:35:00Z"}

        message = format_approval_message(approval, "https://bot.example")

        self.assertIn("Approved — Awaiting Rollout", message)
        self.assertIn("No action needed", message)
        self.assertIn("**Status:** Approved (1/1 votes)", message)
        self.assertNotIn("[Approve app]", message)
        self.assertNotIn("[Deny approval app]", message)
        self.assertNotIn("`approve namespace/deployment/app:1.2.3`", message)

    def test_rejected_approval_message_has_no_action_links(self):
        approval = sample_approval()
        approval.rejected = True

        message = format_approval_message(approval, "https://bot.example")

        self.assertIn("Approval Rejected", message)
        self.assertNotIn("[Approve app]", message)
        self.assertNotIn("[Deny approval app]", message)

    def test_approvals_list_only_offers_links_for_pending(self):
        pending = sample_approval("ns/app:1")
        done = sample_approval("ns/done:1")
        done.votes_received = 1

        message = format_approvals_list([pending, done], approve_base_url="https://bot.example")

        self.assertIn("📋 **Approvals (2)**", message)
        self.assertIn("[Approve app](https://bot.example/approve?identifier=ns%2Fapp%3A1)", message)
        self.assertIn("Status: Approved (1/1 votes)", message)
        self.assertIn("No action needed — this approval is already complete.", message)
        self.assertNotIn("identifier=ns%2Fdone%3A1", message)

    def test_approvals_list_keeps_pending_title_when_all_need_action(self):
        message = format_approvals_list([sample_approval()], approve_base_url="https://bot.example")
        self.assertIn("📋 **Pending Approvals (1)**", message)


class AlreadyApprovedActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._patcher = patch.object(keel_matrix_bot, "approval_memory", loaded_memory())
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    async def test_http_approve_on_already_approved_is_noop(self):
        bot = make_bot()
        calls = []

        async def fake_handle(room_id, identifier, action):
            calls.append((room_id, identifier, action))

        bot.handle_approve_reject = fake_handle
        approved = sample_approval("ns/app:1")
        approved.votes_received = 1

        async def fake_fetch_pending_approvals(*args, **kwargs):
            return [approved], {"status": 200}

        with patch.object(keel_matrix_bot, "fetch_pending_approvals", new=fake_fetch_pending_approvals):
            status_code, message = await bot.process_http_approval_action_request("ns/app:1", "approve")

        self.assertEqual(status_code, 200)
        self.assertEqual(calls, [])
        self.assertIn("already approved", message)
        self.assertIn("No approval submitted", message)

    async def test_http_reject_on_already_approved_is_noop(self):
        bot = make_bot()
        calls = []

        async def fake_handle(room_id, identifier, action):
            calls.append((room_id, identifier, action))

        bot.handle_approve_reject = fake_handle
        approved = sample_approval("ns/app:1")
        approved.votes_received = 1

        async def fake_fetch_pending_approvals(*args, **kwargs):
            return [approved], {"status": 200}

        with patch.object(keel_matrix_bot, "fetch_pending_approvals", new=fake_fetch_pending_approvals):
            status_code, message = await bot.process_http_approval_action_request("ns/app:1", "deny")

        self.assertEqual(status_code, 200)
        self.assertEqual(calls, [])
        self.assertIn("already approved", message)

    async def test_handle_approve_reject_short_circuits_already_approved(self):
        bot = make_bot()
        approved = sample_approval()
        approved.votes_received = 1
        sent = []

        async def fake_send(homeserver, user_id, access_token, room_id, message, txn_id=None):
            sent.append(message)
            return True

        async def fake_fetch(*args, **kwargs):
            return [approved], {"status": 200}

        with patch.object(keel_matrix_bot, "send_matrix_message", new=fake_send), \
             patch.object(keel_matrix_bot, "fetch_pending_approvals", new=fake_fetch):
            status, message = await bot.handle_approve_reject(
                "!room:example", approved.identifier, "approve"
            )

        self.assertEqual(status, 200)
        self.assertIn("Already approved — no action needed", message)
        self.assertFalse(any("Approve submitted" in m for m in sent))


class GetApprovalsResolutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._patcher = patch.object(keel_matrix_bot, "approval_memory", loaded_memory())
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _patch_sinks(self, sent):
        async def fake_send(homeserver, user_id, access_token, room_id, message, txn_id=None):
            sent.append(message)
            return True

        async def fake_send_with_event_id(homeserver, user_id, access_token, room_id, message, txn_id=None):
            sent.append(message)
            return True, "$evt:1"

        async def fake_reaction(*args, **kwargs):
            return True

        return (
            patch.object(keel_matrix_bot, "send_matrix_message", new=fake_send),
            patch.object(keel_matrix_bot, "send_matrix_message_with_event_id", new=fake_send_with_event_id),
            patch.object(keel_matrix_bot, "send_matrix_reaction", new=fake_reaction),
        )

    async def test_pending_and_resolved_approvals_are_shown_separately(self):
        bot = make_bot()
        pending = sample_approval("ns/app:1")
        done = sample_approval("ns/done:1")
        done.votes_received = 1
        sent = []

        async def fake_fetch(*args, **kwargs):
            return [pending, done], {"status": 200}

        send_patch, send_event_patch, reaction_patch = self._patch_sinks(sent)
        with send_patch, send_event_patch, reaction_patch, \
             patch.object(keel_matrix_bot, "fetch_pending_approvals", new=fake_fetch):
            await bot.handle_get_approvals("!room:example")

        joined = "\n".join(sent)
        self.assertIn("📋 **Pending Approvals (1)**", joined)
        self.assertIn("Already approved (1): `ns/done:1`", joined)
        self.assertIn("No action needed (1)", joined)
        # One actionable message for the pending approval only.
        self.assertTrue(any("Update Approval Required" in m and "ns/app:1" in m for m in sent))
        self.assertFalse(any("Update Approval Required" in m and "ns/done:1" in m for m in sent))
        # Header is not duplicated.
        self.assertEqual(sum(1 for m in sent if "Pending Approvals (1)" in m), 1)

    async def test_all_resolved_list_shows_no_pending_and_no_requests(self):
        bot = make_bot()
        done = sample_approval("ns/done:1")
        done.votes_received = 1
        sent = []

        async def fake_fetch(*args, **kwargs):
            return [done], {"status": 200}

        send_patch, send_event_patch, reaction_patch = self._patch_sinks(sent)
        with send_patch, send_event_patch, reaction_patch, \
             patch.object(keel_matrix_bot, "fetch_pending_approvals", new=fake_fetch):
            await bot.handle_get_approvals("!room:example")

        joined = "\n".join(sent)
        self.assertIn("No Pending Approvals", joined)
        self.assertIn("No action needed (1)", joined)
        self.assertFalse(any("Update Approval Required" in m for m in sent))


if __name__ == "__main__":
    unittest.main()
