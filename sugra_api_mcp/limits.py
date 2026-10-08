"""Request limits for the HTTP transport, counted in this process's memory, on only with SUGRA_MCP_LIMITS.

Off, which is the default, none of this runs: no Limits object is built and no
request waits at admission. What an off server answers is exactly what it
answered before this module existed.

On, the server adds, in this order for a request to /mcp:

- The budget for requests without recognised credentials: a token bucket,
  BUDGET_RATE_PER_SECOND with room for a burst of BUDGET_BURST. It stands before
  any signature check and any Sugra API call. A request over it gets 429 with
  Retry-After.
- Two admission lanes for POSTs: RECOGNISED_SEATS seats for credentials this
  process has recognised, OTHER_SEATS for everything else (a request without
  credentials, a JWT it has not checked, a key the Sugra API has not confirmed),
  and at most UNVERIFIED_KEY_SEATS of those for keys the API has not confirmed.
  A request with no seat to take gets 503 server_busy with Retry-After.
- The failed-check shield: when SHIELD_FAILURES refusals of unconfirmed keys from
  one public address fall inside any SHIELD_WINDOW_SECONDS, a new unconfirmed key
  from that address gets 429 with Retry-After for SHIELD_BAR_SECONDS, with no
  Sugra API call. The window slides: the count is the refusals of the last
  SHIELD_WINDOW_SECONDS, never a window anchored at the first one. Recognised
  keys, JWTs and requests without credentials are never held by it, so the
  shared addresses of the AI platforms are not.
- The key-check limit: an unconfirmed key's call is a check of that key, and at
  most KEY_CHECKS_PER_SECOND of them run in a second. Over it the call returns
  server_busy (scope key_checks).
- Per-key counters, only for the numbers the operator sets
  (SUGRA_MCP_KEY_LIMIT_PER_MINUTE, SUGRA_MCP_KEY_LIMIT_PER_DAY). Unset, there is
  no per-key limiting. Over a limit the call returns rate_limited with
  retry_after.

A call is counted against its key BEFORE it runs, in one synchronous step: the
limits are read and the call is charged with no await between the two, so calls
that arrive together cannot all see room that only one of them may take. That
holds for a key this process has recognised and for one it has not. A charge is
given back after the call only for a key this process had not recognised, and
only when the call shows the Sugra API did not take the key (no API request was
sent, or the API answered 401 or 403 for it), so a made-up key leaves nothing
behind. A request that went out and was not refused keeps its charge, however
the call ended. The charge of a recognised
key stays whatever the outcome. A key with no room for its rows in the table is
refused before its call: a call is never let through uncounted.

A credential is recognised only by what this process saw itself: a JWT its auth
layer verified (until the JWT expires, at most RECOGNISED_JWT_MAX_SECONDS) and a
key whose call the Sugra API accepted (RECOGNISED_KEY_SECONDS). The memory holds
SHA-256 digests only, at most RECOGNISED_MAX_ENTRIES of them, and a credential
the API or the auth layer refuses later is struck from it and stays out until its
earlier term would have ended. Nothing is cached about a key that was refused,
and no summary of refusals is kept.

Counting is exact within one process. The counters live only in its memory: they
start at zero when the process starts and are lost when it stops. With more than
one replica each replica enforces its own limits, so a number set here is a limit
for each replica, not a total across them.

Each protection has its own table with its own room (SHIELD_ENTRIES,
KEY_CHECK_ENTRIES, KEY_ENTRIES), so one cannot crowd out another, and a name that
finds its table full of live windows is refused through the refusal path of its
own limit (key_checks, failed_checks, key_minute or key_day) until a window ends.
A name already in a table keeps being counted.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import math
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import config, gate
from .errors import rate_limited_error, server_busy_error

logger = logging.getLogger("sugra_mcp.limits")

# The request scope state key under which AdmissionMiddleware stores the
# request's Admission, read at dispatch the way gate.current_record reads the
# gate record.
ADMISSION_STATE = "sugra_admission"

# The settings, besides config.LIMITS_ENV, read when the limits are on.
KEY_LIMIT_PER_MINUTE_ENV = "SUGRA_MCP_KEY_LIMIT_PER_MINUTE"
KEY_LIMIT_PER_DAY_ENV = "SUGRA_MCP_KEY_LIMIT_PER_DAY"

BUDGET_RATE_PER_SECOND = 10.0
BUDGET_BURST = 20.0

RECOGNISED_SEATS = 24
OTHER_SEATS = 8
UNVERIFIED_KEY_SEATS = 4

RECOGNISED_MAX_ENTRIES = 10_000
RECOGNISED_KEY_SECONDS = 300.0
RECOGNISED_JWT_MAX_SECONDS = 600.0

SHIELD_FAILURES = 30
SHIELD_WINDOW_SECONDS = 60
SHIELD_BAR_SECONDS = 600

KEY_CHECKS_PER_SECOND = 20

# Rows each table keeps, one table to a protection. A name that finds its table
# full of live windows is refused, never counted as a first hit. The key-check
# limit has one name. The shield gets a row for an address only after a refused
# key, and the key-check limit lets at most KEY_CHECKS_PER_SECOND of those
# through a second: at most 20 * 60 rows live a SHIELD_WINDOW_SECONDS and at most
# 20 * 600 / 30 barred ones live a SHIELD_BAR_SECONDS, well under the room. The
# per-key table holds two rows (minute and day) for each of twice as many keys as
# the recognised memory.
KEY_CHECK_ENTRIES = 1
SHIELD_ENTRIES = 4_096
KEY_ENTRIES = 2 * 2 * RECOGNISED_MAX_ENTRIES

MINUTE_SECONDS = 60
DAY_SECONDS = 86_400

_MCP_PATH = "/mcp"

KIND_NONE = "none"
KIND_API_KEY = "api_key"
KIND_TOKEN = "token"


# ---- Settings ----


@dataclass(frozen=True)
class Settings:
    # None means no per-key limit of that kind.
    key_limit_per_minute: int | None = None
    key_limit_per_day: int | None = None


def _bounded_int(env: Mapping[str, str], name: str, low: int, high: int | None = None) -> int | None:
    raw = env.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None
    if value < low or (high is not None and value > high):
        bound = f"between {low} and {high}" if high is not None else f"at least {low}"
        raise ValueError(f"{name} must be {bound}, got {value}")
    return value


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """The limit settings, read when SUGRA_MCP_LIMITS is on.

    An unset per-key limit means no limiting of that kind. A value that is not a
    whole number of at least 1 stops the start, where an operator sees it.
    """
    env = os.environ if environ is None else environ
    return Settings(
        key_limit_per_minute=_bounded_int(env, KEY_LIMIT_PER_MINUTE_ENV, 1),
        key_limit_per_day=_bounded_int(env, KEY_LIMIT_PER_DAY_ENV, 1),
    )


def digest(value: str) -> str:
    """The SHA-256 of a credential or an address, the only form the memory holds."""
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


# ---- In-memory pieces ----


class TokenBucket:
    """A bucket refilled at rate tokens a second up to burst; take returns the wait for a token."""

    def __init__(self, rate: float, burst: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._rate = rate
        self._burst = burst
        self._clock = clock
        self._tokens = burst
        self._at = clock()

    def take(self) -> float:
        """0.0 when a token was taken, else the seconds until one is there."""
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._at) * self._rate)
        self._at = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        return (1.0 - self._tokens) / self._rate


class _BoundedTable:
    """Rows by name with a fixed room, which never evicts a live row to make space.

    A row is live until its end (_end). A table without room for what a caller
    needs first sweeps the ended rows, at most once a second so that a table full
    of live rows costs one pass a second and no more, and a name still without a
    place is for the caller to refuse: has_room says so, and retry_seconds says
    when a row ends.
    """

    def __init__(self, room: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._room = room
        self._clock = clock
        self._rows: dict[str, Any] = {}
        self._swept_at = float("-inf")
        self._soonest = 0.0

    def __len__(self) -> int:
        return len(self._rows)

    @staticmethod
    def _end(row: Any) -> float:
        raise NotImplementedError

    def has_room(self, now: float, need: int = 1) -> bool:
        """True when need more rows fit."""
        if len(self._rows) + need <= self._room:
            return True
        if now - self._swept_at >= 1.0:
            self._swept_at = now
            live: dict[str, Any] = {}
            soonest = float("inf")
            for name, row in self._rows.items():
                end = self._end(row)
                if end > now:
                    live[name] = row
                    soonest = min(soonest, end)
            self._rows = live
            self._soonest = soonest if live else now
        return len(self._rows) + need <= self._room

    def retry_seconds(self) -> int:
        """Whole seconds, at least 1, until a full table frees a row."""
        return _seconds(self._soonest - self._clock())


# One window a charge wants: the counter's name, its length in seconds and the
# most charges it may hold.
Window = tuple[str, float, int]


@dataclass(frozen=True)
class Over:
    """A charge that was refused: the window it stopped at and the seconds to wait.

    The window is the one at its limit, or, for a table without room, the first
    that needs a row.
    """

    index: int
    retry_seconds: int


class Counters(_BoundedTable):
    """Fixed-window counters by name, with charge-before-use.

    The rows are (end of the window, charges). Everything here is plain
    synchronous code: one event loop serves it and nothing awaits inside a
    method, so what reserve reads and what it charges are one step.
    """

    @staticmethod
    def _end(row: tuple[float, int]) -> float:
        return row[0]

    def reserve(self, wanted: Sequence[Window]) -> tuple[float, ...] | Over:
        """Charge one in every window of wanted, or charge none and say where it stopped.

        Returns the end of each window as charged, to give back with release.
        Refused as Over when a window is at its limit, and when the names that
        have no live row cannot all get one: the call is never counted partly
        and never let through uncounted. A window starts at its first charge and
        a later charge never moves it.
        """
        now = self._clock()
        fresh: list[int] = []
        for index, (name, _, limit) in enumerate(wanted):
            row = self._rows.get(name)
            if row is not None and row[0] > now:
                if row[1] >= limit:
                    return Over(index, _seconds(row[0] - now))
            else:
                fresh.append(index)
        if fresh and not self.has_room(now, len(fresh)):
            return Over(fresh[0], self.retry_seconds())
        ends: list[float] = []
        for name, seconds, _ in wanted:
            row = self._rows.get(name)
            if row is None or row[0] <= now:
                row = (now + seconds, 0)
            self._rows[name] = (row[0], row[1] + 1)
            ends.append(row[0])
        return tuple(ends)

    def release(self, name: str, end: float) -> None:
        """Give back one charge of the window of name that ended at end, when it is still that window.

        A charge of an earlier window frees nothing in a later one. A row left
        with no charge is dropped, so it holds no room.
        """
        row = self._rows.get(name)
        if row is None or row[0] != end or row[1] <= 0:
            return
        if row[1] == 1:
            del self._rows[name]
        else:
            self._rows[name] = (end, row[1] - 1)

    def charges(self, name: str) -> int:
        """The charges in name's live window, 0 when it has none."""
        row = self._rows.get(name)
        return row[1] if row is not None and row[0] > self._clock() else 0


