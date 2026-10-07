# robotframework-screencast

Record, compose and verify **multi-actor browser screencasts** from Robot
Framework stories. A story is a `*** Tasks ***` suite that reads like a
screenplay: several personas take turns in their own browser, an observer
watches the whole time, and the result is one composed video, with title
cards and picture-in-picture, that is checked automatically for dead air,
blank frames and a wrong duration.

```robotframework
*** Settings ***
Library    screencast.Screencast    take_dir=${TAKE_DIR}    record=${RECORD}

*** Tasks ***
Watch The Dashboard
    Start Observer    dashboard    https://example.org/dashboard

Alice Adds A Page
    [Setup]    Start Actor Turn    alice    eyebrow=Demo · 1 / 1
    ...    title=Alice    subtitle=Adding a page
    Go To    https://example.org/
    Human Click    role=link[name="Add new…"]
    [Teardown]    End Actor Turn

Wrap Up
    End Observer
```

```sh
screencast run story.robot          # record: one clip per turn, one for the observer
screencast compose <take dir>       # compose them into output.webm
screencast verify <take dir>        # ffprobe, blank frames, dead air, contact sheet
```

**Documentation:** <https://datakurre.github.io/robotframework-screencast/>

## What is in it

Three layers, and only the first is in this repository:

| Layer | What | Where |
|---|---|---|
| **Engine** | A Robot Framework keyword library over sync Playwright (human-paced input, a visible cursor, one recorded context per turn), a versioned timeline (edit-decision list) schema, a declarative composer, a take verifier, and an agent-facing driver | `src/screencast/` |
| **Project keywords** | A `.resource` file that turns your app's UI into keywords: `Log In`, `Add Content`, `Wait For Task` | Your project |
| **Stories** | The `*** Tasks ***` suites | Your project |

`screencast --version` prints the resolved versions of Robot Framework,
Playwright, jsonschema and ffmpeg; paste it into bug reports.

### Commands

| Command | |
|---|---|
| `run story.robot [--task NAME] [--no-record] [--take DIR]` | Run a story in-process and write `timeline.json` and the clips. A full run starts from empty state; `--take <dir> --task NAME` re-runs one task from the state the previous run saved (`Save State` / `Load State`, kept in `<take dir>/state.json`). On failure it prints the keyword path, the message, the traceback and the failure screenshots. `--repl-on-failure` pauses before the failing turn's teardown and drops into a REPL against the still-open page |
| `probe KEYWORD args... [--resource FILE]` | Run one keyword against the live browser session |
| `keywords FILE` | List a resource file's keywords with arguments and docs |
| `check story.robot` | Dry-run a story (no browser) |
| `compose DIR` | Compose a take into `output.webm` (and `output.vtt` when the story has captions) |
| `verify DIR` | Check a composed take and write `report.json` and a contact sheet |
| `log DIR` | Render Robot Framework's `log.html` for a take |

### Dead air

The library records a `wait` event around every waiting keyword (`Sleep`,
`Wait Until Keyword Succeeds`, its own waits, and any keyword you tag
`screencast:wait`). `verify` fails a take with a single wait over 10 seconds
and warns when all waits together pass 30. Tag your own polling keywords:

```robotframework
Wait For Task
    [Tags]    screencast:wait
    Wait Until Keyword Succeeds    20x    3s    Task Is Visible
```

## Requirements

- Python 3.10 or newer, and `ffmpeg`/`ffprobe` on the `PATH`.
- **Robot Framework 7.4 or newer.** 7.4.2 is the oldest release the tests
  cover, next to the latest; anything newer that the engine can use only when
  present (`robot.api.console`, from 7.5) is imported behind a guard.
- Playwright with a Chromium it can launch. It never runs `playwright install`
  itself; provide the browser the way your environment does (a Nix
  `playwright-driver.browsers`, or `playwright install chromium` yourself). The
  `SCREENCAST_CHROMIUM_PATH` environment variable points it at a specific
  Chromium build. Alternatively, `--cdp` (or `SCREENCAST_CDP`) attaches it to
  a Chromium that is already running — a visible one on the host of a
  container, say — with one browser per persona if you like; see
  [Getting started](https://datakurre.github.io/robotframework-screencast/getting-started/).

Not published to PyPI, and no release is planned for now. Install from git:

```sh
pip install "git+https://github.com/datakurre/robotframework-screencast"
```

### With Nix

The repository is a flake. Its package brings `ffmpeg`, a matching Chromium and
fonts, so nothing else needs installing:

```sh
nix run github:datakurre/robotframework-screencast -- --version
nix run github:datakurre/robotframework-screencast -- run story.robot --take out
nix shell github:datakurre/robotframework-screencast --command screencast verify out
```

Pin it with a tag or commit (`github:datakurre/robotframework-screencast/<rev>`).
In another flake, add it as an input and use its overlay:
`nixpkgs.overlays = [ robotframework-screencast.overlays.default ]` gives
`pkgs.robotframework-screencast`. To work on it:

```sh
nix develop --command pytest                 # the engine from the working tree
nix develop --command mkdocs build --strict  # the docs
nix flake check -L                           # lint and every test; the video tests take minutes
```

A story that imports further Python packages of its own needs an environment
with them (a virtualenv with the pip install above); the Nix package carries
only the engine's own dependencies.

## An example use case

The engine was written for a Plone and Operaton (BPMN) project,
collective.bpmproxy, and that project's playground lives on in this repository
as the [`legacy-playground`](https://github.com/datakurre/robotframework-screencast/tree/legacy-playground)
branch. Its `scripts/screencasts/` holds three complete stories (a review
process, a contact form and a renovation project, each with several personas and
Cockpit as the observer) and the project keyword layer
(`resources/bpmproxy.resource`) that they share, and its `docs/*-scenario.md`
walk through each story. That branch is the reference example of the two upper
layers.

## The agent skill

`skills/screencast/` is an agent skill (a `SKILL.md` and a reference) that teaches
an agent to write, run, debug and verify stories with this library. It is
self-contained: its first section says how to install the library from GitHub
(the package is not on PyPI), where the documentation and a worked example are,
and which environment it needs, so it works in any project, not only in a
checkout of this repository. Copy the directory into your agent's skills
directory:

```sh
git clone --depth 1 https://github.com/datakurre/robotframework-screencast /tmp/robotframework-screencast
cp -r /tmp/robotframework-screencast/skills/screencast <your project>/.agents/skills/
# or .claude/skills/, or wherever your agent looks for skills
```

## Development

```sh
pip install -e ".[test]"
pytest
```

No live browser or application stack is needed: `tests/fakes.py` stands in for
Playwright, and the composer and verifier tests run against real ffmpeg on tiny
synthetic clips. CI runs them on Robot Framework 7.4.2 and the latest release.

## Provenance

`main`'s history is the engine's own: it was extracted from the repository's
former default branch (now `legacy-playground`) with `git filter-repo`, keeping
only the commits that touched the engine (`scripts/screencast/`, moved to
`src/screencast/`) and its agent skill (`skills/screencast/`). Commit
messages and code comments that refer to `#N` issues mean issues of this
repository, and some comments use the Plone and Operaton scenario as
illustration; the code itself imports and assumes nothing from it.

### Open work

- Some comments and tests still use that scenario as illustration.
- The `legacy-playground` branch keeps its own copy of the engine under
  `scripts/screencast/`; it switches to this package only if one is ever
  published (no PyPI release is planned for now).

## License

GPL version 2 (`GPL-2.0-only`), like the repository the engine was written in. See `LICENSE`.
