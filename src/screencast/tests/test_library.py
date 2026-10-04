"""Unit tests for screencast.library, against the fake Playwright in
fakes.py -- no real browser involved, per the library's acceptance
criterion in collective/collective.bpmproxy#4."""

from pathlib import Path
from robot.api import FatalError
from screencast import library as library_module
from screencast.tests.fakes import fake_sync_playwright
from screencast.tests.fakes import FakePage
from screencast.tests.fakes import FakePlaywright
import json
import pytest
import sys
import types


@pytest.fixture(autouse=True)
def fake_playwright(monkeypatch):
    FakePlaywright.instances.clear()
    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = fake_sync_playwright
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)
    library_module._SESSION.reset()
    yield
    library_module._SESSION.reset()


def test_start_observer_starts_the_clock_and_a_timeline(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    assert library_module._SESSION.started is not None
    assert library_module._SESSION.timeline.observer["name"] == "cockpit"
    assert library_module._SESSION.current_page.url == "http://example.test/cockpit"


def test_start_observer_starts_the_clock_before_the_first_goto(tmp_path, monkeypatch):
    """(regression, PR #14 review finding #3) Video recording begins at
    context.new_page(), not at the first goto -- starting the clock any
    later makes every timeline timestamp (turn offsets, turn_start/end,
    chapter, focus, hold) land earlier than its true position in the
    observer video by however long that first navigation took."""
    clock = {"t": 100.0}
    monkeypatch.setattr(library_module.time, "monotonic", lambda: clock["t"])

    real_goto = FakePage.goto

    def slow_goto(self, url, wait_until="load"):
        clock["t"] += 5.0  # simulate the first navigation taking 5s
        return real_goto(self, url, wait_until=wait_until)

    monkeypatch.setattr(FakePage, "goto", slow_goto)

    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")

    # Captured at new_page(), before slow_goto's +5s -- not the buggy 105.0.
    assert library_module._SESSION.started == 100.0
    clock["t"] += 2.0
    assert library_module._SESSION.elapsed() == pytest.approx(7.0)


def test_actor_turn_records_matching_start_and_end_events(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.start_actor_turn(
        "author", eyebrow="Story", title="Author", subtitle="Doing"
    )
    screencast.human_click("text=Add new")
    screencast.end_actor_turn()

    timeline = library_module._SESSION.timeline
    starts = timeline.events_of("turn_start")
    ends = timeline.events_of("turn_end")
    chapters = timeline.events_of("chapter")
    assert [event["actor"] for event in starts] == ["author"]
    assert [event["actor"] for event in ends] == ["author"]
    assert chapters[0]["title"] == "Author"
    assert timeline.actor_clip("author")["offset"] >= 0
    # human_click actually clicked, through the fake page.
    clicked_pages = [
        page
        for context in library_module._SESSION.browser.contexts
        for page in context.pages
        if page.clicked
    ]
    assert len(clicked_pages) == 1
    assert clicked_pages[0].clicked == ["text=Add new"]


def test_actor_keywords_stay_on_the_turn_page_through_a_mid_turn_observe(tmp_path):
    """(regression) A story that calls `Observe` (directly, or through a
    project keyword built on it) in the middle of an actor turn -- to have
    the Observer follow along live, rather than only between turns -- must
    not have its later Human Click/Type/Wait Until Visible/... silently
    redirected to the Observer's page. `observe()` reassigns the shared
    `current_page` pointer as a side effect of bringing the Observer to the
    front; `_page()` must route turn-scoped keywords through `_turn_page`
    instead, which only the turn's own start/end ever touch."""
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.start_actor_turn("author")
    screencast.go_to("http://example.test/actor")
    screencast.human_click("text=Before")
    screencast.observe("http://example.test/cockpit/instance/1")
    screencast.human_click("text=After")
    screencast.end_actor_turn()

    actor_pages = [
        page
        for context in library_module._SESSION.browser.contexts
        for page in context.pages
        if page.url == "http://example.test/actor"
    ]
    assert len(actor_pages) == 1
    assert actor_pages[0].clicked == ["text=Before", "text=After"]


def test_end_actor_turn_records_the_turn_pages_own_video_through_a_mid_turn_observe(
    tmp_path,
):
    """(regression) `end_actor_turn()` used to read `_SESSION.current_page`
    directly -- the same single shared pointer `observe()` reassigns (see
    the sibling test above) -- to decide *which page's video* becomes this
    turn's own clip on the timeline. A mid-turn `Observe` left it pointed
    at the Observer's page, so `end_actor_turn()` recorded the Observer's
    own video file as if it were this turn's clip: composing later would
    render the Observer's footage, trimmed at this turn's offsets, as the
    "actor main" view -- indistinguishable on screen from two Cockpit
    insets and no Tasklist at all."""
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    observer_video = library_module._SESSION.observer_page.video.path()

    screencast.start_actor_turn("author")
    screencast.go_to("http://example.test/actor")
    actor_video = library_module._SESSION._turn_page.video.path()
    screencast.observe("http://example.test/cockpit/instance/1")
    screencast.end_actor_turn()

    clip = library_module._SESSION.timeline.actor_clip("author")
    assert clip["video"] == Path(actor_video).name
    assert clip["video"] != Path(observer_video).name

    observer_page = library_module._SESSION.observer_page
    assert observer_page.clicked == []


def test_get_observer_page_stays_on_the_observer_through_an_open_turn(tmp_path):
    """(regression) A project keyword that needs to interact with
    something living on the Observer's own page (a toggle button, say)
    from *within* an open actor turn -- the point of driving the Observer
    mid-turn via a `Follow Instance Live`-style keyword at all -- must not
    go through `self._page()`/`Get Current Page`: those are turn-aware by
    design (see `_page()`'s own docstring) and would silently resolve to
    the turn's own page instead, exactly like `Reload Current Page` and
    `end_actor_turn()`'s own video selection did before each was fixed.
    `Get Observer Page`/`get_observer_page()` is the escape hatch."""
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    observer_page = library_module._SESSION.observer_page

    screencast.start_actor_turn("author")
    screencast.go_to("http://example.test/actor")

    assert screencast.get_observer_page() is observer_page
    assert screencast.get_current_page() is not observer_page

    screencast.end_actor_turn()


def test_turn_start_is_deferred_to_the_first_go_to(tmp_path):
    """(regression, #17) turn_start/chapter used to be recorded at context
    creation, before the turn's page had navigated anywhere -- the
    composer's cut into the turn's clip then landed on blank pre-paint
    frames. Deferring to the first Go To's completion (which already
    waits for "load") means the turn opens on painted content."""
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.start_actor_turn("author", eyebrow="e", title="Author", subtitle="s")

    timeline = library_module._SESSION.timeline
    assert timeline.events_of("turn_start") == []
    assert timeline.events_of("chapter") == []

    screencast.go_to("http://example.test/turn-page")

    starts = timeline.events_of("turn_start")
    chapters = timeline.events_of("chapter")
    assert [event["actor"] for event in starts] == ["author"]
    assert chapters[0]["title"] == "Author"
    assert chapters[0]["time"] == starts[0]["time"]

    screencast.end_actor_turn()
    # A second Go To later in the same turn must not add a duplicate mark.
    assert len(timeline.events_of("turn_start")) == 1


def test_turn_start_falls_back_to_context_creation_if_never_navigated(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.start_actor_turn("author")
    screencast.end_actor_turn()

    starts = library_module._SESSION.timeline.events_of("turn_start")
    assert [event["actor"] for event in starts] == ["author"]


def test_actor_turn_starts_with_the_cursor_centered(tmp_path):
    """(regression, #17) The injected cursor's CSS centers it by default,
    but an incidental early mousemove (e.g. from Playwright's own
    actionability/hover checks) at Chromium's uninitialized (0, 0)
    position would override that with pixel coordinates, snapping the
    visible cursor to the corner until the story's first Human Move.
    Centering Playwright's own tracked mouse position up front keeps any
    such incidental event centered too."""
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.start_actor_turn("author")

    page = library_module._SESSION.current_page
    viewport = library_module._SESSION.viewport
    assert page.mouse.moves == [(viewport["width"] / 2, viewport["height"] / 2, 1)]
    screencast.end_actor_turn()


def _basic(username, password):
    import base64

    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_actor_turn_authenticates_with_http_basic_auth_by_default(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.start_actor_turn("author")
    context = library_module._SESSION._turn_context
    assert context.kwargs["extra_http_headers"] == _basic("author", "author")
    assert "http_credentials" not in context.kwargs
    screencast.end_actor_turn()


def test_actor_turn_password_overrides_the_default(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.start_actor_turn("reviewer1", password="s3cret")
    context = library_module._SESSION._turn_context
    assert context.kwargs["extra_http_headers"] == _basic("reviewer1", "s3cret")
    screencast.end_actor_turn()


def test_anonymous_actor_turn_sets_no_credentials(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.start_actor_turn("visitor", anonymous=True)
    context = library_module._SESSION._turn_context
    assert "http_credentials" not in context.kwargs
    screencast.end_actor_turn()


def test_end_actor_turn_returns_to_observer_with_the_longer_wait(tmp_path, monkeypatch):
    """(regression, PR #14 review finding #11) DEFAULT_RETURN_TO_OBSERVER_WAIT
    was defined but never used -- end_actor_turn's auto-observe() call fell
    through to observe()'s own shorter DEFAULT_OBSERVE_WAIT instead."""
    waits = []
    real_wait_for_timeout = FakePage.wait_for_timeout

    def recording_wait_for_timeout(self, ms):
        waits.append(ms)
        return real_wait_for_timeout(self, ms)

    monkeypatch.setattr(FakePage, "wait_for_timeout", recording_wait_for_timeout)

    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.start_actor_turn("author")
    screencast.end_actor_turn()

    assert waits == [int(library_module.DEFAULT_RETURN_TO_OBSERVER_WAIT * 1000)]


def test_end_actor_turn_records_the_return_wait_as_a_recorded_hold(tmp_path):
    """(regression, PR #14 follow-up review) The return-to-observer wait is
    real elapsed time in the observer's own recording, not a synthetic
    freeze -- recording it as a "recorded" hold lets verify's dead_air
    check budget for it without the composer double-freezing already-real
    footage (see compose.py's emit_holds_at, verify.py's
    predicted_duration)."""
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.start_actor_turn("author")
    screencast.end_actor_turn()

    holds = library_module._SESSION.timeline.events_of("hold")
    assert len(holds) == 1
    assert holds[0]["recorded"] is True
    assert holds[0]["duration"] == library_module.DEFAULT_RETURN_TO_OBSERVER_WAIT
    assert holds[0]["view"] == "observer"


def test_end_actor_turn_without_return_to_observer_records_no_hold(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.start_actor_turn("author")
    screencast.end_actor_turn(return_to_observer=False)

    assert library_module._SESSION.timeline.events_of("hold") == []


def test_end_actor_turn_without_start_raises(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    with pytest.raises(FatalError):
        screencast.end_actor_turn()


def test_starting_a_second_turn_before_closing_the_first_raises(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.start_actor_turn("author")
    with pytest.raises(FatalError):
        screencast.start_actor_turn("author")


def test_chapter_focus_hold_add_timeline_events_without_waiting(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.chapter("Story", "Title", "Subtitle", duration=5.0)
    screencast.focus("actor", scale=0.3)
    screencast.hold(10.0)

    timeline = library_module._SESSION.timeline
    assert timeline.events_of("chapter")[0]["duration"] == 5.0
    assert timeline.events_of("focus")[0]["view"] == "actor"
    assert timeline.events_of("hold")[0]["duration"] == 10.0


def test_focus_rejects_unknown_view(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    with pytest.raises(FatalError):
        screencast.focus("sideways")


def test_browser_is_reused_across_repeated_instantiation(tmp_path):
    """This is the property the agent debug loop (screencast.driver) relies
    on: repeated `TestSuite(...).run()` calls construct a fresh Screencast()
    instance each time, but must not launch a second browser."""
    first = library_module.Screencast(take_dir=tmp_path)
    first.start_browser()
    browser_after_first = library_module._SESSION.browser

    second = library_module.Screencast(take_dir=tmp_path)
    second.start_browser()
    assert library_module._SESSION.browser is browser_after_first
    assert len(FakePlaywright.instances) == 1


def test_end_observer_closes_the_context_and_clears_current_page(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    context = library_module._SESSION.observer_context
    screencast.end_observer()

    assert context.closed
    assert library_module._SESSION.observer_context is None
    assert library_module._SESSION.current_page is None


def test_end_observer_without_start_raises(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    with pytest.raises(FatalError):
        screencast.end_observer()


def test_timeline_written_to_disk_by_the_listener_on_close(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.start_actor_turn("author", title="Author")
    screencast.end_actor_turn()
    library_module.Screencast.ROBOT_LIBRARY_LISTENER.close()

    timeline_path = Path(tmp_path) / "timeline.json"
    assert timeline_path.exists()
    from screencast.timeline import Timeline

    loaded = Timeline.load(timeline_path)
    assert loaded.actor_clip("author")


def test_timeline_is_saved_incrementally_not_only_at_listener_close(tmp_path):
    """(regression, PR #14 review finding #11) The timeline used to be
    written to disk only once, in _Listener.close() at the very end of the
    run -- a crash mid-take (an ffmpeg/browser death, a killed process)
    would lose every event recorded so far, even the ones for turns that
    had already finished and flushed their own video to disk."""
    screencast = library_module.Screencast(take_dir=tmp_path)
    timeline_path = Path(tmp_path) / "timeline.json"

    screencast.start_observer("cockpit", "http://example.test/cockpit")
    assert timeline_path.exists()  # saved before any turn -- no .close() yet

    screencast.start_actor_turn("author", title="Author")
    screencast.end_actor_turn()

    from screencast.timeline import Timeline

    # Still without calling the listener's close(): the crash this guards
    # against would happen well before Robot ever gets to call it.
    loaded = Timeline.load(timeline_path)
    assert loaded.actor_clip("author")
    assert loaded.events_of("turn_end")


def test_no_record_mode_skips_video_but_still_builds_timeline(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path, record=False)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.start_actor_turn("author", title="Author")
    screencast.end_actor_turn()

    timeline = library_module._SESSION.timeline
    assert timeline.actors == []  # no video path was produced to register
    assert timeline.events_of("turn_start")
    assert timeline.events_of("turn_end")


def test_paste_text_fills_without_per_keystroke_typing(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.paste_text("#body", "a long paragraph")
    page = library_module._SESSION.current_page
    assert page.filled["#body"] == "a long paragraph"


def test_human_click_with_index_minus_one_clicks_the_last_match(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.human_click("a.row", index=-1)
    locator = library_module._SESSION.current_page.locator("a.row")
    assert locator.last.index == -1


def test_a_label_selector_resolves_via_get_by_label(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.human_click("label=Approve")
    page = library_module._SESSION.current_page
    assert page.clicked == ["label=Approve"]


def test_a_frame_piercing_selector_resolves_via_frame_locator(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.paste_text("iframe >>> body", "rich text")
    page = library_module._SESSION.current_page
    assert page.filled["iframe >>> body"] == "rich text"


def test_select_option_sets_the_value(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.select_option("#kind", "example-process")
    page = library_module._SESSION.current_page
    assert page.filled["#kind"] == "example-process"


def test_observe_reload_reloads_instead_of_navigating(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.observe(url="http://example.test/should-not-navigate", reload=True)
    page = library_module._SESSION.observer_page
    assert page.reloaded == 1
    assert page.url == "http://example.test/cockpit"


def test_check_and_uncheck(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.check("#diagram-enabled")
    page = library_module._SESSION.current_page
    assert page.checked["#diagram-enabled"] is True
    screencast.uncheck("#diagram-enabled")
    assert page.checked["#diagram-enabled"] is False


def test_scratch_context_does_not_touch_the_timeline(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_scratch_context("http://example.test/login")
    state = screencast.get_storage_state()
    screencast.end_scratch_context()

    assert state == {}
    assert library_module._SESSION.timeline is None
    assert library_module._SESSION.current_page is None


def test_scratch_context_restores_the_previous_current_page(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    observer_page = library_module._SESSION.current_page

    screencast.start_scratch_context("http://example.test/login")
    assert library_module._SESSION.current_page is not observer_page
    screencast.end_scratch_context()

    assert library_module._SESSION.current_page is observer_page


def test_end_scratch_context_without_start_raises(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    with pytest.raises(FatalError):
        screencast.end_scratch_context()


def test_starting_a_second_scratch_context_before_closing_raises(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_scratch_context("http://example.test/login")
    with pytest.raises(FatalError):
        screencast.start_scratch_context("http://example.test/login")


def test_a_bare_library_import_does_not_reset_the_configured_session(tmp_path):
    """(found running a story against a live stack) A story imports the
    library with take_dir/record, then bpmproxy.resource imports it again
    with no arguments; Robot Framework constructs an instance per import.
    The second must not send a `--no-record` run's output to ".", nor turn
    recording back on."""
    library_module.Screencast(take_dir=tmp_path / "take", record=False)
    library_module.Screencast()
    assert library_module._SESSION.take_dir == tmp_path / "take"
    assert library_module._SESSION.record is False


def test_press_key_focuses_the_element_then_presses_the_key(tmp_path):
    """(found running review_process.robot live) Typing into a form-js tag
    list never confirms the entry -- the story has to press Enter, and the
    library had no keyword for it, so the port silently submitted an empty
    tag list."""
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("cockpit", "http://example.test/cockpit")
    screencast.start_actor_turn("lead")
    screencast.press_key(".fjs-taglist-input", "Enter")
    page = library_module._SESSION.current_page
    assert page.pressed == [(".fjs-taglist-input", "Enter")]
    assert page.mouse.moves  # moved to the element first, like Human Click
    screencast.end_actor_turn()


def test_take_screenshot_creates_its_missing_parent_directory(tmp_path):
    """Stories write doc-illustration screenshots under the take directory
    (<take>/screenshots/), which nothing else creates. Playwright's
    screenshot() may not create it, so Take Screenshot must."""
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("observer", "http://example.test")
    target = tmp_path / "screenshots" / "nested" / "shot.png"
    assert not target.parent.exists()
    screencast.take_screenshot(target)
    assert target.parent.is_dir()


def _observer(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.start_observer("observer", "http://example.test/form")
    return screencast, library_module._SESSION.current_page


def test_wait_for_navigation_away_returns_once_the_url_changes(tmp_path):
    screencast, page = _observer(tmp_path)
    ticks = []

    def redirect_after_two_polls(ms):
        ticks.append(ms)
        if len(ticks) == 2:
            page.url = "http://example.test/next"

    page.wait_for_timeout = redirect_after_two_polls
    screencast.wait_for_navigation_away("http://example.test/form", timeout=5)
    assert len(ticks) == 2


def test_wait_for_navigation_away_fails_when_the_page_never_leaves(tmp_path):
    screencast, _page = _observer(tmp_path)
    with pytest.raises(AssertionError, match="stayed at http://example.test/form"):
        screencast.wait_for_navigation_away("http://example.test/form", timeout="0.05s")


def test_wait_for_navigation_away_fails_at_once_with_the_visible_error(tmp_path):
    """The validation message is the useful part: fail immediately with it,
    not after the whole timeout with a generic message."""
    screencast, page = _observer(tmp_path)
    page.visible.add(".fjs-form-field-error")
    page.texts[".fjs-form-field-error"] = "Field is required."
    with pytest.raises(AssertionError, match="was not submitted: Field is required."):
        screencast.wait_for_navigation_away(
            "http://example.test/form",
            error_selector=".fjs-form-field-error",
            timeout=60,
        )


def test_wait_for_navigation_away_ignores_a_hidden_error_element(tmp_path):
    screencast, page = _observer(tmp_path)
    page.url = "http://example.test/next"  # already redirected
    screencast.wait_for_navigation_away(
        "http://example.test/form", error_selector=".fjs-form-field-error"
    )


def test_state_round_trips_and_survives_a_new_process(tmp_path):
    """State is a file in the take directory, so a later run -- here a
    brand-new session, as a new process would have -- reads it back."""
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.save_state("case_url", "http://example.test/case")
    screencast.save_state("entries", [{"id": "owner", "roles": ["Reader"]}])
    assert json.loads((tmp_path / "state.json").read_text()) == {
        "case_url": "http://example.test/case",
        "entries": [{"id": "owner", "roles": ["Reader"]}],
    }
    assert not list(tmp_path.glob("*.tmp")), "the write is atomic: no scratch file"

    library_module._SESSION.reset()
    again = library_module.Screencast(take_dir=tmp_path)
    assert again.load_state("case_url") == "http://example.test/case"
    assert again.load_state("entries")[0]["id"] == "owner"


def test_load_state_of_a_missing_key_names_the_keys_that_exist(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.save_state("one", "1")
    screencast.save_state("two", "2")
    with pytest.raises(AssertionError, match=r"'three' \(saved: one, two\)"):
        screencast.load_state("three")


def test_load_state_default_is_returned_even_when_it_is_none(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    assert screencast.load_state("missing", default="fallback") == "fallback"
    assert screencast.load_state("missing", default=None) is None


def test_save_state_rejects_a_value_that_is_not_json(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.save_state("kept", "yes")
    with pytest.raises(AssertionError, match="must be JSON.*not a object"):
        screencast.save_state("bad", object())
    assert json.loads((tmp_path / "state.json").read_text()) == {"kept": "yes"}


def test_a_corrupt_state_file_is_reported_not_silently_replaced(tmp_path):
    (tmp_path / "state.json").write_text("{not json")
    screencast = library_module.Screencast(take_dir=tmp_path)
    with pytest.raises(AssertionError, match="is not valid JSON"):
        screencast.load_state("anything")
    with pytest.raises(AssertionError, match="is not valid JSON"):
        screencast.save_state("k", "v")


def test_clear_state_forgets_everything(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    screencast.save_state("k", "v")
    screencast.clear_state()
    assert not (tmp_path / "state.json").exists()
    assert screencast.load_state("k", default="gone") == "gone"
    screencast.clear_state()  # nothing to clear is not an error


def test_actor_turn_without_an_observer_works_when_nothing_is_recorded(tmp_path):
    """A partial `--no-record` run (`--task`) plays one turn without the
    observer task that would normally have started it."""
    screencast = library_module.Screencast(take_dir=tmp_path, record=False)
    screencast.start_actor_turn("alice", title="Alice")
    screencast.go_to("http://example.test/")
    screencast.human_click("#go")
    screencast.end_actor_turn()
    assert library_module._SESSION.timeline is None
    assert library_module._SESSION._turn_context is None


def test_actor_turn_still_needs_an_observer_when_recording(tmp_path):
    screencast = library_module.Screencast(take_dir=tmp_path)
    with pytest.raises(FatalError, match="No observer"):
        screencast.start_actor_turn("alice")
