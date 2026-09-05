"""World Bank API client. No key, no registration.

Two quirks it exists to absorb:

  * failure is not signalled by status code. Asking for a retired indicator
    returns HTTP 200 with [{"message": [{"id": "175", ...}]}] -- shaped nothing
    like a normal response. Parsing positionally would raise IndexError far from
    the cause, so the message form is detected and raised as itself.

  * every response is [metadata, rows]. The row list is null, not empty, when
    there is nothing to return.
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("bench.client")

BASE = "https://api.worldbank.org/v2/"
DEFAULT_UA = "text2sql-bench/0.1 (portfolio project)"


class ApiError(RuntimeError):
    pass


class RetiredIndicator(ApiError):
    """The API answered 200 but said the indicator no longer exists."""


class WorldBankClient:
    def __init__(self, user_agent=DEFAULT_UA, timeout=60.0, retries=3,
                 backoff=2.0, min_interval=0.4):
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.min_interval = min_interval
        self._last_call = 0.0
        self.requests = 0

    def _throttle(self):
        gap = time.monotonic() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_call = time.monotonic()

    def get(self, path, **params):
        params.setdefault("format", "json")
        url = BASE + path + "?" + urllib.parse.urlencode(params)

        last_error = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            self.requests += 1
            request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                break
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code < 500:
                    raise ApiError("%s -> HTTP %s" % (path, exc.code)) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
            if attempt < self.retries:
                delay = self.backoff ** attempt
                log.warning("%s attempt %d/%d failed (%s), retrying in %.0fs",
                            path, attempt, self.retries, last_error, delay)
                time.sleep(delay)
        else:
            raise ApiError("%s failed after %d attempts: %s" % (path, self.retries, last_error))

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("%s returned non-JSON (%d bytes)" % (path, len(body))) from exc

        # The failure shape: a one-element list whose only key is "message".
        if isinstance(payload, list) and payload and isinstance(payload[0], dict) \
                and "message" in payload[0]:
            messages = payload[0]["message"]
            text = "; ".join(
                "%s: %s" % (m.get("key", "?"), m.get("value", "")) for m in messages)
            if any(m.get("id") == "175" for m in messages):
                raise RetiredIndicator("%s -> %s" % (path, text))
            raise ApiError("%s -> %s" % (path, text))

        if not isinstance(payload, list) or len(payload) < 2:
            raise ApiError("%s returned an unexpected shape: %.120r" % (path, payload))

        meta, rows = payload[0], payload[1]
        return meta, (rows or [])

    def economies(self):
        """All 295 economies: 217 countries plus 78 regional and income aggregates."""
        meta, rows = self.get("country", per_page=400)
        if meta.get("pages", 1) > 1:
            raise ApiError("country list paginated unexpectedly (%s pages)" % meta["pages"])
        return rows

    def observations(self, indicator_id, start_year, end_year):
        """Every economy's values for one indicator over a year range."""
        return self.get(
            "country/all/indicator/%s" % indicator_id,
            per_page=25000, date="%d:%d" % (start_year, end_year),
        )
