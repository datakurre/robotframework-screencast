"""The Python driver API behind `python -m screencast`: run/probe/keywords/
check/log, all runnable as plain functions too (see __main__.py).

The commands exist for one reason: an agent debugging a broken story should
never have to replay the whole thing blind. `run` gives a compact
keyword-path failure summary with the real Python traceback and the
listener's failure artifacts; `probe` re-runs one keyword against the same
live session `run` just left open; `keywords` and `check` answer "what
exists" and "does this parse" without spending a turn on the browser at all.
"""

from pathlib import Path
from robot.api import ExecutionResult
from robot.libraries.BuiltIn import BuiltIn
from robot.result import Keyword as ResultKeyword
from robot.result import Message as ResultMessage
from robot.running import TestSuite
from robot.running.builder import ResourceFileBuilder
from screencast.console import TaskConsole
from screencast.library import _RECOVERING_WRAPPER_KEYWORDS
from screencast.library import configure_browser
from screencast.library import Screencast
from screencast.library import STATE_FILE
import datetime
import sys
import tempfile


def versions():
    """The resolved versions of everything the engine runs on, so an
    environment mismatch (CI and devenv resolve Robot Framework, Playwright
    and ffmpeg independently) is obvious in a bug report."""
    import importlib.metadata
    import platform
    import shutil
    import subprocess

    found = {"python": platform.python_version()}
    for label, distribution in (
        ("robotframework", "robotframework"),
        ("playwright", "playwright"),
        ("jsonschema", "jsonschema"),
    ):
        try:
            found[label] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            found[label] = "not installed"
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        first_line = subprocess.run(
            [ffmpeg, "-version"], capture_output=True, text=True, check=False
        ).stdout.splitlines()[:1]
        found["ffmpeg"] = (
            first_line[0].removeprefix("ffmpeg version ") if first_line else "unknown"
        )
    else:
        found["ffmpeg"] = "not on PATH"
    return found


def default_take_dir(story, base=None):
    stem = Path(story).stem
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = Path(base) if base else Path.cwd() / "var" / "screencasts" / stem
    return base / stamp


def run(
    story,
    task=None,
    record=True,
    take_dir=None,
    headless=True,
    cdp=None,
    quiet=False,
    repl_on_failure=False,
    repl_stdin=None,
    repl_out=print,
):
    """Run `story` in-process, in this same interpreter, so a later `probe`
    call can reuse the live browser. Returns (return_code, output_path).

    With `repl_on_failure=True`, the first keyword that fails outside any
    recovery boundary (Wait Until Keyword Succeeds/Run Keyword And .../a
    TRY block -- see _ReplOnFailureListener) pauses the run right there,
    before the failing task's own [Teardown] closes its page, and drops
    into a synchronous keyword REPL against that same live session -- see
    the class docstring for why this calls BuiltIn().run_keyword()
    in-process rather than driver.probe()'s throwaway TestSuite.

    `cdp` attaches to running browsers instead of launching one -- see
    `screencast.library.parse_cdp` for its `[NAME=]PORT|URL,...` shape."""
    # The story's own `Library` import does not pass these, and a value it
    # does pass still wins, since the import runs after this.
    configure_browser(headless=headless, cdp=cdp)
    take_dir = Path(take_dir) if take_dir else default_take_dir(story)
    take_dir.mkdir(parents=True, exist_ok=True)
    output = take_dir / "output.json"
    if task is None:
        # A full run starts from empty state, as it starts from empty
        # everything else: the take directory may be reused (`make
        # screencast` always writes to the same one), and data the previous
        # take saved must not leak into this one. A partial run (`--task`)
        # is the case state exists for: it continues from what the previous
        # run in this directory saved.
        (take_dir / STATE_FILE).unlink(missing_ok=True)

    suite = TestSuite.from_file_system(story)
    if task:
        suite.filter(included_tests=[task])
    run_kwargs = {
        "output": str(output),
        "log": None,
        "report": None,
        "loglevel": "DEBUG",
        "console": "none",
        "variable": [f"TAKE_DIR:{take_dir}", f"RECORD:{record}"],
        # Stop at the first failed task instead of running every later one
        # too -- each carries its own Wait Until Keyword Succeeds retry
        # loops (seconds to minutes), which are pointless to burn through
        # once an earlier task has already broken the story's state.
        "exitonfailure": True,
    }
    listeners = []
    if not quiet:
        listeners.append(TaskConsole())
    if repl_on_failure:
        listeners.append(_ReplOnFailureListener(stdin=repl_stdin, out=repl_out))
    if listeners:
        run_kwargs["listener"] = listeners

    result = suite.run(**run_kwargs)
    if not quiet:
        if result.return_code:
            print(summarize_failures(output))
        print(f"Take directory: {take_dir}")
    return result.return_code, output


