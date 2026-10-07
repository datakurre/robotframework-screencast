"""Screencast: a Robot Framework keyword library over sync Playwright.

Session state (Playwright, browser, contexts, pages, the in-progress
Timeline) lives in **module-level globals** (`_SESSION`), not on `self`.
Robot Framework constructs a fresh library instance for every
`TestSuite(...).run()` call, but the module stays imported in one Python
process -- so module-level state is what lets the agent driver (see
screencast.driver) run one keyword after another against a live browser,
REPL-style, across repeated `run()` calls. This is verified by
tests/test_library.py, which calls `.run()` twice against a fake Playwright
and asserts the second run reuses the first run's browser.

The `Browser` Robot Framework library (Node + `rfbrowser init`) is
deliberately not used here: it needs a browser download the `browser` agent
skill's rules forbid re-fetching, and it does not expose the raw
per-context video handling recording depends on.
"""

from datetime import datetime
from pathlib import Path
from robot.api import FatalError
from robot.api import logger
from robot.utils import timestr_to_secs
from screencast.cursor import CLICK_SETTLE_MS
from screencast.cursor import CURSOR_SCRIPT
from screencast.cursor import FILL_SETTLE_MS
from screencast.cursor import MOVE_SETTLE_MS
from screencast.cursor import MOVE_STEPS
from screencast.cursor import TYPE_DELAY_MS
from screencast.timeline import Timeline
import base64
import json
import os
import time


DEFAULT_VIEWPORT = {"width": 1920, "height": 1080}
DEFAULT_OBSERVE_WAIT = 1.5
DEFAULT_RETURN_TO_OBSERVER_WAIT = 6.0
DEFAULT_CAPTION_DURATION = 4.0
TRACK_CORNERS = ("bottom-left", "bottom-right", "top-left", "top-right")
HOLD_VIEWS = ("observer", "actor")
# Injected as an init script by Hide Cursor, so the cursor stays hidden on
# every later document in the context, not just the current one (the cursor
# itself is an init script too, and comes back on every navigation).
HIDE_CURSOR_CSS = (
    "#screencast-recording-cursor,.screencast-recording-click{display:none !important}"
)
HIDE_CURSOR_SCRIPT = """
(() => {
  const install = () => {
    const style = document.createElement('style');
    style.textContent = __CSS__;
    document.documentElement.appendChild(style);
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', install, {once: true});
  } else {
    install();
  }
})();
""".replace("__CSS__", json.dumps(HIDE_CURSOR_CSS))

# RF BuiltIn keywords whose own outcome means a failure nested inside them
# was expected/recovered, not real -- see _Listener._pop_recovery_boundary.
# Wait Until Keyword Succeeds succeeds once a retry does; the three Run
# Keyword And ... variants convert a failure into a status they report
# through their own (always-PASS) result instead of failing themselves.
# Run Keyword And Continue On Failure is included too, though its own
# status already mirrors the wrapped keyword's (so in practice this is a
# no-op for it): a failure it lets the test continue past is still a real
# failure, not a recovered one, and that's exactly what its FAIL status
# already signals without any special-casing here.
# Keywords during which the story is just waiting: nothing is driven on
# screen, and the recording shows a page that does not change. Matched by the
# keyword's own name, whichever library owns it; a project's own polling
# keywords opt in with the tag below (`[Tags]    screencast:wait`).
_WAITING_KEYWORDS = frozenset(
    {
        "Sleep",
        "Wait Until Keyword Succeeds",
        "Wait Until Visible",
        "Wait For Navigation Away",
    }
)
WAIT_TAG = "screencast:wait"

# Data shared between tasks and runs, kept in the take directory so a later
# `run --take <same dir> --task ...`, another suite, or a fresh process can
# read it back (see Save State / Load State).
STATE_FILE = "state.json"
_MISSING = object()
# A wait shorter than this is not dead air, only a page settling; recording
# every one would just fill the timeline with noise.
WAIT_MIN_RECORDED = 0.25


def _is_waiting(result):
    return result.name in _WAITING_KEYWORDS or WAIT_TAG in result.tags


_RECOVERING_WRAPPER_KEYWORDS = frozenset(
    {
        "Wait Until Keyword Succeeds",
        "Run Keyword And Ignore Error",
        "Run Keyword And Return Status",
        "Run Keyword And Expect Error",
        "Run Keyword And Continue On Failure",
    }
)


