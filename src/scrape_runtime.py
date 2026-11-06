"""Small shared runtime safeguards; no matching or publication authority."""
from collections import Counter, deque


class SourceUnhealthy(RuntimeError):
    pass


class AcquisitionProgress:
    """Fail fast on systemic transport/parser failure, not isolated departures."""
    def __init__(self, window=20, failure_fraction=.8, preflight=5):
        self.window = window
        self.failure_fraction = failure_fraction
        self.preflight = preflight
        self.recent = deque(maxlen=window)
        self.counts = Counter()
        self.attempted = 0

    def observe(self, status, http_status=None):
        self.attempted += 1
        self.counts[status] += 1
        failed = status in {"http_error", "request_error", "invalid_html", "not_found"}
        self.recent.append(failed)
        if self.attempted == self.preflight and all(self.recent):
            raise SourceUnhealthy("Source preflight failed: no usable profile responses in first "
                + str(self.preflight) + " attempts; checkpoint retained")
        if len(self.recent) == self.window and sum(self.recent) >= self.window*self.failure_fraction:
            raise SourceUnhealthy("Source circuit breaker: " + str(sum(self.recent)) + "/"
                + str(self.window) + " recent profiles failed; checkpoint retained")

    def summary(self):
        return {"attempted":self.attempted, "statuses":dict(self.counts),
                "usable":sum(self.counts[k] for k in ("ok","field_missing","field_empty"))}
