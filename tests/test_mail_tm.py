import unittest
from unittest.mock import AsyncMock, patch

from mail_tm import (
    Account,
    MailMessage,
    MailTimeoutError,
    MailTM,
    MailTMError,
    _items,
    _MailRateLimitError,
    _retry_after_seconds,
)


class ParsingTests(unittest.TestCase):
    def test_retry_after(self):
        self.assertEqual(_retry_after_seconds("15"), 15)
        self.assertEqual(_retry_after_seconds("-10"), 0)
        self.assertEqual(_retry_after_seconds("invalid"), 0)

    def test_items_validation(self):
        self.assertEqual(_items({"hydra:member": [{"id": "1"}]}), [{"id": "1"}])
        for value in (None, 12, [None], {"hydra:member": "wrong"}):
            with self.subTest(value=value), self.assertRaises(MailTMError):
                _items(value)

    def test_code_precedence_and_boundaries(self):
        self.assertEqual(MailMessage("12345", "67890", "", []).code, "12345")
        self.assertIsNone(MailMessage("123456789", "", "", []).code)
        self.assertEqual(MailMessage("", "", "", ["Code: 123456"]).code, "123456")

    def test_credentials_hidden_in_repr(self):
        account = Account("a@b", "secret-password", "1", "secret-token")
        self.assertNotIn("secret", repr(account))


class MailTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_context_manager(self):
        with self.assertRaises(MailTMError):
            await MailTM().create_account()

    async def test_context_closes_session(self):
        mail = MailTM()
        async with mail:
            session = mail.session
        self.assertTrue(session.closed)
        self.assertIsNone(mail.session)

    async def test_timeout(self):
        mail = MailTM()
        with (
            patch.object(mail, "_poll", new=AsyncMock(return_value=[])),
            self.assertRaises(MailTimeoutError),
        ):
            await mail.wait_for_message("token", timeout=0.01, poll_interval=1)

    async def test_rate_limit_honors_retry_after(self):
        mail = MailTM()
        with (
            patch.object(
                mail,
                "_request",
                new=AsyncMock(side_effect=[_MailRateLimitError("GET", "/messages", 12), []]),
            ),
            patch("mail_tm.asyncio.sleep", new_callable=AsyncMock) as sleep,
        ):
            self.assertEqual(await mail._poll("/messages", "token", 5), [])
            sleep.assert_awaited_once_with(12)

    async def test_second_message_ignores_confirmation(self):
        mail = MailTM()
        responses = [
            [{"id": "first"}, {"id": "second"}],
            {"subject": "11111"},
            {"html": "Code: 22222"},
        ]
        with patch.object(mail, "_poll", new=AsyncMock(side_effect=responses)):
            message = await mail.wait_for_message(
                "token", match=lambda m: m.id != "first" and m.code is not None
            )
        self.assertEqual(message.code, "22222")

    async def test_malformed_message_is_domain_error(self):
        mail = MailTM()
        with (
            patch.object(mail, "_poll", new=AsyncMock(side_effect=[[{"id": "1"}], []])),
            self.assertRaises(MailTMError),
        ):
            await mail.wait_for_message("token")

    async def test_false_values_are_not_valid_mail_fields(self):
        mail = MailTM()
        for detail in ({"html": {}}, {"subject": False}, {"from": []}):
            with (
                self.subTest(detail=detail),
                patch.object(mail, "_poll", new=AsyncMock(side_effect=[[{"id": "1"}], detail])),
                self.assertRaises(MailTMError),
            ):
                await mail.wait_for_message("token")