class _Session:
    """Module-level Playwright/timeline state. See the module docstring."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.playwright = None
        self.browser = None
        # Browsers attached over CDP, by name ("" for an unnamed endpoint),
        # in the order given; `browser` is the first of them. Empty when the
        # engine launched its own.
        self.browsers = {}
        self.cdp = None
        self.headless = True
        self.record = True
        self.take_dir = None
        self.viewport = dict(DEFAULT_VIEWPORT)
        self.started = None
        self.observer_name = None
        self.observer_context = None
        self.observer_page = None
        self.tracks = {}
        self.current_page = None
        self.current_actor = None
        self._turn_context = None
        self._scratch_context = None
        self._page_before_scratch = None
        self._turn_started_at = None
        self._turn_start_recorded = False
        self._pending_turn_meta = None
        self._turn_page = None
        self.timeline = None
        self.console_logs = {}
        self.open_pages = []

    def elapsed(self):
        if self.started is None:
            if not self.record:
                # Nothing is recorded, so there is no clock to keep: a
                # partial `--no-record` run may play an actor turn without
                # the observer task that would have started it.
                return 0.0
            raise FatalError("No observer started yet -- call Start Observer first")
        return time.monotonic() - self.started


_SESSION = _Session()


class _Listener:
    """Writes the in-progress Timeline to disk, and captures failure
    artifacts. Registered automatically via ROBOT_LIBRARY_LISTENER -- story
    authors never import or configure it directly."""

    ROBOT_LISTENER_API_VERSION = 3

    def __init__(self):
        self._pending_dump_paths = None
        self._recovery_boundary_stack = []
        self._wait_depth = 0
        self._wait_started = None

    def start_test(self, data, result):
        self._pending_dump_paths = None
        self._recovery_boundary_stack = []
        self._wait_depth = 0
        self._wait_started = None

    def _start_wait(self, result):
        # Only the outermost waiting keyword counts: a Wait Until Keyword
        # Succeeds polling a Wait Until Visible is one wait, not two.
        if self._wait_depth == 0 and _SESSION.timeline is not None:
            self._wait_started = (_SESSION.elapsed(), result.name)
        self._wait_depth += 1

    def _end_wait(self):
        self._wait_depth = max(0, self._wait_depth - 1)
        if self._wait_depth or self._wait_started is None:
            return
        started, name = self._wait_started
        self._wait_started = None
        duration = _SESSION.elapsed() - started
        if _SESSION.timeline is not None and duration >= WAIT_MIN_RECORDED:
            _SESSION.timeline.add_event(
                {
                    "type": "wait",
                    "time": started,
                    "duration": round(duration, 3),
                    "keyword": name,
                }
            )
            _save_timeline()

    def _push_recovery_boundary(self):
        # Remember whether a dump was already pending *before* this
        # recovery boundary (a Wait Until Keyword Succeeds/Run Keyword
        # And .../TRY block) started -- popping it below only gets to
        # clear a dump captured strictly during the boundary's own body,
        # never one that was already sitting there from something earlier
        # and unrelated.
        self._recovery_boundary_stack.append(self._pending_dump_paths is not None)

    def _pop_recovery_boundary(self, succeeded):
        # That pending dump must not block a later, *different* failure in
        # the same test from getting its own: the common shape here is
        # Open Task -> Wait For Task polls and fails a few times, then
        # succeeds, and only then does e.g. Human Click actually fail for
        # real. Depth alone can't tell "the boundary finally closing" apart
        # from "some later, unrelated keyword happening to close at the
        # same nesting depth" (e.g. the [Teardown] right after) -- so watch
        # for known recovery boundaries directly instead: when one
        # succeeds having had no dump pending at its own start, whatever
        # got dumped during its body was recovered/expected, so clear it.
        had_pending_before = self._recovery_boundary_stack.pop()
        if succeeded and had_pending_before is False and self._pending_dump_paths:
            for path in self._pending_dump_paths:
                path.unlink(missing_ok=True)
            self._pending_dump_paths = None

    def start_keyword(self, data, result):
        if _is_waiting(result):
            self._start_wait(result)
        if data.name in _RECOVERING_WRAPPER_KEYWORDS:
            self._push_recovery_boundary()

    def end_keyword(self, data, result):
        # Dump immediately, on the *first* failure in this test, while the
        # failing keyword's own page is still open -- a task's teardown
        # (e.g. End Actor Turn) can close it before end_test below fires,
        # which would leave nothing left to screenshot. A keyword retried
        # inside Wait Until Keyword Succeeds reports FAIL on every failed
        # attempt even when a later attempt succeeds and the task passes
        # overall, so only the first attempt's dump is kept as "pending",
        # cleared on a recovery boundary's own success (see
        # _pop_recovery_boundary) rather than unconditionally here.
        if _is_waiting(result):
            self._end_wait()
        if data.name in _RECOVERING_WRAPPER_KEYWORDS:
            self._pop_recovery_boundary(succeeded=result.status != "FAIL")
        if result.status == "FAIL" and self._pending_dump_paths is None:
            self._pending_dump_paths = _dump_failure_artifacts(result.name)

    def start_try(self, data, result):
        # A TRY/EXCEPT structure isn't a keyword call at all, so it can't
        # be matched by name -- RF calls this once per structure, same
        # shape as a recovering wrapper keyword: an EXCEPT branch catching
        # the TRY body's failure makes the whole structure's own status
        # PASS, exactly like Run Keyword And Ignore Error converting a
        # failure into a status it reports instead of failing itself.
        self._push_recovery_boundary()

    def end_try(self, data, result):
        self._pop_recovery_boundary(succeeded=result.status != "FAIL")

    def end_test(self, data, result):
        if result.status != "FAIL" and self._pending_dump_paths:
            for path in self._pending_dump_paths:
                path.unlink(missing_ok=True)
        self._pending_dump_paths = None

    def close(self):
        _save_timeline()


def _save_timeline():
    """Write the in-progress timeline to disk now, rather than only once at
    the very end of the run (_Listener.close()) -- a crash mid-take (an
    ffmpeg/browser death, a killed process) would otherwise lose every
    event recorded so far along with whatever video did make it to disk.
    Called after every keyword that mutates the timeline, not just once."""
    if _SESSION.timeline is not None and _SESSION.take_dir is not None:
        path = _SESSION.timeline.save(Path(_SESSION.take_dir) / "timeline.json")
        logger.info(f"Wrote timeline: {path}")


def _dump_failure_artifacts(keyword_name):
    if _SESSION.take_dir is None:
        return []
    take_dir = Path(_SESSION.take_dir)
    take_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S-%f")
    written = []
    for index, page in enumerate(list(_SESSION.open_pages)):
        if page.is_closed():
            continue
        prefix = take_dir / f"failure-{stamp}-{index}"
        try:
            page.screenshot(path=str(prefix.with_suffix(".png")), full_page=True)
            written.append(prefix.with_suffix(".png"))
        except Exception as error:  # noqa: BLE001 -- best-effort diagnostics
            logger.warn(f"Could not screenshot {page.url}: {error}")
        try:
            snapshot = page.locator("body").aria_snapshot()
        except Exception as error:  # noqa: BLE001
            snapshot = f"<aria_snapshot failed: {error}>"
        console = "\n".join(_SESSION.console_logs.get(page, []))
        prefix.with_suffix(".txt").write_text(
            f"keyword: {keyword_name}\n"
            f"url: {page.url}\n\n"
            f"console:\n{console}\n\n"
            f"aria snapshot:\n{snapshot}\n"
        )
        written.append(prefix.with_suffix(".txt"))
        logger.info(f"Failure artifacts: {prefix}.png, {prefix}.txt")
    return written


def _track_console(page):
    _SESSION.console_logs[page] = []
    log = _SESSION.console_logs[page]
    page.on("console", lambda msg: log.append(f"[{msg.type}] {msg.text}"))
    page.on("pageerror", lambda exc: log.append(f"pageerror: {exc}"))
    page.on(
        "requestfailed",
        lambda req: log.append(f"requestfailed: {req.url} {req.failure}"),
    )


def parse_cdp(spec):
    """Parse a CDP attach spec into {name: endpoint URL}, in order.

    Accepts a comma-separated list of `[NAME=]ENDPOINT`, where ENDPOINT is a
    bare port (on 127.0.0.1) or an `http://`/`ws://` URL -- the same
    `alice=9222,bob=9223` shape agent-sandbox's
    `$AGENT_SANDBOX_BROWSER_CDP_PORT` carries, so it can be passed through
    unchanged. An entry without a name is stored under ""."""
    endpoints = {}
    for entry in str(spec).split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, endpoint = entry.rpartition("=")
        if not sep:
            name, endpoint = "", entry
        name, endpoint = name.strip(), endpoint.strip()
        if endpoint.isdigit():
            endpoint = f"http://127.0.0.1:{endpoint}"
        elif "://" not in endpoint:
            raise ValueError(
                f"Not a CDP endpoint: {entry!r} -- expected a port or an "
                "http:// or ws:// URL, optionally prefixed with NAME="
            )
        if name in endpoints:
            raise ValueError(f"CDP browser {name!r} is given twice in {spec!r}")
        endpoints[name] = endpoint
    if not endpoints:
        raise ValueError(f"No CDP endpoint in {spec!r}")
    return endpoints


def configure_browser(headless=None, cdp=None):
    """Set how `start_browser` gets its browser, for callers (the driver)
    that run a story whose own `Library` import does not pass these. Only
    arguments that were actually passed change the session, same rule as
    `Screencast.__init__`."""
    if headless is not None:
        _SESSION.headless = _as_bool(headless)
    if cdp:
        _SESSION.cdp = parse_cdp(cdp)


def _browser_for(name):
    """The browser a context for `name` (an actor, the observer, a track)
    opens in: the attached browser of that name when there is one, so each
    persona can play in its own host window, else the default one."""
    if name:
        wanted = str(name).casefold()
        for browser_name, browser in _SESSION.browsers.items():
            if browser_name.casefold() == wanted:
                return browser
    return _SESSION.browser


def _state_path():
    return Path(_SESSION.take_dir) / STATE_FILE


def _read_state():
    path = _state_path()
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text())
    except ValueError as error:
        raise AssertionError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(state, dict):
        raise AssertionError(
            f"{path} must hold a JSON object, not {type(state).__name__}"
        )
    return state


def _write_state(state):
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write beside it and rename: a crash mid-write must not leave half a file
    # for the next run to choke on.
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(scratch, path)


class Screencast:
    """Robot Framework keyword library for recorded, human-paced browser
    scenarios. See scripts/screencasts/resources/*.resource for how a
    project builds its own keywords on top of these."""

    ROBOT_LIBRARY_SCOPE = "GLOBAL"
    ROBOT_LIBRARY_LISTENER = _Listener()

    def __init__(
        self, take_dir=None, record=None, headless=None, viewport=None, cdp=None
    ):
        # Only arguments that were actually passed change the session. Robot
        # Framework constructs a library instance for *every* import of it,
        # and a story's own `Library screencast.Screencast take_dir=... ` is
        # followed by a resource file's bare `Library screencast.Screencast`
        # (bpmproxy.resource) -- which used to reset the session to the
        # defaults, sending a `--no-record` run's videos, timeline and
        # failure artifacts to the current directory.
        if take_dir is not None:
            _SESSION.take_dir = Path(take_dir)
        elif _SESSION.take_dir is None:
            _SESSION.take_dir = Path(".")
        if record is not None:
            _SESSION.record = _as_bool(record)
        configure_browser(headless=headless, cdp=cdp)
        if viewport:
            _SESSION.viewport = viewport
        if _SESSION.timeline is None:
            _SESSION.take_dir.mkdir(parents=True, exist_ok=True)

    # -- session/browser lifecycle -----------------------------------------

    def start_browser(self):
        """Start Playwright and launch Chromium, unless a previous call (in
        this same process) already did -- the browser instance is reused
        across `TestSuite.run()` calls, which is what makes `probe`/REPL
        debugging possible. Never launches a second browser.

        With a CDP spec (`cdp=`, `--cdp`, or `$SCREENCAST_CDP`), attaches to
        already-running browsers instead -- e.g. a visible one on the host
        of a container that has no display. Every recorded context is still
        a fresh one the engine opens and closes itself, so recording,
        cursor injection and the timeline work the same; only where the
        pixels are drawn changes."""
        if _SESSION.browser is not None:
            return
        if _SESSION.cdp is None and os.environ.get("SCREENCAST_CDP"):
            try:
                _SESSION.cdp = parse_cdp(os.environ["SCREENCAST_CDP"])
            except ValueError as error:
                raise FatalError(f"SCREENCAST_CDP: {error}") from error
        try:
            from playwright.sync_api import sync_playwright

            _SESSION.playwright = sync_playwright().start()
        except Exception as error:
            raise FatalError(f"Could not start Playwright: {error}") from error
        if _SESSION.cdp:
            for name, endpoint in _SESSION.cdp.items():
                try:
                    browser = _SESSION.playwright.chromium.connect_over_cdp(endpoint)
                except Exception as error:
                    label = f"browser {name!r}" if name else "the browser"
                    raise FatalError(
                        f"Could not attach to {label} over CDP at {endpoint}: "
                        f"{error} -- is it running, and is its port reachable "
                        "from here?"
                    ) from error
                _SESSION.browsers[name] = browser
                if _SESSION.browser is None:
                    _SESSION.browser = browser
            return
        try:
            launch_kwargs = {
                "headless": _SESSION.headless,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            # devenv's playwright-driver.browsers (or `playwright install`)
            # already pairs the driver with a matching browser build, so
            # this is normally unset. It exists for environments -- like a
            # plain pip install against a pre-fetched, differently
            # versioned browser cache -- where the two disagree.
            executable_path = os.environ.get("SCREENCAST_CHROMIUM_PATH")
            if executable_path:
                launch_kwargs["executable_path"] = executable_path
            _SESSION.browser = _SESSION.playwright.chromium.launch(**launch_kwargs)
        except Exception as error:
            raise FatalError(f"Could not start the browser: {error}") from error

    def stop_browser(self):
        """Close every open context/page and shut Playwright down. Only the
        top-level driver command calls this at the very end of a process --
        never between `run()` calls, or the session would not survive.

        A browser attached over CDP is only disconnected from: `close()` on
        it drops the contexts this session created and leaves the browser
        itself, and whatever tabs it had, running."""
        for page in list(_SESSION.open_pages):
            if not page.is_closed():
                page.close()
        for browser in list(_SESSION.browsers.values()) or [_SESSION.browser]:
            if browser is not None:
                browser.close()
        if _SESSION.playwright is not None:
            _SESSION.playwright.stop()
        _SESSION.reset()

    # -- observer -------------------------------------------------------------

    def start_observer(self, name, url, storage_state=None):
        """Open the observer recording: the one context that spans the
        whole take, opened first and closed last. Starts the take's clock,
        which every actor clip's `offset` is measured against."""
        self.start_browser()
        if _SESSION.observer_context is not None:
            raise FatalError("Start Observer was already called for this take")
        context_kwargs = {"viewport": _SESSION.viewport}
        if _SESSION.record:
            context_kwargs["record_video_dir"] = str(_SESSION.take_dir)
            context_kwargs["record_video_size"] = _SESSION.viewport
        if storage_state:
            context_kwargs["storage_state"] = storage_state
        try:
            context = _browser_for(name).new_context(**context_kwargs)
            context.add_init_script(CURSOR_SCRIPT)
            page = context.new_page()
            # Recording begins here, at page creation -- not at the first
            # goto below. Starting the clock any later would make every
            # timeline timestamp (turn offsets, turn_start/end, chapter,
            # focus, hold) land earlier than its true position in the
            # observer video by however long that first navigation took.
            _SESSION.started = time.monotonic()
            _track_console(page)
            _SESSION.open_pages.append(page)
            page.goto(url, wait_until="load")
        except Exception as error:
            raise FatalError(
                f"Could not start the observer at {url}: {error}"
            ) from error
        _SESSION.observer_name = name
        _SESSION.observer_context = context
        _SESSION.observer_page = page
        _SESSION.current_page = page
        video_path = page.video.path() if _SESSION.record else None
        _SESSION.timeline = Timeline.new(
            observer_video=Path(video_path).name if video_path else "",
            observer_name=name,
        )
        _save_timeline()

    def end_observer(self):
        """Close the observer context, flushing its video -- Cockpit (or
        whatever the observer is) "closes last": call this as the story's
        very last keyword. Without it the observer's .webm never finishes
        writing and ffprobe sees a near-empty file, since Playwright only
        flushes a context's video on `close()`. Does not stop the browser
        itself -- that stays alive for a following `probe` call.

        A track still open (no `End Track`) is closed and recorded first,
        with a warning -- otherwise its video would never be flushed and it
        would silently be missing from the composed output."""
        context = _SESSION.observer_context
        if context is None:
            raise FatalError("No observer -- call Start Observer first")
        for name in list(_SESSION.tracks):
            logger.warn(f"Track {name!r} was still open at End Observer; closing it")
            self.end_track(name)
        context.close()
        _SESSION.observer_context = None
        _SESSION.observer_page = None
        _SESSION.current_page = None

    # -- extra tracks -----------------------------------------------------
    #
    # A *track* is a second (third, ...) context recorded for the whole
    # take alongside the observer -- e.g. an ambient terminal -- that a
    # story can later `Focus` onto as the main view, not just composite as
    # an always-present corner inset the way `compose()`'s external
    # `tracks=`/`--track` does for a recording the engine itself never
    # drove (see that function's own docstring). A track recorded this way
    # is written into `timeline.json` itself (`Timeline.add_track_clip`),
    # with its `offset` measured against the same clock the observer and
    # every actor turn already share -- `compose()` picks it up
    # automatically, no `--track`/manual offset guessing needed.
    #
    # Two screens (just the observer, pointed at a terminal -- see the
    # skill reference's "Using a ttyd terminal as the observer") don't need
    # this; reach for it only once a take wants a *third* (or more)
    # continuously-recorded, independently focusable screen.

    def start_track(
        self,
        name,
        url,
        storage_state=None,
        focusable=True,
        fade=False,
        scale=None,
        margin=None,
        border=None,
        corner=None,
    ):
        """Open an extra continuously-recorded context/page, from now until
        `End Track`, alongside the observer. Requires `Start Observer` to
        already be running (a track's `offset` is measured on its clock).
        Sets `current_page` to the new track's page, same convenience
        `Start Observer` itself provides, so a same-page setup call (e.g.
        `Hide Cursor`, for a terminal track) can follow immediately without
        a separate keyword to target it -- see `library.py`'s `_page()` for
        why this is safe here (no actor turn can be open yet when a story
        still mid-`Start Observing` calls this).

        `focusable=False` is a structural guarantee that `Focus(view=name)`
        can never make this track the main view -- for a track meant to
        always stay a corner inset (e.g. a shell that should never take
        over the full frame), this is safer than simply never writing such
        a `Focus` call, which a later story edit could still do by mistake.
        `fade=True` makes that corner inset itself fade out past its left
        third (see `compose.pad_inset_faded`) -- most useful together with
        `focusable=False`, so the one thing always on screen in that corner
        gradually gives way to the main view, but independent of it: a
        focusable track can fade too, it only ever affects its own inset
        rendering, never a segment where it is main. `scale`/`margin`/
        `border`/`corner` override this track's own inset size/spacing/
        corner (schema defaults 0.4/24/3/bottom-left) -- `None` (the
        default for each) leaves it unset so the schema default applies;
        only ever affects its inset rendering too, same as `fade`. Set
        `corner` when a take has more than one always-present inset at
        once (another track, or a focus-demoted actor/observer) that would
        otherwise collide in the same corner -- e.g. a second track at
        `corner=top-left` alongside a first left at the `bottom-left`
        default.

        A track name must be unique across the whole take -- this raises
        if the name was already recorded earlier (even if its
        earlier `Start Track`/`End Track` pair already closed), since
        `Focus(view=name)` and `compose()` both resolve a track by name
        alone and could not tell two same-named clips apart."""
        if _SESSION.started is None:
            raise FatalError("No observer -- call Start Observer first")
        if name in _SESSION.tracks:
            raise FatalError(f"Start Track was already called for {name!r}")
        if _SESSION.timeline is not None and any(
            t["name"] == name for t in _SESSION.timeline.tracks
        ):
            # Checked here, not only by add_track_clip() at End Track, so a
            # reused name fails before the whole track is recorded for nothing.
            raise FatalError(
                f"Track {name!r} was already recorded earlier in this take -- "
                "track names must be unique"
            )
        # Robot Framework passes these as strings (their default is None, so
        # it has no type to convert to); timeline.json needs numbers.
        scale = None if scale is None else float(scale)
        margin = None if margin is None else int(margin)
        border = None if border is None else int(border)
        if corner is not None and corner not in TRACK_CORNERS:
            raise FatalError(
                f"Unknown corner {corner!r} -- expected one of "
                f"{', '.join(TRACK_CORNERS)}"
            )
        context_kwargs = {"viewport": _SESSION.viewport}
        if _SESSION.record:
            context_kwargs["record_video_dir"] = str(_SESSION.take_dir)
            context_kwargs["record_video_size"] = _SESSION.viewport
        if storage_state:
            context_kwargs["storage_state"] = storage_state
        try:
            context = _browser_for(name).new_context(**context_kwargs)
            context.add_init_script(CURSOR_SCRIPT)
            page = context.new_page()
            # Same reasoning as start_observer(): capture the offset at
            # page creation, before goto(), so it is not inflated by
            # however long the first navigation took.
            offset = _SESSION.elapsed()
            _track_console(page)
            _SESSION.open_pages.append(page)
            page.goto(url, wait_until="load")
        except Exception as error:
            raise FatalError(
                f"Could not start track {name!r} at {url}: {error}"
            ) from error
        _SESSION.tracks[name] = {
            "context": context,
            "page": page,
            "offset": offset,
            "focusable": _as_bool(focusable),
            "fade": _as_bool(fade),
            "scale": scale,
            "margin": margin,
            "border": border,
            "corner": corner,
        }
        _SESSION.current_page = page

    def end_track(self, name):
        """Close a track opened with `Start Track`, flushing its video and
        recording it on the timeline. Call before `End Observer` (the
        observer "closes last" -- see its own docstring)."""
        track = _SESSION.tracks.get(name)
        if track is None:
            raise FatalError(f"No track named {name!r} -- call Start Track first")
        video_path = (
            track["page"].video.path()
            if _SESSION.record and track["page"].video
            else None
        )
        track["context"].close()
        del _SESSION.tracks[name]
        if _SESSION.current_page is track["page"]:
            _SESSION.current_page = _SESSION.observer_page
        if _SESSION.timeline is not None and video_path:
            _SESSION.timeline.add_track_clip(
                name,
                Path(video_path).name,
                offset=track["offset"],
                focusable=track["focusable"],
                fade=track["fade"],
                scale=track["scale"],
                margin=track["margin"],
                border=track["border"],
                corner=track["corner"],
            )
            _save_timeline()

    def observe(self, url=None, wait=DEFAULT_OBSERVE_WAIT, reload=False):
        """Bring the observer to the front, and refresh it. With `url`, this
        is an in-app route change (`page.goto()`), never `page.reload()`:
        a reload re-bootstraps a client-side app and puts a flash in the
        middle of the view, where an in-app route change does not. Only
        pass `reload=True` for the documented exception (see
        docs/AGENTS.md): forcing a refresh past auto-refresh's own polling
        interval right after a transition that can otherwise complete
        between intervals -- an in-app route change is still the default."""
        page = _SESSION.observer_page
        if page is None:
            raise FatalError("No observer -- call Start Observer first")
        page.bring_to_front()
        if reload:
            page.reload(wait_until="load")
        elif url:
            page.goto(url, wait_until="load")
        _SESSION.current_page = page
        page.wait_for_timeout(int(float(wait) * 1000))

    def start_scratch_context(
        self, url=None, storage_state=None, http_credentials=None
    ):
        """Open an unrecorded, throwaway context/page -- e.g. to complete
        an OIDC login flow that would otherwise put a login redirect in a
        recording (see docs/AGENTS.md), or to do privileged setup (deploy
        fixtures, clear old content) via HTTP Basic Auth rather than a
        Manager's own form login. Never touches the timeline: no
        turn_start/turn_end event, and it does not count as an actor turn.
        Pair with `End Scratch Context`."""
        self.start_browser()
        if _SESSION._scratch_context is not None:
            raise FatalError(
                "A scratch context is already open -- call End Scratch Context first"
            )
        context_kwargs = {"viewport": _SESSION.viewport}
        if storage_state:
            context_kwargs["storage_state"] = storage_state
        if http_credentials:
            context_kwargs["extra_http_headers"] = _basic_auth_headers(
                http_credentials["username"], http_credentials["password"]
            )
        context = _SESSION.browser.new_context(**context_kwargs)
        page = context.new_page()
        _track_console(page)
        _SESSION.open_pages.append(page)
        if url:
            page.goto(url, wait_until="load")
        _SESSION._scratch_context = context
        _SESSION._page_before_scratch = _SESSION.current_page
        _SESSION.current_page = page

    def end_scratch_context(self):
        """Close the scratch context and restore whatever page was current
        before `Start Scratch Context` (typically none yet, if this ran
        ahead of `Start Observer` as intended)."""
        context = _SESSION._scratch_context
        if context is None:
            raise FatalError("No scratch context -- call Start Scratch Context first")
        context.close()
        _SESSION._scratch_context = None
        _SESSION.current_page = _SESSION._page_before_scratch
        _SESSION._page_before_scratch = None

    def get_storage_state(self):
        """The current page's context storage_state() (cookies, local
        storage) -- typically assigned to a variable right before `End
        Scratch Context` and passed on to `Start Observer`'s
        `storage_state` argument, to carry an OIDC session into a recorded
        context without recording the login redirect."""
        return self._page().context.storage_state()

    # -- actor turns ------------------------------------------------------

    def start_actor_turn(
        self,
        actor,
        eyebrow=None,
        title=None,
        subtitle=None,
        password=None,
        anonymous=False,
    ):
        """Open a short recorded context for one persona turn. Use as
        `[Setup]` on the Task that plays the turn, with `End Actor Turn` as
        its `[Teardown]` -- the context is created immediately before the
        turn and closed immediately after, so no wall time it is open goes
        undriven and becomes dead air in its clip.

        Authenticates the context via HTTP Basic Auth as `actor`/`password`
        (defaulting `password` to `actor`, this project's convention for its
        demo users) unless `anonymous=True`, via an explicit `Authorization`
        header on the context (see `_basic_auth_headers` for why Playwright's
        `http_credentials` option cannot be used). Without this, every turn
        ran as an anonymous visitor regardless of `actor`, which most
        stories cannot get past their first permission-gated click.

        With `title`, also records a `chapter` event: a title card the
        composer inserts ahead of this turn's clip. No time is spent
        waiting on it in the browser -- unlike the title overlay the old
        e2e_*.py scripts drew and waited 8s for on every turn.
        """
        if _SESSION.observer_context is None:
            if _SESSION.record:
                raise FatalError("No observer -- call Start Observer first")
            # Not recording, so there is no observer clip to time the turn
            # against: allow it, so one task can be re-run on its own.
            self.start_browser()
        if _SESSION._turn_context is not None:
            raise FatalError(
                f"Actor turn for {_SESSION.current_actor!r} was not closed "
                "with End Actor Turn before starting a new one"
            )
        offset = _SESSION.elapsed()
        context_kwargs = {"viewport": _SESSION.viewport}
        if _SESSION.record:
            context_kwargs["record_video_dir"] = str(_SESSION.take_dir)
            context_kwargs["record_video_size"] = _SESSION.viewport
        if not _as_bool(anonymous):
            context_kwargs["extra_http_headers"] = _basic_auth_headers(
                actor, password or actor
            )
        try:
            context = _browser_for(actor).new_context(**context_kwargs)
            context.add_init_script(CURSOR_SCRIPT)
            page = context.new_page()
            # The injected cursor's CSS centers it by default, but Chromium
            # has no real "last mouse position" yet on a brand-new context,
            # so an incidental mousemove (Playwright's own actionability/
            # hover checks can synthesize one) at that uninitialized (0, 0)
            # would override the CSS with pixel coordinates and snap the
            # visible cursor to the corner until the story's first Human
            # Move. Centering Playwright's own tracked position up front
            # keeps any such incidental event centered too.
            page.mouse.move(
                _SESSION.viewport["width"] / 2, _SESSION.viewport["height"] / 2
            )
            _track_console(page)
            _SESSION.open_pages.append(page)
        except Exception as error:
            raise FatalError(f"Could not open a turn for {actor}: {error}") from error
        _SESSION._turn_context = context
        _SESSION._turn_started_at = offset
        _SESSION.current_actor = actor
        _SESSION.current_page = page
        _SESSION._turn_page = page
        # turn_start (and its chapter, if any) is *not* recorded here, at
        # context creation -- the composer cuts into the turn's clip at
        # that mark, and the page is still blank/pre-paint at this exact
        # instant. Deferred to the first Go To's completion instead (see
        # _record_turn_start, called from go_to()), which already waits
        # for "load"; End Actor Turn falls back to this offset if the turn
        # never navigates at all.
        _SESSION._turn_start_recorded = False
        _SESSION._pending_turn_meta = {
            "actor": actor,
            "eyebrow": eyebrow,
            "title": title,
            "subtitle": subtitle,
        }

    def _record_turn_start(self, at=None):
        """Add the pending turn's turn_start (and chapter, if any) event,
        at `at` seconds (or now, if `at` is None). No-op once already
        recorded for this turn, or if there is no turn open."""
        meta = _SESSION._pending_turn_meta
        if meta is None or _SESSION._turn_start_recorded or _SESSION.timeline is None:
            return
        time_at = _SESSION.elapsed() if at is None else at
        _SESSION.timeline.add_event(
            {"type": "turn_start", "time": time_at, "actor": meta["actor"]}
        )
        if meta["title"]:
            _SESSION.timeline.add_event(
                {
                    "type": "chapter",
                    "time": time_at,
                    "eyebrow": meta["eyebrow"] or "",
                    "title": meta["title"],
                    "subtitle": meta["subtitle"] or "",
                    "duration": 8.0,
                }
            )
        _SESSION._turn_start_recorded = True
        _save_timeline()

    def end_actor_turn(self, return_to_observer=True):
        """Close the current actor turn's context, flush its video, and
        register it on the timeline. Use as `[Teardown]`."""
        context = _SESSION._turn_context
        if context is None:
            raise FatalError("No actor turn is open -- call Start Actor Turn first")
        # Fallback for a turn that never navigated (Go To normally records
        # this at the first painted frame instead -- see go_to()).
        self._record_turn_start(at=_SESSION._turn_started_at)
        # `_turn_page`, not `current_page`: the latter is a single shared
        # pointer `observe()`/`start_track()` reassign as a side effect of
        # bringing a different page to the front, so a story that calls an
        # Observer-driving keyword (directly, or e.g. via a project's own
        # "Follow Instance Live") anywhere in this turn -- including right
        # before this very call -- would otherwise have this turn's own
        # clip silently recorded as *that other page's* video instead of
        # its own, with no error (see `_page()`'s own docstring for the
        # same bug in every other turn-scoped keyword, fixed the same way).
        page = _SESSION._turn_page
        video_path = page.video.path() if _SESSION.record and page.video else None
        context.close()
        end_offset = _SESSION.elapsed()
        actor = _SESSION.current_actor
        if _SESSION.timeline is not None:
            _SESSION.timeline.add_event(
                {"type": "turn_end", "time": end_offset, "actor": actor}
            )
            if video_path:
                _SESSION.timeline.add_actor_clip(
                    actor,
                    Path(video_path).name,
                    offset=_SESSION._turn_started_at,
                    duration=round(end_offset - _SESSION._turn_started_at, 3),
                )
            _save_timeline()
        _SESSION._turn_context = None
        _SESSION._turn_started_at = None
        _SESSION.current_actor = None
        _SESSION._pending_turn_meta = None
        _SESSION._turn_start_recorded = False
        _SESSION._turn_page = None
        if return_to_observer and _SESSION.observer_page is not None:
            # Longer than observe()'s own default: this is the cut back to
            # the wide/observer shot after a turn ends, not a brief in-app
            # navigation settle -- give the viewer time to register it. This
            # wait is real elapsed time in the observer's own recording, not
            # a synthetic freeze the composer inserts (unlike a story's own
            # Hold) -- record it as a "recorded" hold so verify's dead_air
            # check budgets for it too, without the composer double-freezing
            # already-real footage or the duration check expecting output
            # time that was never inserted (see compose.py's emit_holds_at
            # and verify.py's predicted_duration).
            if _SESSION.timeline is not None:
                _SESSION.timeline.add_event(
                    {
                        "type": "hold",
                        "time": end_offset,
                        "duration": DEFAULT_RETURN_TO_OBSERVER_WAIT,
                        "view": "observer",
                        "recorded": True,
                    }
                )
                _save_timeline()
            self.observe(wait=DEFAULT_RETURN_TO_OBSERVER_WAIT)

    # -- timeline-only events, no browser wait -----------------------------

    def chapter(self, eyebrow, title, subtitle, duration=8.0):
        """Record a title-card event at the current moment, without opening
        an actor turn. `duration` is how long the composer holds the card,
        not time spent waiting here."""
        if _SESSION.timeline is None:
            raise FatalError("No timeline -- call Start Observer first")
        _SESSION.timeline.add_event(
            {
                "type": "chapter",
                "time": _SESSION.elapsed(),
                "eyebrow": eyebrow,
                "title": title,
                "subtitle": subtitle,
                "duration": float(duration),
            }
        )
        _save_timeline()

    def focus(self, view, scale=0.4, margin=24, border=3, solo=False):
        """Record which recording is the composer's main view from this
        point on; the others become insets. `view` is `'actor'`,
        `'observer'`, or the `name` of a track opened with `Start Track` --
        the composer resolves a track name against the take's own recorded
        tracks at compose time (see `compose._view_at`), so this does not
        validate it against `_SESSION.tracks` here: a track closed with
        `End Track` earlier in the same turn, or not opened until later in
        the take, is still a legitimate name to focus temporarily away from
        and back to.

        `solo=True` turns every inset off from this point on -- not just
        the usual actor/observer cross-inset, but every track's own
        always-present corner inset too -- until the next `Focus` call
        says otherwise. Use it to end a take on the main view alone (e.g.
        matching a trailing `Hold`, which already never renders an inset):
        nothing else can turn an inset back off once a turn or a track has
        made one available."""
        if not view:
            raise FatalError(f"Focus view must be a non-empty name, got {view!r}")
        if _SESSION.timeline is None:
            raise FatalError("No timeline -- call Start Observer first")
        _SESSION.timeline.add_event(
            {
                "type": "focus",
                "time": _SESSION.elapsed(),
                "view": view,
                "scale": float(scale),
                "margin": int(margin),
                "border": int(border),
                "solo": bool(solo),
            }
        )
        _save_timeline()

    def hold(self, duration, view="observer"):
        """Record extra observer (or actor) time to hold at the current
        moment, e.g. after a submit, or over the final History view."""
        if _SESSION.timeline is None:
            raise FatalError("No timeline -- call Start Observer first")
        if view not in HOLD_VIEWS:
            # The schema only allows these two; anything else would make
            # the whole timeline.json fail to load at compose time.
            raise FatalError(
                f"Hold view must be one of {', '.join(HOLD_VIEWS)}, got {view!r}"
            )
        _SESSION.timeline.add_event(
            {
                "type": "hold",
                "time": _SESSION.elapsed(),
                "duration": float(duration),
                "view": view,
            }
        )
        _save_timeline()

    def caption(self, text, duration=DEFAULT_CAPTION_DURATION):
        """Record a caption event at the current (raw, observer-clock)
        moment. `compose()` writes every caption into a WebVTT sidecar
        (`output.vtt`, next to the composed output) if there is at least
        one, mapping this raw time to the cue's actual position in the
        composed output -- accounting for every title card/hold inserted
        before it, the same way the composer's own segment loop does."""
        if _SESSION.timeline is None:
            raise FatalError("No timeline -- call Start Observer first")
        _SESSION.timeline.add_event(
            {
                "type": "caption",
                "time": _SESSION.elapsed(),
                "text": text,
                "duration": float(duration),
            }
        )
        _save_timeline()

    def hide_cursor(self):
        """Hide the injected human-paced cursor
        (`#screencast-recording-cursor`) on the current page outright,
        instead of relying on its ~3s idle fade (see `screencast/cursor.py`)
        -- for a turn with no mouse interaction at all (a terminal, say:
        see "Using a ttyd terminal as the observer" in reference.md), where
        the cursor looks out of place even appearing once. The click-ripple
        (`.screencast-recording-click`) is a separate, classless element the
        click handler creates fresh each time; hidden here too, since a
        turn that calls this has usually already done its one deliberate
        click (e.g. to focus a terminal) before going keyboard-only.

        Stays in effect for the rest of the page's context (the turn, the
        track, or the observer), across navigations too: the cursor is
        re-injected on every new document, so this is as well."""
        page = self._page()
        page.context.add_init_script(HIDE_CURSOR_SCRIPT)
        page.add_style_tag(content=HIDE_CURSOR_CSS)

    # -- human-paced input, against the current page -----------------------

    def _locator(self, selector, index=0):
        """Resolve `selector` against the current page, at `index` (0 is
        the first match, -1 the last) -- e.g. the most recently created row
        in a table that only ever grows, which project keywords need and
        Playwright's own `.first`/`.last` express.

        A `label=<text>` selector resolves via `page.get_by_label()`
        instead of `page.locator()`: form-js (and most form libraries)
        associate an input with its visible label rather than an
        accessible role+name, and `get_by_label` has no plain-string
        equivalent in Playwright's own locator engine syntax (unlike
        `role=`, which `.locator()` already understands natively).

        A `<frame selector> >>> <inner selector>` selector resolves via
        `page.frame_locator()` -- `>>>` looks like Playwright's own
        shadow-DOM-piercing combinator but does *not* cross an `<iframe>`
        boundary (verified: it parses as a plain child combinator against
        the iframe *element*, which times out), so real `<iframe>` content
        -- a rich-text editor's body, for instance -- needs this instead."""
        if selector.startswith("label="):
            locator = self._page().get_by_label(selector[len("label=") :])
        elif " >>> " in selector:
            frame_selector, _, inner_selector = selector.partition(" >>> ")
            locator = self._page().frame_locator(frame_selector).locator(inner_selector)
        else:
            locator = self._page().locator(selector)
        index = int(index)
        if index == 0:
            return locator.first
        if index == -1:
            return locator.last
        return locator.nth(index)

    def human_move(self, selector, index=0):
        page = self._page()
        locator = self._locator(selector, index)
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        if box is None:
            raise AssertionError(f"{selector!r} has no bounding box to move to")
        page.mouse.move(
            box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=MOVE_STEPS
        )
        page.wait_for_timeout(MOVE_SETTLE_MS)

    def human_click(self, selector, index=0):
        self.human_move(selector, index)
        self._locator(selector, index).click()
        self._page().wait_for_timeout(CLICK_SETTLE_MS)

    def human_type(self, selector, text, delay=TYPE_DELAY_MS, index=0):
        self.human_click(selector, index)
        locator = self._locator(selector, index)
        locator.fill("")
        locator.press_sequentially(text, delay=int(delay))
        self._page().wait_for_timeout(FILL_SETTLE_MS)

    def paste_text(self, selector, text, index=0):
        """Fill a long body of text in one shot instead of Human Type's
        per-keystroke pacing -- typing hundreds of characters at 75ms each
        would stretch a turn's recording by tens of seconds for no benefit."""
        self.human_click(selector, index)
        self._locator(selector, index).fill(text)
        self._page().wait_for_timeout(FILL_SETTLE_MS)

    def press_key(self, selector, key, index=0):
        """Focus `selector` and press one key (`Enter`, `Tab`,
        `ArrowDown`, ...) -- e.g. to pick a suggestion from a tag-list
        input after typing into it, which typing alone never confirms."""
        self.human_move(selector, index)
        self._locator(selector, index).press(key)
        self._page().wait_for_timeout(FILL_SETTLE_MS)

    def wait_until_visible(self, selector, timeout=10000, index=0):
        self._locator(selector, index).wait_for(state="visible", timeout=int(timeout))

    def count_matches(self, selector):
        """The number of elements matching `selector` right now -- for a
        project keyword branching on whether something is present (an IF,
        not a wait), e.g. Cockpit rendering one of two possible layouts."""
        return self._page().locator(selector).count()

    def get_attribute(self, selector, name, index=0):
        return self._locator(selector, index).get_attribute(name)

    def select_option(self, selector, value, index=0):
        """Select `value` on a `<select>` -- there is no human-paced
        equivalent for a native select dropdown the way there is for a
        click or a typed field, so this does not move the mouse first."""
        self._locator(selector, index).select_option(value)
        self._page().wait_for_timeout(FILL_SETTLE_MS)

    def check(self, selector, index=0):
        self.human_move(selector, index)
        self._locator(selector, index).check()
        self._page().wait_for_timeout(CLICK_SETTLE_MS)

    def uncheck(self, selector, index=0):
        self.human_move(selector, index)
        self._locator(selector, index).uncheck()
        self._page().wait_for_timeout(CLICK_SETTLE_MS)

    def go_to(self, url):
        """Navigate the current page. A thin wrapper over `page.goto()` --
        project keywords needing anything more specific (auth, polling
        redirects) build on `Get Current Page` instead.

        If this is the first navigation of an open actor turn's own page,
        also records that turn's deferred turn_start/chapter here, now
        that `wait_until="load"` above has it painted (see
        `_record_turn_start`). Guarded by identity, not just "a turn is
        open", so a scratch context opened mid-turn (not done by any
        current story, but not forbidden either) navigating its own,
        different page does not misattribute the mark."""
        page = self._page()
        page.goto(url, wait_until="load")
        if page is _SESSION._turn_page and not _SESSION._turn_start_recorded:
            self._record_turn_start()

    # -- state shared between tasks and runs -------------------------------

    def save_state(self, key, value):
        """Keep `value` under `key` in `<take dir>/state.json`, for a later
        task, suite or run to read back with `Load State`.

        A variable assigned in a Robot Framework task is local to it, and
        `Set Suite Variable` lives only in this process, so a re-run of one
        task (`run --take <dir> --task ...`) would start without what the
        earlier tasks learned -- a created item's URL, say. The file is
        written at once (atomically), like `timeline.json`.

        `value` must be JSON: a string, number, boolean, list or dict (build
        one with `${{ ... }}`). The file may hold credentials (a Playwright
        storage state, say): it lives in the take directory, which is not
        meant to be committed.
        """
        try:
            json.dumps(value)
        except (TypeError, ValueError) as error:
            raise AssertionError(
                f"State {key!r} must be JSON (a string, number, boolean, list or "
                f"dict), not a {type(value).__name__}: {error}"
            ) from error
        state = _read_state()
        state[str(key)] = value
        _write_state(state)

    def load_state(self, key, default=_MISSING):
        """The value `Save State` kept under `key`, or `default` when there is
        none. Without a `default`, a missing key fails with the keys that do
        exist. A full `screencast run` starts from empty state; `run --task`
        (with the same `--take`) continues from what the previous run saved."""
        state = _read_state()
        if str(key) in state:
            return state[str(key)]
        if default is not _MISSING:
            return default
        saved = ", ".join(sorted(state)) or "nothing"
        raise AssertionError(
            f"No state saved under {key!r} (saved: {saved}) in {_state_path()}. "
            "Run the task that saves it first, or re-run with --take pointing "
            "at the take directory that has it."
        )

    def clear_state(self):
        """Forget everything `Save State` kept. A full `screencast run` does
        this by itself; call it first in a story run some other way."""
        _state_path().unlink(missing_ok=True)

    def get_url(self):
        """The current page's URL."""
        return self._page().url

    def wait_for_navigation_away(self, from_url, error_selector=None, timeout=15):
        """Wait until the current page is no longer at `from_url`, i.e. a
        form submit actually went through and redirected somewhere else.

        A click on a submit button that the page then rejects (a form-js
        field that failed validation, say) leaves the URL unchanged and
        nothing on screen says the story went wrong, so a story that never
        completed its task would still "pass" here and only fail, much
        later, in an unrelated turn. Fail at the step that failed instead:
        immediately, with the visible message, when an element matching
        `error_selector` is on screen; otherwise when `timeout` (seconds, or
        a Robot time string such as `15s`) runs out.
        """
        page = self._page()
        limit = timestr_to_secs(timeout)
        deadline = time.monotonic() + limit
        while True:
            if page.url != from_url:
                return
            if error_selector:
                errors = page.locator(error_selector)
                if errors.count() and errors.first.is_visible():
                    message = errors.first.inner_text().strip() or repr(error_selector)
                    raise AssertionError(
                        f"The form at {from_url} was not submitted: {message}"
                    )
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"The page stayed at {from_url} for {limit:g}s after the "
                    "submit: the form was not accepted"
                )
            page.wait_for_timeout(250)

    def get_current_page(self):
        """Return the live Playwright Page for the current context, for
        project keywords that need the raw Playwright API (e.g. `.request`
        for a JSON fetch, or `.keyboard`)."""
        return self._page()

    def get_observer_page(self):
        """Return the raw Playwright Page for the Observer specifically,
        regardless of whether an actor turn is currently open -- unlike
        `Get Current Page`/every other keyword here, which routes through
        `_page()` and so stays pinned to an open turn's own page by design
        (see `_page()`'s own docstring: that is what keeps a mid-turn
        `Observe` call from hijacking the rest of the turn's Human Click/
        Type/Wait Until Visible). A project keyword that itself needs to
        interact with something living on the Observer's own page (a
        toggle button, say) from *within* an open turn -- the whole point
        of driving the Observer mid-turn via a `Follow Instance Live`-style
        keyword -- needs this instead, or it would silently operate on the
        turn's own page too."""
        if _SESSION.observer_page is None:
            raise FatalError("No observer -- call Start Observer first")
        return _SESSION.observer_page

    def take_screenshot(self, path, full_page=True):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._page().screenshot(path=str(path), full_page=_as_bool(full_page))

    def _page(self):
        # While an actor turn is open, every input/wait/read keyword must
        # stay pinned to *that turn's own page* -- not whatever page was
        # driven most recently. `current_page` is a single shared pointer
        # that `observe()` (and `start_scratch_context()`) reassign as a
        # side effect of simply bringing a different page to the front; a
        # story that calls an Observer-driving keyword (e.g. `Observe`, or
        # a project keyword built on it) in between a turn's own keywords --
        # to have the Observer follow along live, mid-turn, rather than only
        # between turns -- would otherwise silently redirect every
        # subsequent Human Click/Type/Wait Until Visible/... in that turn to
        # the Observer's page instead, with no error: selectors just never
        # match on a page the story never intended to drive. `_turn_page` is
        # set once, when the turn opens, and untouched by anything else
        # until the turn ends, so it is the one reliable answer to "which
        # page is this turn's own" regardless of what else ran in between.
        page = (
            _SESSION._turn_page
            if _SESSION._turn_context is not None
            else _SESSION.current_page
        )
        if page is None:
            raise FatalError("No open page -- call Start Observer first")
        return page


def _basic_auth_headers(username, password):
    """An explicit `Authorization: Basic` header for a context's
    `extra_http_headers`.

    Playwright's own `http_credentials` context option does NOT work for
    Plone: it only answers a 401 challenge, and Plone serves its pages to
    anonymous visitors with a 200 and never challenges -- so the browser
    never sends the credentials and the page (and every `fetch()` from it)
    stays anonymous. Verified against a live Plone: with `http_credentials`
    (even `send="always"`) the front page is anonymous and
    `@bpmproxy-deployments` returns 401; with this header both are logged in.
    """
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "no", "0", "")
