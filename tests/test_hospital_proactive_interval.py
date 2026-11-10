import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1] / 'plugins' / 'astrbot_plugin_hospital_proactive'


def load_main():
    names = ['astrbot', 'astrbot.api', 'astrbot.api.event', 'astrbot.api.star',
             'astrbot.core', 'astrbot.core.agent', 'astrbot.core.agent.message',
             'astrbot.core.message', 'astrbot.core.message.components', 'test_proactive']
    modules = {name: types.ModuleType(name) for name in names}
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
        def on_llm_response(*args, **kwargs):
            return lambda fn: fn

        @staticmethod
        def on_decorating_result(*args, **kwargs):
            return lambda fn: fn

        @staticmethod
        def after_message_sent(*args, **kwargs):
            return lambda fn: fn

    modules['astrbot.api.event'].AstrMessageEvent = type('AstrMessageEvent', (), {})
    modules['astrbot.api.event'].filter = Filter
    modules['astrbot.api.star'].Context = type('Context', (), {})
    modules['astrbot.api.star'].Star = type('Star', (), {})
    modules['astrbot.api.star'].register = lambda *args, **kwargs: (lambda cls: cls)
    modules['astrbot.core.agent.message'].TextPart = type('TextPart', (), {})
    modules['astrbot.core.message.components'].At = type('At', (), {})
    modules['astrbot.core.message.components'].Plain = type('Plain', (), {})
    modules['test_proactive'].__path__ = [str(ROOT)]
    with patch.dict(sys.modules, modules):
        return importlib.import_module('test_proactive.main')


main = load_main()


class IntervalOpenTest(unittest.TestCase):
    def test_first_reply_is_allowed(self):
        self.assertTrue(main.interval_open(0, 1000, 3600))

    def test_blocks_before_one_hour_and_allows_at_boundary(self):
        self.assertFalse(main.interval_open(1000, 4599, 3600))
        self.assertTrue(main.interval_open(1000, 4600, 3600))

    def test_zero_interval_disables_limit(self):
        self.assertTrue(main.interval_open(1000, 1001, 0))


class ReplyIntervalTest(unittest.TestCase):
    def instance(self, value):
        obj = main.HospitalProactive.__new__(main.HospitalProactive)
        obj.config = {'user_reply_interval_seconds': value}
        return obj

    def test_default_and_configured_value(self):
        self.assertEqual(self.instance(3600).reply_interval(), 3600)
        self.assertEqual(self.instance(0).reply_interval(), 0)

    def test_invalid_value_falls_back_to_one_hour(self):
        self.assertEqual(self.instance('bad').reply_interval(), 3600)
        self.assertEqual(self.instance(None).reply_interval(), 3600)


class ProactiveHintTest(unittest.TestCase):
    def test_hint_is_appended_after_body(self):
        self.assertEqual(main.with_proactive_hint('检查电源'),
                         '检查电源\n\n`如需继续提问，请 @ 我`')

    def test_hint_is_not_duplicated(self):
        once = main.with_proactive_hint('检查电源')
        self.assertEqual(main.with_proactive_hint(once), once)

    def test_empty_body_only_contains_hint(self):
        self.assertEqual(main.with_proactive_hint(''),
                         '`如需继续提问，请 @ 我`')


if __name__ == '__main__':
    unittest.main()
