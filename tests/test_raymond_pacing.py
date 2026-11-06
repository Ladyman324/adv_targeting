import sys
from pathlib import Path
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from raymond_pacing import ActionPacer


class PacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_dwell_and_response_quiet_period(self):
        now = [0.0]
        waits = []
        async def sleep(delay):
            waits.append(delay)
            now[0] += delay
        pacer = ActionPacer(45, 50, clock=lambda:now[0], sleep=sleep)
        await pacer.before('input')
        pacer.response(SimpleNamespace(url='https://www.raymondjames.com/dotcom/api/x?page=1&sessiontoken=secret', status=200))
        now[0] += 10
        await pacer.before('Next')
        self.assertEqual([50, 35], waits)
        self.assertNotIn('secret', str(pacer.events))
        self.assertNotIn('sessiontoken', str(pacer.events))

    async def test_already_elapsed_gap_needs_no_extra_delay(self):
        now = [0.0]
        pacer = ActionPacer(45, 0, clock=lambda:now[0])
        pacer.response(SimpleNamespace(url='https://www.raymondjames.com/dotcom/api/x', status=200))
        now[0] = 60
        await pacer.before('Next')
        self.assertEqual(0, pacer.events[-1]['waitSeconds'])

    async def test_response_during_sleep_extends_quiet_deadline(self):
        now = [0.0]
        waits = []
        pacer = None
        async def sleep(delay):
            waits.append(delay)
            if len(waits) == 1:
                now[0] = 40
                pacer.response(SimpleNamespace(url='https://www.raymondjames.com/dotcom/api/x', status=200))
                now[0] = 50
            else: now[0] += delay
        pacer = ActionPacer(45, 50, clock=lambda:now[0], sleep=sleep)
        await pacer.before('Next')
        self.assertEqual([50, 35], waits)
        self.assertEqual(85, now[0])

    def test_invalid_policy_rejected(self):
        for value in (-1, True, 3601, '45'):
            with self.subTest(value=value), self.assertRaises(ValueError): ActionPacer(value)