def _format_keyword_call(data):
    args = ", ".join(str(arg) for arg in data.args)
    return f"{data.name}[{args}]" if args else data.name


class _ReplOnFailureListener:
    """Registered as an extra `run()` listener when `repl_on_failure=True`.
    Pauses on the first keyword that fails outside any recovery boundary
    and drops into a synchronous keyword REPL against the live session,
    before Robot Framework runs the failing task's own [Teardown] -- so
    `End Actor Turn` has not yet closed the page that failed.

    Keywords typed at the REPL run via BuiltIn().run_keyword(), in this
    same process, inside this same listener callback -- *not*
    driver.probe()'s throwaway TestSuite. A nested TestSuite.run() from
    inside a running suite's listener is unsafe: TestSuite.run() wraps
    its execution in `with LOGGER:` (robot.running.model), and LOGGER is
    a process-wide singleton whose __exit__ unconditionally resets it
    (`self.__init__(register_console_logger=False)`), discarding every
    listener the *outer*, still-running suite registered. Verified
    directly against the installed Robot Framework (7.5) for #16: a
    nested suite.run() from a listener callback returns normally with no
    exception, but every listener notification for the rest of the outer
    run silently stops arriving. BuiltIn().run_keyword() has none of
    that: it runs the keyword in the already-active execution context,
    correctly reaches both library keywords and the story's own resource
    keywords (RF resolves it by name against the whole active namespace,
    same as any other keyword call), and still fires the normal listener
    notifications for it -- which is also why a keyword that fails *at
    the REPL* does not recursively trigger another pause: `_paused` below
    is set before the REPL loop starts, and (deliberately) never reset
    until the next test, so a nested failure notification for the very
    keyword the REPL is running hits the same one-shot guard.
    """

    ROBOT_LISTENER_API_VERSION = 3

    def __init__(self, stdin=None, out=print):
        self._stdin = stdin if stdin is not None else sys.stdin
        self._out = out
        self._depth = 0
        self._path = []
        self._paused = False

    def start_test(self, data, result):
        self._depth = 0
        self._path = [data.name]
        self._paused = False

    def start_keyword(self, data, result):
        self._path.append(_format_keyword_call(data))
        if data.name in _RECOVERING_WRAPPER_KEYWORDS:
            self._depth += 1

    def start_try(self, data, result):
        self._depth += 1

    def end_try(self, data, result):
        self._depth -= 1
        self._maybe_pause(result)

    def end_keyword(self, data, result):
        if data.name in _RECOVERING_WRAPPER_KEYWORDS:
            self._depth -= 1
        self._maybe_pause(result)
        self._path.pop()

    def _maybe_pause(self, result):
        if self._paused:
            return
        if result.status != "FAIL" or self._depth != 0:
            return
        self._paused = True
        self._out(f"FAIL: {' > '.join(self._path)}")
        if result.message:
            self._out(f"  {result.message}")
        for artifact in Screencast.ROBOT_LIBRARY_LISTENER._pending_dump_paths or []:
            self._out(f"  artifact: {artifact}")
        self._out(
            "repl-on-failure: one keyword per line against the live session "
            "(Keyword Name<tab or 4 spaces>arg1<tab or 4 spaces>arg2), "
            "Ctrl-D/EOF to stop and let teardown run."
        )
        self._loop()

    def _loop(self):
        for line in self._stdin:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t") if "\t" in line else line.split("    ")
            parts = [part.strip() for part in parts if part.strip()]
            name, args = parts[0], parts[1:]
            try:
                BuiltIn().run_keyword(name, *args)
                self._out("OK")
            except Exception as error:  # noqa: BLE001 -- report, keep looping
                self._out(f"FAIL: {error}")


def summarize_failures(output_path):
    """A compact keyword-path failure summary from `output`: the keyword
    path (Task > Setup[args] > Human Click[args]), its message, the
    DEBUG-level Python traceback, and any failure-artifact paths the
    library's listener logged -- everything screencast.library writes for
    exactly this purpose."""
    result = ExecutionResult(str(output_path))
    lines = []
    for test in result.suite.all_tests:
        if test.status != "FAIL":
            continue
        lines.append(f"FAIL: {test.full_name}")
        for part in (test.setup, *test.body, test.teardown):
            if part is not None:
                _summarize_keyword(part, [test.name], lines)
    return "\n".join(lines) if lines else "All tasks passed."


def _summarize_keyword(item, path, lines):
    if not isinstance(item, ResultKeyword) or item.status != "FAIL":
        return
    if item.name is None:
        return  # a synthetic body wrapper (e.g. an invalid-test placeholder)
    args = ", ".join(str(arg) for arg in item.args)
    label = f"{item.name}[{args}]" if args else item.name
    path = [*path, label]
    failed_children = [
        child
        for child in item.body
        if isinstance(child, ResultKeyword) and child.status == "FAIL"
    ]
    if failed_children:
        for child in failed_children:
            _summarize_keyword(child, path, lines)
        return
    # This is the leaf: the keyword that actually failed.
    lines.append("  " + " > ".join(path))
    if item.message:
        lines.append(f"    {item.message}")
    for message in item.body:
        if not isinstance(message, ResultMessage):
            continue
        if message.level == "DEBUG" or "Failure artifacts" in (message.message or ""):
            lines.append(f"    {message.message}")


