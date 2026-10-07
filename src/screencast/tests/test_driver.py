"""screencast.driver tests, against the fake Playwright in fakes.py -- real
Robot Framework execution, no real browser. Manually verified separately
against a real headless Chromium and a toy two-actor story (see the #4/#5
commit messages); that is not repeated here since it would need a browser
in CI."""

from screencast import driver
from screencast import library as library_module
from screencast.tests.fakes import fake_sync_playwright
from screencast.tests.fakes import FakePlaywright
import io
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


PASSING_STORY = """\
*** Settings ***
Library    screencast.Screencast    take_dir=${TAKE_DIR}    record=${RECORD}

*** Test Cases ***
Watches
    Start Observer    observer    http://example.test

Turn
    [Setup]    Start Actor Turn    author    title=Author
    No Operation
    [Teardown]    End Actor Turn
"""

FAILING_STORY = """\
*** Settings ***
Library    screencast.Screencast    take_dir=${TAKE_DIR}    record=${RECORD}

*** Test Cases ***
Broken
    [Setup]    Start Actor Turn    author
    No Operation
    [Teardown]    End Actor Turn
"""

ORDINARY_FAILURE_STORY = """\
*** Settings ***
Library    screencast.Screencast    take_dir=${TAKE_DIR}    record=${RECORD}

*** Test Cases ***
Broken
    Fail    something in the page did not look right

After
    No Operation
"""

EVENTUAL_SUCCESS_STORY = """\
*** Settings ***
Library    screencast.Screencast    take_dir=${TAKE_DIR}    record=${RECORD}

*** Test Cases ***
Eventually Succeeds
    Start Observer    observer    http://example.test
    Wait Until Keyword Succeeds    5x    0.01s    Fail Twice Then Pass
    [Teardown]    End Observer

*** Keywords ***
Fail Twice Then Pass
    ${count}=    Get Variable Value    ${ATTEMPT_COUNT}    ${0}
    ${count}=    Evaluate    ${count} + 1
    Set Suite Variable    ${ATTEMPT_COUNT}    ${count}
    IF    ${count} < 3
        Fail    not yet
    END
"""

GENUINE_FAILURE_WITH_RETRIES_STORY = """\
*** Settings ***
Library    screencast.Screencast    take_dir=${TAKE_DIR}    record=${RECORD}

*** Test Cases ***
Genuinely Fails
    Start Observer    observer    http://example.test
    Wait Until Keyword Succeeds    3x    0.01s    Fail    still broken
    [Teardown]    End Observer
"""

RECOVERS_THEN_REALLY_FAILS_STORY = """\
*** Settings ***
Library    screencast.Screencast    take_dir=${TAKE_DIR}    record=${RECORD}

*** Test Cases ***
Recovers Then Really Fails
    Start Observer    observer    http://example.test
    Wait Until Keyword Succeeds    5x    0.01s    Fail Twice Then Pass
    Should Be Equal    a    b
    [Teardown]    End Observer

*** Keywords ***
Fail Twice Then Pass
    ${count}=    Get Variable Value    ${ATTEMPT_COUNT}    ${0}
    ${count}=    Evaluate    ${count} + 1
    Set Suite Variable    ${ATTEMPT_COUNT}    ${count}
    IF    ${count} < 3
        Fail    not yet
    END
"""


