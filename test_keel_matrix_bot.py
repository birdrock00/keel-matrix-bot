import unittest
import sys
import types

httpx_stub = types.ModuleType("httpx")
httpx_stub.RequestError = Exception
httpx_stub.AsyncClient = object
sys.modules.setdefault("httpx", httpx_stub)

nio_stub = types.ModuleType("nio")
nio_stub.AsyncClient = object
nio_stub.RoomMessageText = object
sys.modules.setdefault("nio", nio_stub)

from keel_matrix_bot import (
    Approval,
    KeelMatrixBot,
    format_approval_message,
    format_approvals_list,
    get_action_gerund,
    get_approval_action_url,
    get_deny_action_url,
    render_async_approval_action_page,
)


def sample_approval(identifier="namespace/deployment/app:1.2.3"):
    return Approval(
        id="approval-1",
        provider="kubernetes",
        identifier=identifier,
        message="New image available",
        current_version="1.2.2",
        new_version="1.2.3",
        created_at="2026-05-31T12:34:56Z",
        repository_name="docker.io/example/app",
        repository_tag="1.2.3",
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
            "[Deny app](https://bot.example/deny?identifier=namespace%2Fdeployment%2Fapp%3A1.2.3)",
            message,
        )
        self.assertIn("`approve namespace/deployment/app:1.2.3`", message)
        self.assertIn("`reject namespace/deployment/app:1.2.3`", message)

    def test_approvals_list_includes_deny_link_for_each_approval(self):
        message = format_approvals_list([sample_approval()], approve_base_url="https://bot.example")

        self.assertIn("[Approve app](https://bot.example/approve?identifier=namespace%2Fdeployment%2Fapp%3A1.2.3)", message)
        self.assertIn("[Deny app](https://bot.example/deny?identifier=namespace%2Fdeployment%2Fapp%3A1.2.3)", message)

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

        self.assertIn("<title>Deny Update</title>", page)
        self.assertIn('/api/deny?identifier=ns%2Fapp%3A1', page)
        self.assertIn("Submitting deny", page)

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

        status_code, message = await bot.process_http_approval_action_request(" ns/app:1 ", "deny")

        self.assertEqual(status_code, 200)
        self.assertEqual(calls, [("!room:example", "ns/app:1", "reject")])
        self.assertIn("Deny request submitted", message)


if __name__ == "__main__":
    unittest.main()