class ShieldTable(_BoundedTable):
    """The failed-check shield: one row for each address with a live failure or bar.

    A row holds the times of the address's latest SHIELD_FAILURES refusals and
    the time its bar ends. A refusal counts for SHIELD_WINDOW_SECONDS from its
    own time, so the window slides. The bar starts at the refusal that makes
    SHIELD_FAILURES of them inside the window.
    """

    @staticmethod
    def _end(row: tuple[list[float], float]) -> float:
        times, barred_until = row
        return max(barred_until, times[-1] + SHIELD_WINDOW_SECONDS if times else 0.0)

    def hold(self, address: str) -> float:
        """Seconds a new unconfirmed key from address is held for, 0.0 when it is not held.

        An address with no row is held while the table has no room for one,
        because its next refusal could not be counted.
        """
        now = self._clock()
        row = self._rows.get(address)
        if row is None:
            return 0.0 if self.has_room(now) else float(self.retry_seconds())
        return max(0.0, row[1] - now)

    def record(self, address: str) -> None:
        """Count a refusal against address; the bar starts when the window holds SHIELD_FAILURES of them.

        With no room for a new address nothing is counted here, and hold has
        already refused that address.
        """
        now = self._clock()
        row = self._rows.get(address)
        if row is None and not self.has_room(now):
            return
        times, barred_until = row if row is not None else ([], 0.0)
        edge = now - SHIELD_WINDOW_SECONDS
        times = [at for at in times if at > edge]
        times.append(now)
        del times[:-SHIELD_FAILURES]
        if len(times) >= SHIELD_FAILURES and barred_until <= now:
            barred_until = now + SHIELD_BAR_SECONDS
        self._rows[address] = (times, barred_until)