def probe(
    resource, keyword, args=(), take_dir=".", record=False, headless=True, cdp=None
):
    """Run one keyword against the live session -- the browser started by a
    previous `run`/`probe` call in this process survives, per
    screencast.library's module-level session state. Builds a throwaway
    TestSuite instead of parsing a story file, per the driver's own
    research: BuiltIn().run_keyword() outside a run raises
    RobotNotRunningError, so even one keyword needs a (tiny) suite run."""
    take_dir = Path(take_dir)
    take_dir.mkdir(parents=True, exist_ok=True)
    suite = TestSuite(name="Probe")
    library_args = [f"take_dir={take_dir}", f"record={record}", f"headless={headless}"]
    if cdp:
        library_args.append(f"cdp={cdp}")
    suite.resource.imports.library("screencast.Screencast", args=tuple(library_args))
    if resource:
        # This suite is built in memory (not TestSuite.from_file_system), so
        # it has no source file for Robot to resolve a relative import
        # against -- resolve it against the process's own CWD ourselves, the
        # same base a user typing a relative --resource path expects.
        suite.resource.imports.resource(str(Path(resource).resolve()))
    task = suite.tests.create(name="Probe")
    task.body.create_keyword(name=keyword, args=tuple(args))
    output = take_dir / "probe-output.json"
    result = suite.run(
        output=str(output), log=None, report=None, loglevel="DEBUG", console="none"
    )
    print(summarize_failures(output))
    return result.return_code


def repl(resource, take_dir=".", record=False, headless=True, cdp=None):
    """Interactive mode: read one keyword call per line from stdin (space
    separated: `Keyword Name    arg1    arg2`), run it against the live
    session, print its status, and keep going -- Ctrl-D / an empty line to
    stop. The browser survives between lines, same as `probe`."""
    print("screencast repl -- one keyword per line, Ctrl-D to stop")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t") if "\t" in line else line.split("    ")
        parts = [part.strip() for part in parts if part.strip()]
        name, args = parts[0], parts[1:]
        code = probe(
            resource,
            name,
            args,
            take_dir=take_dir,
            record=record,
            headless=headless,
            cdp=cdp,
        )
        print("OK" if code == 0 else "FAIL")


def keywords(resource):
    """List a resource file's own keywords with their arguments and source
    line, via ResourceFileBuilder -- what an agent checks before writing or
    fixing a story, instead of guessing what a project resource exposes."""
    built = ResourceFileBuilder().build(Path(resource))
    lines = []
    for keyword in built.keywords:
        args = ", ".join(str(arg) for arg in keyword.args)
        lines.append(f"{keyword.name}({args})  -- {resource}:{keyword.lineno}")
        if keyword.doc:
            lines.append(f"    {keyword.doc.splitlines()[0]}")
    return "\n".join(lines)


def check(story, take_dir=None):
    """Check `story` with Robot Framework's own `--dryrun`: it parses,
    resolves every keyword against the libraries/resources actually
    imported, and validates arguments -- without opening a browser, since
    dry-run skips real keyword bodies. Cheaper than a real run when the
    browser was never the problem. Returns a list of "task: message"
    strings; empty means it checked out clean."""
    if take_dir is None:
        # Nothing here is worth keeping: a dry run has no recording, and a
        # timestamped directory in the current directory per check just
        # litters the repository.
        with tempfile.TemporaryDirectory(prefix="screencast-check-") as scratch:
            return check(story, take_dir=scratch)
    take_dir = Path(take_dir)
    take_dir.mkdir(parents=True, exist_ok=True)
    output = take_dir / "check-output.json"
    suite = TestSuite.from_file_system(str(story))
    suite.run(
        dryrun=True,
        output=str(output),
        log=None,
        report=None,
        console="none",
        variable=["TAKE_DIR:.", "RECORD:False"],
    )
    result = ExecutionResult(str(output))
    return [
        f"{test.full_name}: {test.message}"
        for test in result.suite.all_tests
        if test.status == "FAIL"
    ]


def render_log(take_dir):
    """Render log.html from a take directory's output.json on demand, for
    humans -- `run` itself never writes log.html, to keep the fast loop
    fast."""
    from robot.api import ResultWriter

    take_dir = Path(take_dir)
    output = take_dir / "output.json"
    log = take_dir / "log.html"
    ResultWriter(str(output)).write_results(report=None, log=str(log))
    return log
