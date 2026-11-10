import importlib
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1] / 'plugins' / 'astrbot_plugin_hospital_queue'


def load_main():
    names = ['aiohttp', 'astrbot', 'astrbot.api', 'astrbot.api.event', 'astrbot.api.star',
             'astrbot.core', 'astrbot.core.agent', 'astrbot.core.agent.message',
             'astrbot.core.message', 'astrbot.core.message.components', 'test_queue']
    modules = {name: types.ModuleType(name) for name in names}
    modules['aiohttp'].ClientError = type('ClientError', (Exception,), {})
    modules['aiohttp'].ClientTimeout = lambda *args, **kwargs: None
    modules['aiohttp'].ClientSession = type('ClientSession', (), {})
    modules['astrbot.api'].AstrBotConfig = dict
    modules['astrbot.api'].logger = types.SimpleNamespace(
        info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None)

    class Filter:
        class EventMessageType:
            ALL = 'ALL'

        @staticmethod
        def event_message_type(*args, **kwargs):
            return lambda fn: fn

        @staticmethod
        def on_llm_request(*args, **kwargs):
            return lambda fn: fn

        @staticmethod
        def llm_tool(*args, **kwargs):
            return lambda fn: fn

    modules['astrbot.api.event'].AstrMessageEvent = type('AstrMessageEvent', (), {})
    modules['astrbot.api.event'].filter = Filter
    modules['astrbot.api.star'].Context = type('Context', (), {})
    modules['astrbot.api.star'].Star = type('Star', (), {})
    modules['astrbot.api.star'].register = lambda *args, **kwargs: (lambda cls: cls)
    modules['astrbot.core.agent.message'].TextPart = type('TextPart', (), {})
    modules['astrbot.core.message.components'].At = type('At', (), {})
    modules['astrbot.core.message.components'].Plain = type('Plain', (), {})
    modules['test_queue'].__path__ = [str(ROOT)]
    with patch.dict(sys.modules, modules):
        return importlib.import_module('test_queue.main')


main = load_main()
UTC = timezone.utc
START = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)


def activity(ends_at):
    return {'activityAt': '2026-11-10T08:00:00Z', 'endsAt': ends_at}


class ActivityEndTest(unittest.TestCase):
    def test_uses_api_end_time(self):
        self.assertEqual(main.activity_end(activity('2026-11-10T10:30:00Z')),
                         datetime(2026, 11, 10, 10, 30, tzinfo=UTC))

    def test_falls_back_to_two_hours_when_missing(self):
        self.assertEqual(main.activity_end(activity(None)), START + timedelta(hours=2))
        self.assertEqual(main.activity_end({'activityAt': '2026-11-10T08:00:00Z'}),
                         START + timedelta(hours=2))

    def test_falls_back_when_end_is_invalid_or_naive(self):
        self.assertEqual(main.activity_end(activity('not-a-date')), START + timedelta(hours=2))
        self.assertEqual(main.activity_end(activity('2026-11-10T10:30:00')),
                         START + timedelta(hours=2))

    def test_missing_start_has_no_end(self):
        self.assertIsNone(main.activity_end({'endsAt': '2026-11-10T10:30:00Z'}))


class DueSlotTest(unittest.TestCase):
    END = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)

    def test_first_slot_after_fifteen_minutes(self):
        self.assertEqual(main.due_slot(START, self.END, START + timedelta(minutes=15)), 1)
        self.assertEqual(main.due_slot(START, self.END, START + timedelta(minutes=16, seconds=59)), 1)

    def test_grace_expires_and_missed_slots_are_not_replayed(self):
        self.assertIsNone(main.due_slot(START, self.END, START + timedelta(minutes=17)))
        self.assertIsNone(main.due_slot(START, self.END, START + timedelta(minutes=20)))
        self.assertEqual(main.due_slot(START, self.END, START + timedelta(minutes=30, seconds=30)), 2)

    def test_last_slot_is_the_end_slot_and_then_stops(self):
        self.assertEqual(main.due_slot(START, self.END, START + timedelta(minutes=105)), 7)
        self.assertEqual(main.due_slot(START, self.END, self.END), 8)
        self.assertIsNone(main.due_slot(START, self.END, self.END + timedelta(minutes=3)))

    def test_activity_longer_than_two_hours_keeps_broadcasting(self):
        end = START + timedelta(hours=4)
        self.assertEqual(main.due_slot(START, end, START + timedelta(hours=2)), 8)
        self.assertEqual(main.due_slot(START, end, START + timedelta(hours=3, minutes=45)), 15)

    def test_short_activity_has_no_slots(self):
        self.assertIsNone(main.due_slot(START, START + timedelta(minutes=10),
                                        START + timedelta(minutes=15)))
        self.assertIsNone(main.due_slot(START, START, START + timedelta(minutes=15)))


class HasWaitingTest(unittest.TestCase):
    def observation(self, count):
        return types.SimpleNamespace(waiting_count=count)

    def test_zero_or_unknown_is_filtered(self):
        self.assertFalse(main.has_waiting('ACTIVITY_ENDED'))
        self.assertFalse(main.has_waiting(self.observation(None)))
        self.assertFalse(main.has_waiting(self.observation(0)))

    def test_positive_count_is_broadcast(self):
        self.assertTrue(main.has_waiting(self.observation(1)))
        self.assertTrue(main.has_waiting(self.observation(12)))


if __name__ == '__main__':
    unittest.main()