class RecognisedMemory:
    """Digests of the credentials this process has recognised, and of those struck from it.

    A struck entry keeps the end of its earlier term as the time it is barred
    until, so a credential the API refused does not come back into the
    recognised lane before that term would have ended.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._rows: dict[str, tuple[float, bool]] = {}
        self._swept_at = float("-inf")

    def __len__(self) -> int:
        return len(self._rows)

    def is_recognised(self, credential: str | None) -> bool:
        if credential is None:
            return False
        row = self._rows.get(credential)
        if row is None:
            return False
        if row[0] <= self._clock():
            del self._rows[credential]
            return False
        return not row[1]

    def remember(self, credential: str, seconds: float) -> bool:
        """Recognise credential for seconds; False when it is barred or the memory is full."""
        now = self._clock()
        row = self._rows.get(credential)
        if row is not None and row[0] > now:
            if row[1]:
                return False
        elif len(self._rows) >= RECOGNISED_MAX_ENTRIES:
            self._sweep(now)
            if len(self._rows) >= RECOGNISED_MAX_ENTRIES:
                return False
        if seconds <= 0:
            return False
        self._rows[credential] = (now + seconds, False)
        return True

    def strike(self, credential: str) -> None:
        row = self._rows.get(credential)
        if row is not None and not row[1] and row[0] > self._clock():
            self._rows[credential] = (row[0], True)

    def _sweep(self, now: float) -> None:
        # At most once a second, so a full memory costs one pass a second and no more.
        if now - self._swept_at < 1.0:
            return
        self._swept_at = now
        self._rows = {key: value for key, value in self._rows.items() if value[0] > now}


class Seats:
    """The in-flight POSTs of each lane. One event loop serves them, so plain counters do."""

    def __init__(self) -> None:
        self.recognised = 0
        self.other = 0
        self.unverified_keys = 0

    def take_recognised(self) -> bool:
        if self.recognised >= RECOGNISED_SEATS:
            return False
        self.recognised += 1
        return True

    def take_other(self, unverified_key: bool) -> bool:
        if self.other >= OTHER_SEATS:
            return False
        if unverified_key:
            if self.unverified_keys >= UNVERIFIED_KEY_SEATS:
                return False
            self.unverified_keys += 1
        self.other += 1
        return True


# ---- The limits ----


class Held:
    """The charges one tool call holds on its key: (name, window end) for each window set.

    The handle of ONE call: before_call makes it, the call carries it, and
    after_call settles that handle and no other, at most once.

    unless_accepted is True for a key this process had not recognised when the
    call was charged: its charges are given back only when no API request was
    sent or the API answered 401 or 403 for the key.
    """

    __slots__ = ("settled", "unless_accepted", "windows")

    def __init__(self, windows: tuple[tuple[str, float], ...], unless_accepted: bool) -> None:
        self.windows = windows
        self.unless_accepted = unless_accepted
        self.settled = False


class CallCharge(NamedTuple):
    """What before_call decided for one call.

    refusal is the payload the call is refused with, or None to run it. held is
    the call's handle when it was charged to a key, else None (refused, or
    nothing to charge). The call passes held to after_call.
    """

    refusal: dict[str, Any] | None
    held: Held | None


class Limits:
    """What one process knows and does to hold its limits."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self._wall = wall
        self.budget = TokenBucket(BUDGET_RATE_PER_SECOND, BUDGET_BURST, clock)
        self.seats = Seats()
        self.recognised = RecognisedMemory(clock)
        self.shield = ShieldTable(SHIELD_ENTRIES, clock)
        self.checks = Counters(KEY_CHECK_ENTRIES, clock)
        self.keys = Counters(KEY_ENTRIES, clock)

    # -- admission --

    def admit(self, scope: Scope) -> Admission | Refusal:
        """Take a request to /mcp in, or say how it is refused. Holds the seat it returns."""
        authorization = None
        real_ip = None
        for name, value in scope.get("headers") or ():
            lowered = name.lower()
            if lowered == b"authorization" and authorization is None:
                authorization = value.decode("latin-1")
            elif lowered == b"x-real-ip" and real_ip is None:
                real_ip = value.decode("latin-1")
        kind, credential = _credential_of(authorization)
        post = scope.get("method") == "POST"
        recognised = self.recognised.is_recognised(credential)
        admission = Admission(self, kind, credential, recognised)

        if recognised:
            if post and not self.seats.take_recognised():
                return Refusal(503, server_busy_error("lane_recognised", RECOGNISED_SEATS), 1)
            admission.seat = "recognised" if post else None
            return admission

        wait = self.budget.take()
        if wait > 0.0:
            seconds = _seconds(wait)
            return Refusal(429, rate_limited_error("budget", retry_after=seconds), seconds)
        unverified_key = kind == KIND_API_KEY
        if post:
            if not self.seats.take_other(unverified_key):
                return Refusal(503, server_busy_error("lane_other", OTHER_SEATS), 1)
            admission.seat = "other"
            admission.unverified_key_seat = unverified_key
        if unverified_key:
            try:
                address = _public_address(scope, real_ip)
                held = 0
                if address is not None:
                    admission.address = digest(address)
                    held = self.address_held(admission.address)
            except BaseException:
                # A failure while the shield looks must not keep the seat.
                admission.release()
                raise
            if held > 0:
                admission.release()
                return Refusal(429, rate_limited_error("failed_checks", retry_after=held), held)
        return admission

    # -- the failed-check shield --

    def address_held(self, address: str) -> int:
        """Seconds a new unconfirmed key from address is held for, 0 when it is not held."""
        held = self.shield.hold(address)
        return _seconds(held) if held > 0 else 0

    # -- the key-check limit --

    def key_check_over(self) -> int | None:
        """Charge one check of an unconfirmed key; the limit it ran into, or None when it may go."""
        reserved = self.checks.reserve([("checks", 1.0, KEY_CHECKS_PER_SECOND)])
        return KEY_CHECKS_PER_SECOND if isinstance(reserved, Over) else None

    # -- the per-key counters --

    @property
    def has_key_limits(self) -> bool:
        return self.settings.key_limit_per_minute is not None or self.settings.key_limit_per_day is not None

    def _key_windows(self, digest_of_key: str) -> list[tuple[str, str, float, int]]:
        """The (scope, name, seconds, limit) of each per-key limit the operator set."""
        windows: list[tuple[str, str, float, int]] = []
        if self.settings.key_limit_per_minute is not None:
            windows.append(("key_minute", f"m:{digest_of_key}", MINUTE_SECONDS, self.settings.key_limit_per_minute))
        if self.settings.key_limit_per_day is not None:
            windows.append(("key_day", f"d:{digest_of_key}", DAY_SECONDS, self.settings.key_limit_per_day))
        return windows

    def reserve_key(self, digest_of_key: str) -> tuple[tuple[str, float], ...] | dict[str, Any] | None:
        """Charge one call of the key with this digest, in one step, before the call runs.

        Returns None when no per-key limit is set, the rate_limited payload when
        a set limit is reached or the table has no room for the key's rows (the
        call is then not counted and is refused), else the charges to give back
        with release_key. Whether it is a recognised key makes no difference
        here: every key is checked and charged the same way.
        """
        windows = self._key_windows(digest_of_key)
        if not windows:
            return None
        reserved = self.keys.reserve([(name, seconds, limit) for _, name, seconds, limit in windows])
        if isinstance(reserved, Over):
            scope_name, _, _, limit = windows[reserved.index]
            return rate_limited_error(scope_name, limit=limit, retry_after=reserved.retry_seconds)
        return tuple((name, end) for (_, name, _, _), end in zip(windows, reserved, strict=True))

    def release_key(self, windows: tuple[tuple[str, float], ...]) -> None:
        """Give back one charge in each window, as reserve_key made it."""
        for name, end in windows:
            self.keys.release(name, end)

    # -- what a response tells --

    def observe_response(self, admission: Admission, scope: Scope, status: object) -> None:
        """Recognise a JWT the auth layer verified, strike one it refused. Never raises."""
        if admission.kind != KIND_TOKEN or admission.credential is None:
            return
        from .server import request_principal_method

        if request_principal_method(scope) == "oauth":
            if not admission.recognised:
                token = _token_of(scope)
                seconds = _jwt_seconds(token, self._wall()) if token is not None else 0.0
                self.recognised.remember(admission.credential, seconds)
        elif status in (401, 403):
            # Whatever the request saw when it began: another request may have
            # recognised the token since, and a strike changes nothing for a
            # token with no live entry.
            self.recognised.strike(admission.credential)


