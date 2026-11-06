"""Measured, deterministic pacing for Raymond-owned UI actions only."""
import asyncio
import json
import time
from urllib.parse import urlsplit, parse_qs


class ActionPacer:
    def __init__(self, quiet_seconds=45, initial_seconds=50, *, clock=None, sleep=None):
        for value in (quiet_seconds, initial_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 3600:
                raise ValueError('Pacing must be between0 and3600seconds')
        self.clock = clock or time.monotonic
        self.sleep = sleep or asyncio.sleep
        self.quiet_seconds = quiet_seconds
        self.initial_seconds = initial_seconds
        self.started = self.clock()
        self.last_response = None
        self.events = []

    def response(self, response, stage='headers'):
        now = self.clock()
        self.last_response = now
        url = urlsplit(response.url)
        query = parse_qs(url.query)
        parameters = {key: query[key][0] for key in ('radius', 'page', 'location') if key in query}
        self.events.append({'kind': 'response_' + stage, 'elapsedSeconds': round(now-self.started, 3),
            'path': url.path, 'parameters': parameters, 'status': response.status})

    async def before(self, action):
        total_delay = 0
        while True:
            deadline = self.started + self.initial_seconds
            if self.last_response is not None:
                deadline = max(deadline, self.last_response + self.quiet_seconds)
            delay = max(0, deadline-self.clock())
            if not delay: break
            print('Source pacing: waiting %.1fs before %s' % (delay, action), flush=True)
            await self.sleep(delay)
            total_delay += delay
        self.events.append({'kind': 'action_ready', 'action': action,
            'elapsedSeconds': round(self.clock()-self.started, 3), 'waitSeconds': round(total_delay, 3)})

    def save(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'quietSeconds': self.quiet_seconds,
            'initialSeconds': self.initial_seconds, 'events': self.events}, indent=2), encoding='utf-8')
