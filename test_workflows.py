import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from workflows import collect_contacts, invite_contacts, member_ids


def members(*ids):
    return [SimpleNamespace(contact=SimpleNamespace(id=identity)) for identity in ids]


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_pages_and_deduplication(self):
        client = SimpleNamespace(get_chat_members=AsyncMock(side_effect=[(members(1, 2), 44), (members(2, 3), 0)]))
        self.assertEqual(await member_ids(client, -1), [1, 2, 3])
        self.assertEqual(client.get_chat_members.await_args_list[1].kwargs['marker'], 44)

    async def test_repeating_pages_fail_closed(self):
        client = SimpleNamespace(get_chat_members=AsyncMock(side_effect=[(members(1), 44), (members(2), 44)]))
        with self.assertRaises(ValueError):
            await member_ids(client, -1)

    async def test_contact_count_excludes_existing_and_self(self):
        client = SimpleNamespace(get_chat_members=AsyncMock(return_value=(members(1, 2, 3, 4), 0)), add_contact=AsyncMock(return_value=SimpleNamespace(id=3)))
        events = []
        result = await collect_contacts(client, -1, 1, {2}, 1, threading.Event(), lambda kind, data: events.append((kind, data)))
        client.add_contact.assert_awaited_once_with(3)
        self.assertIn('1 / 1', result)
        self.assertIn(('contact_added', 3), events)

    async def test_invites_exclude_existing_and_verify(self):
        client = SimpleNamespace(get_chat_members=AsyncMock(side_effect=[(members(1, 2), 0), (members(1, 2, 3), 0)]), invite_users_to_group=AsyncMock(return_value=None))
        result = await invite_contacts(client, -1, 1, [1, 2, 3, 3, 4], 1, threading.Event(), lambda *args: None)
        client.invite_users_to_group.assert_awaited_once_with(-1, [3], show_history=False)
        self.assertIn('1 / 1', result)

    async def test_membership_failure_prevents_invites(self):
        client = SimpleNamespace(get_chat_members=AsyncMock(side_effect=RuntimeError()), invite_users_to_group=AsyncMock())
        with self.assertRaises(RuntimeError):
            await invite_contacts(client, -1, 2, [3, 4], 1, threading.Event(), lambda *args: None)
        client.invite_users_to_group.assert_not_awaited()

    async def test_unverified_invite_stops_without_retry(self):
        client = SimpleNamespace(get_chat_members=AsyncMock(return_value=(members(1), 0)), invite_users_to_group=AsyncMock())
        with self.assertRaises(ValueError):
            await invite_contacts(client, -1, 2, [3, 4], 1, threading.Event(), lambda *args: None)
        self.assertEqual(client.invite_users_to_group.await_count, 1)

    async def test_stop_keeps_partial_contacts(self):
        stop = threading.Event()
        client = SimpleNamespace(get_chat_members=AsyncMock(return_value=(members(3, 4), 0)), add_contact=AsyncMock(return_value=SimpleNamespace(id=3)))
        def emit(kind, data):
            if kind == 'contact_added':
                stop.set()
        result = await collect_contacts(client, -1, 2, set(), 1, stop, emit)
        self.assertEqual(client.add_contact.await_count, 1)
        self.assertIn('остановлено', result)


if __name__ == '__main__':
    unittest.main()