@dataclass
class Refusal:
    """A request refused at admission: the HTTP status, the JSON body and the Retry-After seconds."""

    status: int
    body: dict[str, Any]
    retry_after: int


class Admission:
    """One request to /mcp admitted: its credential, its seat, and the per-call hooks of server.py.

    Holds the SHA-256 digest of the credential and of the address, never their text.
    """

    __slots__ = ("_limits", "address", "credential", "kind", "recognised", "seat", "unverified_key_seat")

    def __init__(self, limits: Limits, kind: str, credential: str | None, recognised: bool) -> None:
        self._limits = limits
        self.kind = kind
        self.credential = credential
        self.recognised = recognised
        self.address: str | None = None
        self.seat: str | None = None
        self.unverified_key_seat = False

    def release(self) -> None:
        """Give the seat back. Safe to call twice."""
        seats = self._limits.seats
        if self.seat == "recognised":
            seats.recognised -= 1
        elif self.seat == "other":
            seats.other -= 1
            if self.unverified_key_seat:
                seats.unverified_keys -= 1
        self.seat = None
        self.unverified_key_seat = False

    def before_call(self, api_key: str | None) -> CallCharge:
        """What to do with a tool call before it runs: its refusal payload or None to run it, and its handle.

        The handle (CallCharge.held) belongs to this one call; the request keeps
        no list of charges, so calls of one request cannot settle each other's.
        Nothing here awaits, so the limits it reads and the charge it makes are
        one step. A key the API has not confirmed takes a key-check first. Then,
        when per-key limits are set, the call is charged to its key (a confirmed
        key, the key an OAuth token resolved to and an unconfirmed key alike) and
        refused when a limit is reached or the table has no room for the key.
        A failure here is the limits', never the call's: an unexpected error
        lets the call run.
        """
        limits = self._limits
        try:
            unconfirmed = self.kind == KIND_API_KEY and not limits.recognised.is_recognised(self.credential)
            if unconfirmed:
                over = limits.key_check_over()
                if over is not None:
                    refusal = server_busy_error("key_checks", over)
                    refusal["retry_after"] = 1
                    return CallCharge(refusal, None)
            if api_key and limits.has_key_limits:
                reserved = limits.reserve_key(digest(api_key))
                if isinstance(reserved, dict):
                    return CallCharge(reserved, None)
                if reserved is not None:
                    return CallCharge(None, Held(reserved, unconfirmed))
        except Exception as exc:
            logger.warning("Limits check failed (%s); the call runs.", type(exc).__name__)
        return CallCharge(None, None)

    def after_call(self, held: Held | None, outcome: str, api_requests: int) -> None:
        """Learn from how a call ended: settle its own charge, strike a refused credential, count a failed check.

        held is the handle before_call returned for THIS call (None when it was
        refused or charged nothing). It is settled here and nowhere else, at
        most once: a call never settles another call's charge, whatever order
        the calls of one request finish in.

        The charge of a call of an unconfirmed key is given back only when the
        call proves the API did not take the key: no API request was sent, or the
        API answered 401 or 403 for it. A request that went out with no refusal
        seen (accepted, cancelled in flight, a transport error, a local failure
        afterwards) keeps its charge, as does the charge of any other call.
        """
        limits = self._limits
        try:
            accepted = outcome == gate.SUCCEEDED and api_requests > 0
            not_taken = api_requests == 0 or outcome == gate.AUTH_REFUSED
            if held is not None and not held.settled:
                held.settled = True
                if held.unless_accepted and not_taken:
                    limits.release_key(held.windows)
            if self.kind == KIND_NONE or self.credential is None:
                return
            if outcome == gate.AUTH_REFUSED:
                was_recognised = limits.recognised.is_recognised(self.credential)
                limits.recognised.strike(self.credential)
                if self.kind == KIND_API_KEY and not was_recognised and self.address is not None:
                    limits.shield.record(self.address)
            elif accepted and self.kind == KIND_API_KEY:
                limits.recognised.remember(self.credential, RECOGNISED_KEY_SECONDS)
        except Exception as exc:
            logger.warning("Limits update failed (%s).", type(exc).__name__)