def write_story(tmp_path, text, name="story.robot"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_run_passes_and_writes_a_timeline(tmp_path):
    story = write_story(tmp_path, PASSING_STORY)
    code, output = driver.run(story, take_dir=tmp_path / "take", quiet=True)
    assert code == 0
    assert output.exists()
    timeline_path = tmp_path / "take" / "timeline.json"
    assert timeline_path.exists()


def test_run_applies_headed_and_cdp_the_story_import_does_not_pass(tmp_path):
    """(regression) `run --headed` used to be dropped: only probe() put
    `headless` on the library import, and a story's own import never does."""
    story = write_story(tmp_path, PASSING_STORY)
    code, _ = driver.run(story, take_dir=tmp_path / "take", headless=False, quiet=True)
    assert code == 0
    assert FakePlaywright.instances[0].chromium.launched["headless"] is False


def test_run_with_cdp_attaches_instead_of_launching(tmp_path):
    story = write_story(tmp_path, PASSING_STORY)
    code, _ = driver.run(
        story, take_dir=tmp_path / "take", cdp="9222,author=9223", quiet=True
    )
    assert code == 0
    chromium = FakePlaywright.instances[0].chromium
    assert chromium.launched is None
    default, author = chromium.attached
    assert len(default.contexts) == 1  # the observer
    assert len(author.contexts) == 1  # the author's turn


def test_probe_passes_cdp_to_the_library(tmp_path):
    driver.probe(
        None,
        "Start Observer",
        ["observer", "http://example.test"],
        take_dir=tmp_path / "take",
        cdp="9222",
    )
    chromium = FakePlaywright.instances[0].chromium
    assert [b.endpoint_url for b in chromium.attached] == ["http://127.0.0.1:9222"]


def test_run_with_task_runs_only_that_task(tmp_path):
    story = write_story(tmp_path, PASSING_STORY)
    code, output = driver.run(
        story, task="Watches", take_dir=tmp_path / "take", quiet=True
    )
    assert code == 0
    from robot.api import ExecutionResult

    result = ExecutionResult(str(output))
    ran = [test.name for test in result.suite.all_tests if test.status != "SKIP"]
    assert ran == ["Watches"]


def test_run_failure_is_summarized_with_keyword_path(tmp_path):
    story = write_story(tmp_path, FAILING_STORY)
    code, output = driver.run(story, take_dir=tmp_path / "take", quiet=True)
    assert code != 0
    summary = driver.summarize_failures(output)
    assert "Broken" in summary
    assert "Start Actor Turn" in summary
    assert "FatalError" in summary


def test_run_stops_after_the_first_failed_task(tmp_path):
    """(regression, PR #14 review finding #8) run() used to run every task
    regardless of an earlier failure -- each carries its own Wait Until
    Keyword Succeeds retries, so a broken early task meant burning through
    every later one's retry loops for nothing. Uses an ordinary keyword
    failure (Fail), not one of screencast.library's own FatalErrors --
    robot.api.FatalError already aborts the whole run by itself, which
    would make this test pass regardless of whether run() asks for
    exitonfailure."""
    story = write_story(tmp_path, ORDINARY_FAILURE_STORY)
    code, output = driver.run(story, take_dir=tmp_path / "take", quiet=True)
    assert code != 0
    from robot.api import ExecutionResult

    result = ExecutionResult(str(output))
    statuses = {test.name: test.status for test in result.suite.all_tests}
    assert statuses["Broken"] == "FAIL"
    assert statuses["After"] != "PASS"


def test_failure_artifacts_dumped_once_per_task_not_per_retry_attempt(tmp_path):
    """(regression, PR #14 review finding #9) The listener used to dump a
    screenshot/aria-snapshot/console-log bundle on every failed keyword
    (end_keyword), including every failed attempt inside a Wait Until
    Keyword Succeeds retry loop -- noisy, and outright wrong when a later
    attempt succeeds and the task passes overall. FakePage.screenshot() is
    a no-op, so a dump's real, countable side effect is its .txt file."""
    take_dir = tmp_path / "take"

    # Two failed attempts, then a third that succeeds -- the task PASSES
    # overall, so nothing should be dumped.
    story = write_story(tmp_path, EVENTUAL_SUCCESS_STORY, name="eventual.robot")
    code, _ = driver.run(story, take_dir=take_dir, quiet=True)
    assert code == 0
    assert list(take_dir.glob("failure-*.txt")) == []

    # Every attempt fails -- the task FAILS overall, so exactly one bundle
    # should be dumped, not one per retry attempt.
    story = write_story(
        tmp_path, GENUINE_FAILURE_WITH_RETRIES_STORY, name="broken.robot"
    )
    code, _ = driver.run(story, take_dir=take_dir, quiet=True)
    assert code != 0
    assert len(list(take_dir.glob("failure-*.txt"))) == 1


def test_a_later_genuine_failure_gets_its_own_dump_not_a_recovered_ones(tmp_path):
    """(regression, PR #14 follow-up review, reproduced) The one dump slot
    per task used to be claimed by whichever failure happened first, even
    a *recovered* one -- a Wait Until Keyword Succeeds attempt that later
    succeeds. That left a genuine failure later in the same task with no
    dump of its own (the slot was taken), and the stale recovered
    attempt's artifacts sitting there instead. This is the shape it
    happens in for real: Open Task -> Wait For Task polls and fails a few
    times, then succeeds, and only then does e.g. a click actually fail."""
    take_dir = tmp_path / "take"
    story = write_story(tmp_path, RECOVERS_THEN_REALLY_FAILS_STORY)
    code, _ = driver.run(story, take_dir=take_dir, quiet=True)
    assert code != 0

    dumps = list(take_dir.glob("failure-*.txt"))
    assert len(dumps) == 1
    assert "keyword: Should Be Equal" in dumps[0].read_text()


def _story_with_recovery_boundary(snippet):
    return (
        "*** Settings ***\n"
        "Library    screencast.Screencast    take_dir=${TAKE_DIR}    record=${RECORD}\n"
        "\n"
        "*** Test Cases ***\n"
        "Recovers Then Really Fails\n"
        "    Start Observer    observer    http://example.test\n"
        f"{snippet}"
        "    Should Be Equal    a    b\n"
        "    [Teardown]    End Observer\n"
    )


RECOVERY_BOUNDARY_SNIPPETS = [
    (
        "Run Keyword And Ignore Error",
        "    Run Keyword And Ignore Error    Fail    not really\n",
    ),
    (
        "Run Keyword And Return Status",
        "    Run Keyword And Return Status    Fail    not really\n",
    ),
    (
        "Run Keyword And Expect Error",
        "    Run Keyword And Expect Error    *    Fail    not really\n",
    ),
    (
        "TRY/EXCEPT",
        "    TRY\n"
        "        Fail    not really\n"
        "    EXCEPT    AS    ${err}\n"
        "        Log    Caught: ${err}\n"
        "    END\n",
    ),
]


@pytest.mark.parametrize(
    "name,snippet",
    RECOVERY_BOUNDARY_SNIPPETS,
    ids=[n for n, _ in RECOVERY_BOUNDARY_SNIPPETS],
)
def test_other_recovery_boundaries_also_free_the_dump_slot(tmp_path, name, snippet):
    """(regression, #18) The dump-slot recovery fix (PR #14) only watched
    Wait Until Keyword Succeeds by name. Run Keyword And Ignore Error/
    Return Status/Expect Error convert a failure into a status they report
    through their own (always-PASS) result instead of failing themselves,
    and a TRY/EXCEPT structure isn't a keyword call at all so it can't be
    matched by name either -- each needed its own recovery-boundary
    tracking (_RECOVERING_WRAPPER_KEYWORDS / start_try/end_try) or a
    failure inside one would take the task's single dump slot and leave a
    later genuine failure with none of its own."""
    take_dir = tmp_path / "take"
    story = write_story(tmp_path, _story_with_recovery_boundary(snippet))
    code, _ = driver.run(story, take_dir=take_dir, quiet=True)
    assert code != 0

    dumps = list(take_dir.glob("failure-*.txt"))
    assert len(dumps) == 1, f"{name}: expected exactly one dump, got {len(dumps)}"
    assert "keyword: Should Be Equal" in dumps[0].read_text(), name


RUN_KEYWORD_AND_CONTINUE_ON_FAILURE_STORY = _story_with_recovery_boundary(
    "    Run Keyword And Continue On Failure    Fail    not really\n"
)


def test_run_keyword_and_continue_on_failure_does_not_wrongly_clear_a_real_failure(
    tmp_path,
):
    """(regression, #18) Run Keyword And Continue On Failure lets the test
    continue past a failure, but is still itself marked FAILED (unlike
    Ignore Error/Return Status/Expect Error, which always report PASS) --
    its own status already mirrors the wrapped keyword's, so including it
    in _RECOVERING_WRAPPER_KEYWORDS must not cause a genuine failure
    inside it to be treated as recovered."""
    take_dir = tmp_path / "take"
    story = write_story(tmp_path, RUN_KEYWORD_AND_CONTINUE_ON_FAILURE_STORY)
    code, _ = driver.run(story, take_dir=take_dir, quiet=True)
    assert code != 0

    dumps = list(take_dir.glob("failure-*.txt"))
    assert len(dumps) == 1
    assert "keyword: Fail" in dumps[0].read_text()


def test_summarize_failures_reports_all_tasks_passed(tmp_path):
    story = write_story(tmp_path, PASSING_STORY)
    _, output = driver.run(story, take_dir=tmp_path / "take", quiet=True)
    assert driver.summarize_failures(output) == "All tasks passed."


def test_check_passes_a_valid_story(tmp_path):
    story = write_story(tmp_path, PASSING_STORY)
    errors = driver.check(story, take_dir=tmp_path / "check")
    assert errors == []


def test_check_catches_a_keyword_typo_without_a_browser(tmp_path):
    story = write_story(
        tmp_path,
        PASSING_STORY.replace("Start Observer", "Start Observerrr"),
    )
    errors = driver.check(story, take_dir=tmp_path / "check")
    assert errors
    assert "Start Observerrr" in errors[0]
    # No browser was launched -- dryrun never executes keyword bodies.
    assert FakePlaywright.instances == []


def test_probe_runs_one_keyword_against_the_live_session(tmp_path):
    code = driver.probe(
        None,
        "Start Observer",
        ["observer", "http://example.test"],
        take_dir=tmp_path,
        record=False,
    )
    assert code == 0
    assert library_module._SESSION.observer_page is not None


def test_probe_passes_take_dir_and_record_as_named_library_args(tmp_path):
    """(regression, PR #14 review finding #5) probe() used to build the
    library import's args with `:` instead of `=` -- Robot Framework only
    recognizes `name=value` as named-argument syntax for a library import,
    so the whole string ("take_dir:/x") was passed as one positional
    argument instead: take_dir ended up holding that literal string, and
    record -- run through _as_bool() -- was always truthy (any non-empty
    string) regardless of the value actually asked for."""
    take_dir = tmp_path / "take"
    driver.probe(
        None,
        "Start Observer",
        ["observer", "http://example.test"],
        take_dir=take_dir,
        record=False,
    )
    assert library_module._SESSION.take_dir == take_dir
    assert library_module._SESSION.record is False


def test_probe_resolves_a_relative_resource_path_against_cwd(tmp_path, monkeypatch):
    """(regression, PR #14 review finding #6) probe()'s suite is built in
    memory, not via TestSuite.from_file_system, so it has no source file
    for Robot to resolve a relative --resource path against -- passing one
    straight through used to fail (or resolve against the wrong base)."""
    resource_dir = tmp_path / "resources"
    resource_dir.mkdir()
    (resource_dir / "project.resource").write_text(
        "*** Keywords ***\nDo The Thing\n    No Operation\n"
    )
    monkeypatch.chdir(tmp_path)

    code = driver.probe(
        "resources/project.resource",
        "Do The Thing",
        take_dir=tmp_path / "take",
        record=False,
    )
    assert code == 0


def test_probe_reuses_the_browser_across_calls(tmp_path):
    driver.probe(
        None, "Start Observer", ["observer", "http://example.test"], take_dir=tmp_path
    )
    browser_after_first = library_module._SESSION.browser
    driver.probe(None, "Chapter", ["Story", "Title", "Subtitle"], take_dir=tmp_path)
    assert library_module._SESSION.browser is browser_after_first
    assert len(FakePlaywright.instances) == 1


def test_keywords_lists_resource_keywords_with_docs(tmp_path):
    resource = tmp_path / "project.resource"
    resource.write_text(
        "*** Keywords ***\n"
        "Do The Thing\n"
        "    [Documentation]    Does the thing.\n"
        "    [Arguments]    ${x}\n"
        "    No Operation\n"
    )
    listing = driver.keywords(resource)
    assert "Do The Thing(x)" in listing
    assert "Does the thing." in listing


def test_render_log_writes_log_html(tmp_path):
    story = write_story(tmp_path, PASSING_STORY)
    _, _ = driver.run(story, take_dir=tmp_path / "take", quiet=True)
    log_path = driver.render_log(tmp_path / "take")
    assert log_path.exists()


MID_TURN_FAILURE_STORY = """\
*** Settings ***
Library    screencast.Screencast    take_dir=${TAKE_DIR}    record=${RECORD}

*** Test Cases ***
Turn
    Start Observer    observer    http://example.test
    Start Actor Turn    author
    Fail    boom mid-turn
    [Teardown]    End Actor Turn
"""


def test_repl_on_failure_probes_the_same_page_before_teardown_closes_it(tmp_path):
    """(#16) A nested TestSuite.run() from inside a listener callback is
    unsafe (see _ReplOnFailureListener's docstring) -- confirmed directly
    against the installed Robot Framework for this issue. repl_on_failure
    instead runs REPL-typed keywords via BuiltIn().run_keyword(), pausing
    before the failing task's own [Teardown] (End Actor Turn) closes the
    actor's context. Scripts one `Go To` at the REPL and checks it landed
    on the *actor's* still-open page, not on whatever current_page becomes
    once End Actor Turn later switches it back to the observer -- proving
    both that the probe ran against the live session and that it ran
    strictly before teardown."""
    story = write_story(tmp_path, MID_TURN_FAILURE_STORY)
    take_dir = tmp_path / "take"
    stdin = io.StringIO("Go To\thttp://probed.example.test\n")

    code, _ = driver.run(
        story,
        take_dir=take_dir,
        quiet=True,
        repl_on_failure=True,
        repl_stdin=stdin,
    )
    assert code != 0

    browser = FakePlaywright.instances[0].browser
    observer_context, actor_context = browser.contexts[0], browser.contexts[1]
    actor_page = actor_context.pages[0]
    assert actor_page.url == "http://probed.example.test"
    assert observer_context.pages[0].url != "http://probed.example.test"
    # End Actor Turn ran (closed the turn's context) -- but only after the
    # REPL above already ran against it.
    assert actor_context.closed is True


def test_repl_on_failure_does_not_pause_on_a_retry_that_later_succeeds(tmp_path):
    """A failure inside a Wait Until Keyword Succeeds retry loop that
    later recovers must not pop the REPL -- only a failure that escapes
    every recovery boundary should. Asserts on the printed banner, not
    just the exit code: pausing here would not itself break the story
    (the pause is a side effect of end_keyword, not a redirect of Robot's
    own control flow), so a wrongly early pause needs its own signal to
    catch -- an empty scripted stdin makes the REPL loop a no-op either
    way, and the retry keeps recovering into a passing task regardless."""
    story = write_story(tmp_path, EVENTUAL_SUCCESS_STORY)
    take_dir = tmp_path / "take"
    stdin = io.StringIO()  # never read from if no pause happens
    out = []

    code, _ = driver.run(
        story,
        take_dir=take_dir,
        quiet=True,
        repl_on_failure=True,
        repl_stdin=stdin,
        repl_out=out.append,
    )
    assert code == 0
    assert not any(line.startswith("repl-on-failure:") for line in out), out


def test_repl_on_failure_reports_a_failing_probe_and_keeps_looping(tmp_path):
    """A probed keyword that itself fails must not crash the REPL loop or
    recursively re-trigger a pause -- it just reports FAIL and reads the
    next line. Scripts three failing probes in a row: BuiltIn().run_keyword()
    fires the same listener's end_keyword for a probed keyword too (see the
    class docstring), so without the one-shot `_paused` guard, each failing
    probe would re-enter _maybe_pause -> _loop, printing the pause banner
    again and nesting the Python call stack one level deeper per failure --
    here surfaced as an extra banner per failing probe rather than exactly
    one, since the recursive calls share (and keep draining) the same
    stdin iterator."""
    story = write_story(tmp_path, MID_TURN_FAILURE_STORY)
    take_dir = tmp_path / "take"
    stdin = io.StringIO(
        "Fail\tprobe boom 1\n"
        "Fail\tprobe boom 2\n"
        "Fail\tprobe boom 3\n"
        "Go To\thttp://after-failing-probes.example.test\n"
    )
    out = []

    code, _ = driver.run(
        story,
        take_dir=take_dir,
        quiet=True,
        repl_on_failure=True,
        repl_stdin=stdin,
        repl_out=out.append,
    )
    assert code != 0

    banners = [line for line in out if line.startswith("repl-on-failure:")]
    assert len(banners) == 1, out

    browser = FakePlaywright.instances[0].browser
    actor_page = browser.contexts[1].pages[0]
    assert actor_page.url == "http://after-failing-probes.example.test"


def test_check_leaves_nothing_behind_in_the_working_directory(tmp_path, monkeypatch):
    """`check` used to write a timestamped take directory into the current
    directory on every run (default_take_dir(base=cwd))."""
    story = write_story(tmp_path, PASSING_STORY)
    monkeypatch.chdir(tmp_path)
    assert driver.check(story) == []
    assert sorted(path.name for path in tmp_path.iterdir()) == [story.name]


def test_version_prints_the_resolved_versions(capsys):
    from screencast.__main__ import main

    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    for label in (
        "python:",
        "robotframework:",
        "playwright:",
        "jsonschema:",
        "ffmpeg:",
    ):
        assert label in output
    assert "robotframework: not installed" not in output


WAIT_STORY = """\
*** Settings ***
Library    screencast.Screencast    take_dir=${TAKE_DIR}    record=${RECORD}

*** Test Cases ***
Waits
    Start Observer    observer    http://example.test
    Sleep    0.3s
    Sleep    0.05s
    Wait Until Keyword Succeeds    3x    0.01s    Tagged Poll
    Tagged Poll

*** Keywords ***
Tagged Poll
    [Tags]    screencast:wait
    Sleep    0.3s
"""


def test_waiting_keywords_are_recorded_as_wait_events(tmp_path):
    """The timeline records the story's own waiting, which `verify` judges
    (datakurre/collective.bpmproxy#15): built-in waits, and any keyword a
    project tags `screencast:wait`. Only the outermost wait counts (the
    Sleep inside a tagged poll is part of it), and a wait too short to be
    dead air (0.05s) is not recorded at all."""
    take_dir = tmp_path / "take"
    code, _ = driver.run(
        write_story(tmp_path, WAIT_STORY), take_dir=take_dir, quiet=True
    )
    assert code == 0
    timeline = json.loads((take_dir / "timeline.json").read_text())
    waits = [e for e in timeline["events"] if e["type"] == "wait"]
    assert [w["keyword"] for w in waits] == [
        "Sleep",
        "Wait Until Keyword Succeeds",
        "Tagged Poll",
    ]
    assert all(w["duration"] >= 0.29 for w in waits)


STATE_STORY = """\
*** Settings ***
Library    screencast.Screencast    take_dir=${TAKE_DIR}    record=${RECORD}

*** Test Cases ***
Create
    Save State    url    http://example.test/made

Use
    ${url}=    Load State    url
    Should Be Equal    ${url}    http://example.test/made
"""


def test_task_rerun_continues_from_the_state_the_previous_run_saved(tmp_path):
    """(datakurre/collective.bpmproxy#26) The point of state: after a full
    run, `run --take <same dir> --task Use` needs nothing from `Create`."""
    story = write_story(tmp_path, STATE_STORY)
    take_dir = tmp_path / "take"
    code, _ = driver.run(story, take_dir=take_dir, record=False, quiet=True)
    assert code == 0
    code, _ = driver.run(story, task="Use", take_dir=take_dir, record=False, quiet=True)
    assert code == 0


def test_task_rerun_without_saved_state_fails_and_says_why(tmp_path):
    story = write_story(tmp_path, STATE_STORY)
    code, output = driver.run(
        story, task="Use", take_dir=tmp_path / "fresh", record=False, quiet=True
    )
    assert code != 0
    assert "No state saved under 'url'" in driver.summarize_failures(output)


def test_a_full_run_does_not_see_state_left_by_a_previous_take(tmp_path):
    """`make screencast` writes every take to the same directory: what the
    last take saved must not leak into this one."""
    take_dir = tmp_path / "take"
    take_dir.mkdir()
    (take_dir / "state.json").write_text('{"stale": "from the previous take"}')
    story = write_story(tmp_path, STATE_STORY)
    code, _ = driver.run(story, take_dir=take_dir, record=False, quiet=True)
    assert code == 0
    assert json.loads((take_dir / "state.json").read_text()) == {
        "url": "http://example.test/made"
    }


def test_a_single_actor_turn_runs_on_its_own_when_not_recording(tmp_path):
    """`--no-record --task Turn`: the turn's story normally starts the
    observer first, which a partial run skips."""
    story = write_story(tmp_path, PASSING_STORY)
    code, _ = driver.run(
        story, task="Turn", take_dir=tmp_path / "take", record=False, quiet=True
    )
    assert code == 0