def current_admission() -> Admission | None:
    """The admission of the request that carried the message being dispatched, or None.

    None when the limits are off, outside HTTP, and for a request that is not
    to /mcp.
    """
    from mcp.server.lowlevel.server import request_ctx

    try:
        scope = request_ctx.get().request.scope
        admission = scope["state"][ADMISSION_STATE]
    except Exception:
        return None
    return admission if isinstance(admission, Admission) else None


# ---- The middleware ----


class AdmissionMiddleware:
    """ASGI middleware that applies the limits to a request to /mcp before authentication.

    Added between the authentication layer and CORS, so it runs before the
    credential is checked and a refusal still carries the CORS headers. A
    request to any other path, and an OPTIONS request, passes through
    untouched.
    """

    def __init__(self, app: ASGIApp, *, limits: Limits) -> None:
        self.app = app
        self.limits = limits

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") == "OPTIONS"
            or scope.get("path", "").rstrip("/") != _MCP_PATH
        ):
            await self.app(scope, receive, send)
            return
        try:
            admitted = self.limits.admit(scope)
        except Exception as exc:
            # The limits are never the reason a request fails.
            logger.warning("Admission failed (%s); the request runs.", type(exc).__name__)
            await self.app(scope, receive, send)
            return
        if isinstance(admitted, Refusal):
            response = JSONResponse(
                admitted.body, status_code=admitted.status, headers={"Retry-After": str(admitted.retry_after)}
            )
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})[ADMISSION_STATE] = admitted
        observed = False

        async def admitted_send(message: Message) -> None:
            nonlocal observed
            if message["type"] == "http.response.start" and not observed:
                observed = True
                try:
                    self.limits.observe_response(admitted, scope, message.get("status"))
                except Exception as exc:
                    logger.warning("Admission update failed (%s).", type(exc).__name__)
            await send(message)

        try:
            await self.app(scope, receive, admitted_send)
        finally:
            admitted.release()


# ---- Helpers ----


def _seconds(value: float) -> int:
    """Whole seconds to wait, at least 1."""
    return max(1, math.ceil(value))


def _credential_of(authorization: str | None) -> tuple[str, str | None]:
    """The kind of credential an Authorization header carries and its digest.

    Read as the authentication layer reads it: a bearer token, `sugra_` for a
    key and anything else a token.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return KIND_NONE, None
    presented = authorization[7:].strip()
    if not presented:
        return KIND_NONE, None
    return (KIND_API_KEY if presented.startswith("sugra_") else KIND_TOKEN), digest(presented)


def _token_of(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or ():
        if name.lower() == b"authorization":
            text = value.decode("latin-1")
            if text.lower().startswith("bearer "):
                return text[7:].strip()
            return None
    return None


def _public_address(scope: Scope, real_ip: str | None) -> str | None:
    """The caller's address when it is a public one, else None.

    The shield attributes failures to an address, so it holds only one that
    names a caller: with the proxy settings trusted, the X-Real-IP the proxy
    wrote; otherwise the connection peer. A private, loopback or other
    non-public address is a proxy or a platform front, shared by many callers,
    and is never held.
    """
    peer = scope.get("client")
    peer_address = peer[0] if type(peer) in (tuple, list) and peer else None
    address, _ = config.caller_address(peer_address, real_ip)
    if type(address) is not str:
        return None
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return None
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        parsed = mapped
    return str(parsed) if parsed.is_global else None


def _jwt_seconds(token: str, now: float) -> float:
    """How long a JWT the auth layer verified is recognised: until it expires, at most 10 minutes.

    The expiry is read from the payload of a token whose signature and expiry
    that layer has just checked, and only ever shortens the term.
    """
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expiry = claims.get("exp") if isinstance(claims, dict) else None
    except Exception:
        return RECOGNISED_JWT_MAX_SECONDS
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        return RECOGNISED_JWT_MAX_SECONDS
    return max(0.0, min(RECOGNISED_JWT_MAX_SECONDS, float(expiry) - now))


def build_limits() -> Limits | None:
    """The limits when SUGRA_MCP_LIMITS is on, else None and nothing else happens.

    Off reads no other setting. On, the settings are read and checked.
    """
    if not config.limits_enabled():
        return None
    return Limits(load_settings())
