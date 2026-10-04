"""`python -m screencast compose DIR/` -- turn a take's timeline.json into
one ffmpeg filter_complex, replacing the three copies of
`compose_recording()` and scripts/scenarios/cut_*.py.

## Declarative picture-in-picture policy

The output is a chronological concatenation of segments, cut at every
turn_start/turn_end/focus/chapter/hold event boundary:

- Before any actor turn has started, the observer is the only view (no
  inset -- there is nothing to inset yet).
- During an actor turn, that actor is the main view and the observer is
  the inset, unless a `focus` event inside the turn's own [start, end)
  window says otherwise.
- Between turns, the main view follows the most recent `focus` event
  (default: observer), with the most recently active actor's clip as the
  inset -- live footage if their turn is still technically open (main
  flipped to observer by a `focus` event without a matching turn_end yet),
  otherwise their clip's last frame, frozen. A gap `focus` event can also
  bring that actor back as the *main* view instead (e.g. a story
  deliberately ending on their last frame, via `Focus actor` left in
  effect through `End Actor Turn`) -- past `turn_end` there is no more
  live footage for them either way, so this is their last frame, frozen,
  same as the inset case just describes.
- A `chapter` event inserts an independent title-card segment (rendered
  once, cached) ahead of the live segment starting at the same instant.
  Title cards add output time; they do not consume any recorded footage,
  so the live segment right after one resumes at the exact same source
  timestamp the chapter event fired at.
- A `hold` event inserts a frozen segment -- both main and inset held at
  their frame at that instant -- for its `duration`, before continuing.
- A `caption` event doesn't affect the video at all; if there is at least
  one, `compose()` writes a `output.vtt` WebVTT sidecar alongside the
  output, with each cue's start mapped from its raw (observer-clock) time
  to its actual position in the output (see `_output_time()`).

Inset scale/margin/border come from the focus event in effect (defaults:
0.4, 24px margin, 3px border, from the timeline schema).

## External PiP tracks (`tracks=`/`--track`)

`compose()` takes an optional `tracks` list -- each a secondary recording the
engine itself never captured (a `ttyd`-recorded terminal, say), composited as
its own corner inset on top of everything the main per-boundary loop already
produced. This is deliberately *not* part of `timeline.json`: the engine
never manages recording a non-browser source, only (optionally) compositing
it afterward, so a track is passed to `compose()`/`--track` at compose time,
never stored.

`offset` uses the exact same clock and sign convention as `actors[].offset`
already does: the observer-clock instant at which the track's own t=0
occurred (`rel_start = segment_start - offset`, same formula `turn_slice()`
uses for an actor clip). An actor's own offset is always positive -- a turn
can't start before `Start Observer` opens the context every turn is cut
against. A track has no such constraint: the common case (an ambient
terminal recording started moments *before* kicking off the whole take) has
its own t=0 *before* `Start Observer`, which is a **negative** offset --
e.g. a terminal recording that had already been running for 23.7s once
`Start Observer` fires is `offset=-23.7`, not `+23.7`.

The hook point is narrow and deliberate: only the main per-boundary loop's
own segment label (the `label` the `if view == "actor": ... else: ...` block
above builds) gets a track composited onto it. `emit_chapters_at()`'s and
`emit_holds_at()`'s own segments are appended to `segment_labels` directly,
never passing through that `label` -- so a track is automatically absent
behind a title card or a synthetic hold freeze, with no `enable=` expression
needed at all. Track visibility is resolved once per `[start, end)` boundary
slice (in the timeline's raw observer-clock, the same clock a track's own
`offset` is given in), not frame-by-frame -- which is exact, because every
track's own start and end are themselves boundaries, so no segment ever
straddles either.

A track's own footage relative to one segment is one of three cases (see
`track_slice()`):

- it fully covers the segment: a plain trim, like `turn_slice()`.
- it runs out partway through the segment, or has already finished before
  the segment even starts: trim whatever is left, then freeze its last
  frame (`tpad=stop_mode=clone`) for the remainder, like `frozen_turn_slice()`
  -- chapters add real output duration with no corresponding external
  footage, so a track recorded only for the live portions of a take runs out
  long before a multi-chapter composed output does.
- it has not started yet when the segment begins (its `offset` is still
  ahead of the segment's own start): nothing to show. This composes nothing
  for the track in that segment at all (as opposed to freezing a first
  frame) -- chosen because "not recorded yet" reads more honestly than a
  frozen frame implying something is already there.

Multiple tracks compose independently, each layered onto the output of the
previous one in the order given (track 1 onto main+inset, track 2 onto that,
...) -- order only matters if corners overlap, which a sane set of tracks
won't do.

## Rules carried over from the three e2e_*.py composers (see docs/AGENTS.md)

- Never use `overlay=...:shortest=1` -- it truncates the output at the
  shorter input.
- Use `tpad=stop_mode=clone` to freeze a frame, not a still-image input.
- Trim clips using the timeline's own measured offsets, never an assumed
  constant trim.
- Encode with `-v error -nostats`, VP9 at 1920x1080 25fps, audio-free.
"""

from pathlib import Path
from screencast.timeline import OVERLAP_TOLERANCE
from screencast.timeline import Timeline
import subprocess
import warnings


DEFAULT_SCALE = 0.4
DEFAULT_MARGIN = 24
DEFAULT_BORDER = 3
DEFAULT_BORDER_COLOR = "0x1f2937"
DEFAULT_CAPTION_DURATION = 4.0
FPS = 25
VIDEO_SIZE = "1920x1080"


def ffmpeg(*args, capture=True):
    return subprocess.run(
        ["ffmpeg", *[str(arg) for arg in args]],
        check=True,
        capture_output=capture,
        text=True,
    )


def ffprobe_duration(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def _escape_drawtext(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def make_title_card(path, eyebrow, title, subtitle, duration):
    """Render an independent title-card clip. Cached: a second compose of
    the same take with the same chapter text does not re-render it."""
    path = Path(path)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    filters = (
        "drawtext=font='DejaVu Sans':"
        f"text='{_escape_drawtext(eyebrow)}':fontcolor=#7dd3fc:fontsize=40:x=160:y=330,"
        "drawtext=font='DejaVu Sans':"
        f"text='{_escape_drawtext(title)}':fontcolor=white:fontsize=76:x=160:y=420,"
        "drawtext=font='DejaVu Sans':"
        f"text='{_escape_drawtext(subtitle)}':fontcolor=#cbd5e1:fontsize=40:x=160:y=560,"
        # Countdown bar: full-width at t=0, shrinking to zero width by
        # t=duration, so the card visibly counts down its own on-screen
        # time. drawbox's x/y/w/h expressions are only ever evaluated once,
        # at filter init -- unlike drawtext, it has no `eval=frame` option
        # to force per-frame re-evaluation (confirmed against this ffmpeg
        # build: passing `eval=frame` to drawbox is a hard "Option not
        # found" error, and leaving it off renders a bar frozen at its t=0
        # width for the whole clip). geq, by contrast, evaluates its pixel
        # expressions per frame by design (its whole purpose), so it is
        # used here instead for the same visual effect: every pixel in the
        # bottom 8px strip is recolored sky-blue only while its X coordinate
        # is left of the shrinking threshold `W*(1-T/duration)`, and left
        # untouched (passed through via r(X,Y)/g(X,Y)/b(X,Y)) elsewhere.
        "geq="
        f"r='if(gte(Y,H-8)*lt(X,(W)*(1-T/{float(duration):.3f})),125,r(X,Y))':"
        f"g='if(gte(Y,H-8)*lt(X,(W)*(1-T/{float(duration):.3f})),211,g(X,Y))':"
        f"b='if(gte(Y,H-8)*lt(X,(W)*(1-T/{float(duration):.3f})),252,b(X,Y))'"
    )
    ffmpeg(
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=#0f172a:s={VIDEO_SIZE}:d={float(duration):.3f}",
        "-vf",
        filters,
        "-r",
        FPS,
        "-c:v",
        "libvpx-vp9",
        "-deadline",
        "good",
        "-b:v",
        "0",
        "-crf",
        "32",
        "-an",
        path,
        capture=False,
    )
    return path


class ComposeError(ValueError):
    pass


def _build_track_def(name, path, offset, duration, input_index, focusable, source):
    """Build one `track_defs` entry, shared by compose()'s two track
    sources (`timeline.tracks` and the external `tracks=`/`--track`
    parameter) -- they differ only in `path` resolution, the `name`
    fallback, and whether `focusable` ever varies (engine tracks honor
    their own recorded flag; an external track is never focusable), never
    in the inset-styling defaults below."""
    return {
        "name": name,
        "path": path,
        "offset": float(offset),
        "duration": duration,
        "corner": source.get("corner", "bottom-left"),
        "scale": source.get("scale", DEFAULT_SCALE),
        "margin": source.get("margin", DEFAULT_MARGIN),
        "border": source.get("border", DEFAULT_BORDER),
        "focusable": focusable,
        "fade": source.get("fade", False),
        "input_index": input_index,
    }


def _turn_window(t, turns, tolerance=1e-6):
    """(active_turn, most_recent_turn), each `(turn_id, actor, start, end)`
    or None -- the turn covering `t` right now, and the most recent one
    that had started by `t` (possibly the same one). Shared by `_view_at`
    (to scope which focus events apply) and by the composer's own
    per-segment loop (to know whether an actor PiP is available at all,
    independent of what the current focus happens to be)."""
    active_turn = None
    most_recent_turn = None
    for turn_id, actor, start, end in turns:
        if start - tolerance <= t < end + tolerance:
            active_turn = (turn_id, actor, start, end)
        if start <= t + tolerance:
            most_recent_turn = (turn_id, actor, start, end)
    return active_turn, most_recent_turn


def _view_at(
    t, focus_events, turns, track_names=frozenset(), track_windows=None, tolerance=1e-6
):
    """Resolve (kind, turn_id_or_None, scale, margin, border, solo) in
    effect at time `t`. `turns` is a list of (turn_id, actor, start, end)
    sorted by start -- keyed by `turn_id` (a clip, i.e. one turn), not by
    `actor` name, since one actor can play more than one turn (e.g.
    "reception" in contact_form.robot has three) and two turns never share
    a clip.

    `kind` is `"actor"`, `"observer"`, or `f"track:{name}"` for a name in
    `track_names` (a track opened with `Screencast.start_track()` -- see
    its own docstring) -- the segment loop renders that track full-frame as
    the main view instead of the observer or the active actor. A focus
    event naming anything else (an unrecorded, opted-out, or -- see
    `track_windows` below -- not-yet-available/already-finished track)
    falls through to the ordinary actor/observer resolution below, the same
    safety net `view == "actor"` with no active turn already relies on.

    `track_windows`, when given, maps a name in `track_names` to its own
    `(offset, duration)` -- the same window `track_slice()` trims against.
    A name only resolves to `f"track:{name}"` when `t` actually falls
    inside that window (`offset <= t < offset + duration`); a focus event
    naming a track that has not started yet, or has already run out, falls
    through to ordinary resolution instead, exactly as the timeline
    schema's own `focusEvent.view` description promises ("the composer
    falls back to 'observer' ... not yet available at this point in the
    take") -- matching the per-segment inset loop's own `start <
    track["offset"]` guard, which already skips an unavailable track's
    inset for the same reason. `None` (the default, and what every caller
    that only cares about name resolution -- e.g. the pure `_view_at` unit
    tests -- passes) skips this check entirely: a name in `track_names`
    always resolves, as if permanently available.

    `solo` (a focus event's own optional `solo`, default False) tells the
    segment loop to render only `kind`, full-frame, with no inset of any
    kind -- not the usual actor/observer cross-inset, and not any track's
    own always-present corner inset either. For a take whose last stretch
    is meant to end on the main view alone (e.g. mirroring a trailing
    `Hold`, which already never renders an inset -- see `emit_holds_at()`),
    this is the only way to turn insets off again once something (a turn, a
    track) has made one available; nothing else in the timeline vocabulary
    can undo that."""
    active_turn, most_recent_turn = _turn_window(t, turns, tolerance)
    inset_turn_id = (active_turn or most_recent_turn or (None,))[0]

    # The most recent focus event at or before `t`, scoped to the active
    # turn's own window when inside one (a focus event from a previous
    # turn or gap does not leak into a later turn's default).
    scope_start = active_turn[2] if active_turn else 0.0
    latest_focus = None
    for event in focus_events:
        if event["time"] <= t + tolerance and event["time"] >= scope_start - tolerance:
            latest_focus = event

    if latest_focus is not None:
        requested = latest_focus["view"]
        if requested not in ("actor", "observer") and requested in track_names:
            window = track_windows.get(requested) if track_windows else None
            available = window is None or (
                window[0] - tolerance <= t < window[0] + window[1] + tolerance
            )
            if available:
                return (
                    f"track:{requested}",
                    inset_turn_id,
                    latest_focus.get("scale", DEFAULT_SCALE),
                    latest_focus.get("margin", DEFAULT_MARGIN),
                    latest_focus.get("border", DEFAULT_BORDER),
                    bool(latest_focus.get("solo", False)),
                )

    if active_turn:
        turn_id = active_turn[0]
        if latest_focus is None:
            return (
                "actor",
                turn_id,
                DEFAULT_SCALE,
                DEFAULT_MARGIN,
                DEFAULT_BORDER,
                False,
            )
        if latest_focus["view"] == "actor":
            return (
                "actor",
                turn_id,
                latest_focus.get("scale", DEFAULT_SCALE),
                latest_focus.get("margin", DEFAULT_MARGIN),
                latest_focus.get("border", DEFAULT_BORDER),
                bool(latest_focus.get("solo", False)),
            )
        return (
            "observer",
            most_recent_turn[0] if most_recent_turn else None,
            latest_focus.get("scale", DEFAULT_SCALE),
            latest_focus.get("margin", DEFAULT_MARGIN),
            latest_focus.get("border", DEFAULT_BORDER),
            bool(latest_focus.get("solo", False)),
        )

    # A gap: no active turn.
    inset_turn_id = most_recent_turn[0] if most_recent_turn else None
    if latest_focus is None:
        return (
            "observer",
            inset_turn_id,
            DEFAULT_SCALE,
            DEFAULT_MARGIN,
            DEFAULT_BORDER,
            False,
        )
    view = "actor" if latest_focus["view"] == "actor" else "observer"
    if view == "actor" and inset_turn_id is None:
        view = "observer"  # nothing to focus on yet
    return (
        view,
        inset_turn_id,
        latest_focus.get("scale", DEFAULT_SCALE),
        latest_focus.get("margin", DEFAULT_MARGIN),
        latest_focus.get("border", DEFAULT_BORDER),
        bool(latest_focus.get("solo", False)),
    )


def _inserted_holds(hold_events, shift):
    """The holds the composer actually inserts a synthetic freeze for: not a
    "recorded" one (that time is already real observer footage; see
    predicted_duration()'s own docstring), and not one inside the lead-in
    `_leading_shift()` drops -- its boundary is dropped with the rest of the
    lead-in, so no segment is ever emitted for it."""
    return [
        e for e in hold_events if not e.get("recorded") and e["time"] >= shift - 1e-6
    ]


def _output_time(raw_time, chapter_events, hold_events, shift=0.0):
    """Map `raw_time` (observer-clock, the same clock every timeline event
    uses) to its position in the composed output: `raw_time` less the
    dropped lead-in `shift` (see `_leading_shift()`; a time inside the
    lead-in maps to 0.0, where the output starts), plus the duration of
    every chapter/hold segment the composer actually inserts at or before
    it (title cards, and the holds `_inserted_holds()` keeps)."""
    inserted = sum(e["duration"] for e in chapter_events if e["time"] <= raw_time)
    inserted += sum(
        e["duration"]
        for e in _inserted_holds(hold_events, shift)
        if e["time"] <= raw_time
    )
    return max(0.0, raw_time - shift) + inserted


def _leading_shift(chapter_events, turn_starts):
    """Seconds to shift every timestamp back by when the take's first
    chapter fires at or before its own turn starts -- i.e. nothing of
    value plays in the raw observer footage ahead of it (Start Observer's
    own load, the first turn's context creation and navigation), so
    compose() drops that lead-in and opens directly on the card instead.
    Shared by compose()'s own boundary handling and chapter_windows() so
    the two can never drift apart on this edge case."""
    if not chapter_events:
        return 0.0
    first_chapter_time = min(e["time"] for e in chapter_events)
    first_turn_start = min(turn_starts, default=float("inf"))
    if 0.0 < first_chapter_time <= first_turn_start:
        return first_chapter_time
    return 0.0


def chapter_windows(timeline):
    """Each chapter event's own `(start, end)` span in seconds within
    compose()'s *output* -- not the raw observer-clock `time` timeline.json
    itself stores -- accounting for every earlier chapter's inserted
    duration and the frame-0 lead-in shift from `_leading_shift()`.

    For anything compositing a secondary element onto an already-composed
    output that needs to avoid a title card's span -- e.g. hiding an
    externally recorded PiP track (a terminal, say) while a card is on
    screen, the way a project might one-off `ffmpeg overlay` a recording
    the engine itself never knew about on top of `compose()`'s own output.
    See docs/extending.md."""
    chapter_events = sorted(timeline.events_of("chapter"), key=lambda e: e["time"])
    if not chapter_events:
        return []
    turn_starts = [e["time"] for e in timeline.events_of("turn_start")]
    shift = _leading_shift(chapter_events, turn_starts)
    # compose() emits every chapter at an instant before any hold at that
    # same instant, so only a hold strictly earlier pushes a card back.
    holds = _inserted_holds(timeline.events_of("hold"), shift)
    windows = []
    inserted_chapters = 0.0
    for chapter in chapter_events:
        inserted_holds = sum(
            e["duration"] for e in holds if e["time"] < chapter["time"] - 1e-6
        )
        start = (chapter["time"] - shift) + inserted_chapters + inserted_holds
        duration = float(chapter["duration"])
        windows.append((start, start + duration))
        inserted_chapters += duration
    return windows


def _format_vtt_timestamp(seconds):
    seconds = max(0.0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, remainder = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{remainder:06.3f}"


def write_captions_vtt(
    vtt_path, caption_events, chapter_events, hold_events, shift=0.0
):
    """Write a WebVTT sidecar for `caption_events` (schema v2's optional
    "caption" event type) at `vtt_path`, mapping each one's raw time to
    its actual position in the composed output via `_output_time()` --
    `shift` is the lead-in compose() drops (see `_leading_shift()`).
    `duration` shifts the cue's start the same way but is not itself
    stretched by an insertion that happens to fall *inside* it -- a
    caption is expected to describe one continuous stretch of real
    footage, not straddle a title card or hold."""
    lines = ["WEBVTT", ""]
    for index, event in enumerate(
        sorted(caption_events, key=lambda e: e["time"]), start=1
    ):
        start = _output_time(event["time"], chapter_events, hold_events, shift)
        end = start + float(event.get("duration", DEFAULT_CAPTION_DURATION))
        lines.append(str(index))
        lines.append(f"{_format_vtt_timestamp(start)} --> {_format_vtt_timestamp(end)}")
        lines.append(event["text"])
        lines.append("")
    vtt_path = Path(vtt_path)
    vtt_path.write_text("\n".join(lines))
    return vtt_path


def compose(take_dir, output=None, tracks=None):
    take_dir = Path(take_dir)
    timeline = Timeline.load(take_dir / "timeline.json")

    observer_video = take_dir / timeline.observer["video"]
    observer_duration = ffprobe_duration(observer_video)

    # `timeline.actors` has one clip per turn (not per actor -- an actor
    # with several turns, e.g. "reception" in contact_form.robot, gets one
    # clip per turn), appended in occurrence order by end_actor_turn right
    # after it appends that turn's own turn_end event; turn_start events are
    # appended in the same occurrence order when the turn opens. Turns never
    # overlap (Start/End Actor Turn's one-at-a-time contract), so the Nth
    # turn_start pairs with the Nth turn_end and the Nth actor clip -- use
    # that shared index as `turn_id` instead of grouping by actor name,
    # which would collapse an actor's earlier turns into their last one.
    starts = timeline.events_of("turn_start")
    ends = timeline.events_of("turn_end")
    if len(starts) != len(timeline.actors) or len(ends) != len(timeline.actors):
        raise ComposeError(
            f"turn_start ({len(starts)}), turn_end ({len(ends)}), and actor "
            f"clip ({len(timeline.actors)}) counts disagree -- was this take "
            "recorded with record=True for every turn?"
        )

    clips = []
    turns = []
    for turn_id, clip in enumerate(timeline.actors):
        path = take_dir / clip["video"]
        duration = ffprobe_duration(path)
        clips.append(
            {
                "actor": clip["actor"],
                "path": path,
                "offset": clip["offset"],
                "duration": duration,
            }
        )
        turns.append(
            (turn_id, clip["actor"], starts[turn_id]["time"], ends[turn_id]["time"])
        )
    turns.sort(key=lambda t: t[2])

    # Guard against real overlaps (see screencast.timeline.OVERLAP_TOLERANCE):
    # two turns whose windows genuinely overlap by more than encoder-startup
    # noise mean two persona contexts were open at once, contradicting
    # Start Actor Turn/End Actor Turn's one-at-a-time contract.
    for (turn_a, actor_a, _, end_a), (turn_b, actor_b, start_b, _) in zip(
        turns, turns[1:], strict=False
    ):
        if start_b < end_a - OVERLAP_TOLERANCE:
            raise ComposeError(
                f"Turn {turn_a} ({actor_a!r}) overlaps turn {turn_b} "
                f"({actor_b!r}) by {end_a - start_b:.2f}s -- Start/End Actor "
                "Turn should never leave two contexts open at once."
            )

    def clamp(t):
        # time.monotonic() event timestamps and ffprobe's measured observer
        # duration disagree by a small amount (encoder flush latency, not an
        # ordering bug) -- clamp instead of dropping an event that lands a
        # few milliseconds past the observer's own measured length, or a
        # trailing hold/focus/turn_end right before End Observer silently
        # never renders.
        return min(t, observer_duration)

    focus_events = [dict(e, time=clamp(e["time"])) for e in timeline.events_of("focus")]
    chapter_events = sorted(
        (dict(e, time=clamp(e["time"])) for e in timeline.events_of("chapter")),
        key=lambda e: e["time"],
    )
    hold_events = sorted(
        (dict(e, time=clamp(e["time"])) for e in timeline.events_of("hold")),
        key=lambda e: e["time"],
    )
    caption_events = sorted(
        (dict(e, time=clamp(e["time"])) for e in timeline.events_of("caption")),
        key=lambda e: e["time"],
    )
    turns = [
        (turn_id, actor, clamp(start), clamp(end))
        for turn_id, actor, start, end in turns
    ]
    turns_by_id = {turn_id: (start, end) for turn_id, _actor, start, end in turns}

    # A turn's chapter event is timestamped when its first `Go To` finishes
    # loading, not when `Start Actor Turn` opens the context (see
    # library.py's start_actor_turn()/_record_turn_start()) -- so there is a
    # real stretch of raw observer footage at the very start of a take,
    # before the first chapter card, covering Start Observer's own load plus
    # the first turn's context creation and navigation. When the take's
    # first chapter fires at or before every turn's own start, the segment
    # loop drops every boundary ahead of it (see below), so the output opens
    # directly on that card -- chapters are pure insertions ahead of the
    # live segment (see the module docstring), so this skips the unneeded
    # raw rendering before it without skipping anything that was recorded.
    shift = _leading_shift(chapter_events, [t[2] for t in turns])

    output = Path(output) if output else take_dir / "output.webm"
    title_dir = take_dir / "titles"

    filters = []
    segment_labels = []
    inputs = ["-i", str(observer_video)]
    input_index_by_turn = {}
    for turn_id, clip in enumerate(clips):
        input_index_by_turn[turn_id] = len(inputs) // 2
        inputs += ["-i", str(clip["path"])]

    # Tracks -- added as inputs right after the actor clips, before any
    # title card input is added lazily below, so their input indices never
    # shift once fixed here. Two sources, merged into one `track_defs` list:
    # engine-recorded tracks from the timeline itself (`Screencast.
    # start_track()`/`end_track()` -- see their own docstrings), always
    # `focusable` (eligible to become the segment loop's main view, not
    # just an always-present inset); and the external `tracks=`/`--track`
    # parameter below (see the module docstring's "External PiP tracks"
    # section) for a recording the engine itself never captured -- always a
    # fixed-corner inset, never focusable, exactly as before this existed.
    track_defs = []
    for track in timeline.tracks:
        path = take_dir / track["video"]
        duration = ffprobe_duration(path)
        input_index = len(inputs) // 2
        inputs += ["-i", str(path)]
        track_defs.append(
            _build_track_def(
                track["name"],
                path,
                track["offset"],
                duration,
                input_index,
                track.get("focusable", True),
                track,
            )
        )
    for track in tracks or []:
        path = Path(track["video"])
        duration = ffprobe_duration(path)
        input_index = len(inputs) // 2
        inputs += ["-i", str(path)]
        track_defs.append(
            _build_track_def(
                track.get("name", path.stem),
                path,
                track["offset"],
                duration,
                input_index,
                False,
                track,
            )
        )

    # An engine-recorded track (Start Track/End Track) and an external
    # tracks=/--track entry drawing from two different namespaces would
    # otherwise be free to collide on the same name -- Timeline.
    # add_track_clip() only rejects a duplicate *within* the engine-recorded
    # set, since it has no idea what --track will be passed at compose time.
    # Both track_names (focus resolution) and the per-boundary inset loop
    # below key a track by name alone, so a collision would silently let one
    # win in Focus(view=name) lookups while the inset loop still rendered
    # *both* on top of each other -- fail loudly instead.
    seen_names = set()
    for track in track_defs:
        if track["name"] in seen_names:
            raise ComposeError(
                f"Track name {track['name']!r} is used by more than one track "
                "(an engine-recorded Start Track and/or an external "
                "tracks=/--track entry) -- track names must be unique across "
                "both sources."
            )
        seen_names.add(track["name"])

    # Names of *focusable* tracks only (engine-recorded via start_track()/
    # end_track(), and not opted out with focusable=False -- e.g. a shell
    # that should always stay a corner inset and never take over the main
    # view) -- an external tracks=/--track entry is never eligible to
    # become the main view either, so it is never in this set. A focus
    # event naming anything not in this set falls through to ordinary
    # actor/observer resolution (see _view_at's own docstring).
    track_names = frozenset(t["name"] for t in track_defs if t["focusable"])
    track_defs_by_name = {t["name"]: t for t in track_defs}
    track_windows = {
        name: (track_defs_by_name[name]["offset"], track_defs_by_name[name]["duration"])
        for name in track_names
    }

    # A focus event's view naming neither "actor"/"observer" nor any track
    # ever recorded on this timeline (focusable or not) cannot be the
    # documented "closed earlier"/"not opened yet"/"opted out with
    # focusable=False" cases -- those all name a track that *is* in
    # track_defs_by_name. It can only be a typo (or a name that was never
    # Start Track'd at all), silently falling back to ordinary resolution
    # with nothing else in the pipeline ever surfacing the mistake -- warn
    # here instead of failing the whole compose, since the documented
    # fallback itself is deliberate and must still work for the legitimate
    # cases above.
    for event in focus_events:
        requested = event["view"]
        if requested in ("actor", "observer") or requested in track_defs_by_name:
            continue
        warnings.warn(
            f"Focus(view={requested!r}) at {event['time']:.3f}s matches "
            "neither 'actor'/'observer' nor any track recorded on this "
            "timeline -- falling back to ordinary actor/observer "
            "resolution. If this name was meant to match a track, check "
            "it against Start Track for a typo.",
            stacklevel=2,
        )

    boundaries = {0.0, observer_duration}
    for _turn_id, _actor, start, end in turns:
        boundaries.add(start)
        boundaries.add(end)
    for event in focus_events + chapter_events + hold_events:
        boundaries.add(event["time"])
    # A track's own start and end are cut points too: _view_at() resolves a
    # whole segment from its midpoint, and the inset loop below decides
    # whether a track has started from the segment's start -- without these,
    # a track starting or running out mid-segment would be promoted (or
    # shown as an inset) with footage from before its own t=0, out of sync.
    for track in track_defs:
        for edge in (track["offset"], track["offset"] + track["duration"]):
            if 0.0 < edge < observer_duration:
                boundaries.add(edge)
    # Everything before a leading first chapter is dropped (see `shift`
    # above) -- every boundary inside the lead-in, not only 0.0, or the
    # segments between them would still render it.
    boundaries = sorted(b for b in boundaries if b >= shift - 1e-6)

    title_inputs = {}  # cache key -> input index

    # Normalize every slice to VIDEO_SIZE: concat requires every segment to
    # share one frame size, and nothing guarantees a source clip (or a
    # title card, already rendered at VIDEO_SIZE) was recorded at exactly
    # that size -- e.g. a differently configured viewport, or (as in
    # tests/test_compose.py) a synthetic clip standing in for a recording.
    scale_to_size = f"scale={VIDEO_SIZE.replace('x', ':')}"

    def observer_slice(label, start, end):
        end = max(end, start + 0.001)
        filters.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,"
            f"{scale_to_size},fps={FPS}[{label}]"
        )

    def turn_slice(label, turn_id, start, end):
        clip = clips[turn_id]
        index = input_index_by_turn[turn_id]
        rel_start = max(0.0, start - clip["offset"])
        rel_end = min(clip["duration"], max(rel_start + 0.001, end - clip["offset"]))
        filters.append(
            f"[{index}:v]trim=start={rel_start:.3f}:end={rel_end:.3f},"
            f"setpts=PTS-STARTPTS,{scale_to_size},fps={FPS}[{label}]"
        )

    def frozen_turn_slice(label, turn_id, at, duration):
        clip = clips[turn_id]
        index = input_index_by_turn[turn_id]
        freeze_at = max(0.0, min(clip["duration"] - 0.04, at - clip["offset"]))
        filters.append(
            f"[{index}:v]trim=start={freeze_at:.3f}:end={freeze_at + 0.04:.3f},"
            f"setpts=PTS-STARTPTS,{scale_to_size},tpad=stop_duration={duration:.3f}:"
            f"stop_mode=clone,fps={FPS}[{label}]"
        )

    def frozen_observer_slice(label, at, duration):
        at = max(0.0, min(observer_duration - 0.04, at))
        filters.append(
            f"[0:v]trim=start={at:.3f}:end={at + 0.04:.3f},"
            f"setpts=PTS-STARTPTS,{scale_to_size},tpad=stop_duration={duration:.3f}:"
            f"stop_mode=clone,fps={FPS}[{label}]"
        )

    def pad_inset(src, dst, scale, border=DEFAULT_BORDER):
        filters.append(
            f"[{src}]scale=iw*{scale}:-2,"
            f"pad=iw+{2 * border}:ih+{2 * border}:{border}:"
            f"{border}:color={DEFAULT_BORDER_COLOR}[{dst}]"
        )

    def pad_inset_faded(src, dst, scale, border=DEFAULT_BORDER):
        """Like pad_inset(), but the inset's own left third stays fully
        opaque and its trailing two-thirds fade to transparent on a
        logarithmic curve (the standard audio fade-out shape: steep at
        first, easing out towards zero) -- for a track PiP that is always
        on screen (never promoted to main), this keeps its most legible,
        left-aligned content readable while letting more of the main view
        show through behind the rest of the box."""
        filters.append(
            f"[{src}]scale=iw*{scale}:-2,"
            f"pad=iw+{2 * border}:ih+{2 * border}:{border}:"
            f"{border}:color={DEFAULT_BORDER_COLOR},format=yuva420p,"
            "geq=lum='lum(X,Y)':cb='cb(X,Y)':cr='cr(X,Y)':"
            "a='if(lt(X,W/3),255,"
            "255*(1-log(1+9*(X-W/3)/(2*W/3))/log(10)))'"
            f"[{dst}]"
        )

    _CORNER_POSITIONS = {
        "bottom-right": "W-w-{m}:H-h-{m}",
        "bottom-left": "{m}:H-h-{m}",
        "top-right": "W-w-{m}:{m}",
        "top-left": "{m}:{m}",
    }

    def overlay(main, inset, margin, dst, corner="bottom-right"):
        try:
            position = _CORNER_POSITIONS[corner]
        except KeyError:
            raise ComposeError(
                f"Unknown corner {corner!r} -- expected one of "
                f"{sorted(_CORNER_POSITIONS)}"
            ) from None
        filters.append(f"[{main}][{inset}]overlay={position.format(m=margin)}[{dst}]")

    def track_slice(label, track, start, end):
        """Analogous to turn_slice()/frozen_turn_slice(), but for an
        external track whose own duration has no relationship to the
        take's own boundaries. Only called for a segment at or after
        `track["offset"]`: a track's offset is itself a boundary, and both
        callers (the inset loop's own `start < offset` skip, and
        `_view_at()`'s `track_windows` check for the main view) leave out
        a segment before it -- so `rel_start` here is never negative."""
        index = track["input_index"]
        rel_start = max(0.0, start - track["offset"])
        rel_end = end - track["offset"]
        total = end - start
        if rel_start >= track["duration"] - 1e-6:
            # Already finished before this segment even starts: freeze its
            # very last frame for the whole segment, same technique as
            # frozen_turn_slice()'s fixed ~1-frame freeze window.
            freeze_at = max(0.0, track["duration"] - 0.04)
            filters.append(
                f"[{index}:v]trim=start={freeze_at:.3f}:end={freeze_at + 0.04:.3f},"
                f"setpts=PTS-STARTPTS,{scale_to_size},"
                f"tpad=stop_duration={max(total - 0.04, 0.001):.3f}:"
                f"stop_mode=clone,fps={FPS}[{label}]"
            )
            return
        avail_end = min(rel_end, track["duration"])
        trim_end = max(rel_start + 0.001, avail_end)
        covered = trim_end - rel_start
        freeze_duration = max(0.0, total - covered)
        if freeze_duration <= 1e-6:
            # Fully covers the segment: plain trim.
            filters.append(
                f"[{index}:v]trim=start={rel_start:.3f}:end={trim_end:.3f},"
                f"setpts=PTS-STARTPTS,{scale_to_size},fps={FPS}[{label}]"
            )
        else:
            # Runs out partway through the segment: trim what's available,
            # then freeze its last frame for the remainder.
            filters.append(
                f"[{index}:v]trim=start={rel_start:.3f}:end={trim_end:.3f},"
                f"setpts=PTS-STARTPTS,{scale_to_size},"
                f"tpad=stop_duration={freeze_duration:.3f}:stop_mode=clone,"
                f"fps={FPS}[{label}]"
            )

    def frozen_track_slice(label, track, at, duration):
        """Analogous to frozen_turn_slice(), but freezing a track's own
        frame instead of an actor's -- used by emit_holds_at() when a
        `hold` event's effective view is a track (see its own call site):
        `_view_at()` only ever resolves to `f"track:{name}"` when the track
        is actually available at `at` (see `_view_at`'s own `track_windows`
        docstring), so `freeze_at` here is never negative before the
        `max(0.0, ...)` clamp -- kept anyway, same defensive style
        frozen_turn_slice() already uses for its own always-live turn_id."""
        index = track["input_index"]
        freeze_at = max(0.0, min(track["duration"] - 0.04, at - track["offset"]))
        filters.append(
            f"[{index}:v]trim=start={freeze_at:.3f}:end={freeze_at + 0.04:.3f},"
            f"setpts=PTS-STARTPTS,{scale_to_size},tpad=stop_duration={duration:.3f}:"
            f"stop_mode=clone,fps={FPS}[{label}]"
        )

    counter = [0]

    def next_label(prefix):
        counter[0] += 1
        return f"{prefix}{counter[0]}"

    def emit_chapters_at(t):
        for chapter in chapter_events:
            if abs(chapter["time"] - t) >= 1e-6:
                continue
            key = (
                chapter["eyebrow"],
                chapter["title"],
                chapter["subtitle"],
                chapter["duration"],
            )
            if key not in title_inputs:
                card_path = title_dir / f"title-{len(title_inputs) + 1:02d}.webm"
                make_title_card(card_path, *key)
                title_inputs[key] = len(inputs) // 2
                inputs.extend(["-i", str(card_path)])
            label = next_label("chapter")
            filters.append(f"[{title_inputs[key]}:v]fps={FPS}[{label}]")
            segment_labels.append(label)

    def emit_holds_at(t):
        for hold in hold_events:
            if abs(hold["time"] - t) >= 1e-6:
                continue
            if hold.get("recorded"):
                # Real elapsed recording time (e.g. the return-to-observer
                # wait), not a synthetic freeze to insert -- that stretch is
                # already ordinary observer footage the boundary loop below
                # renders on its own; a segment emitted here would double it.
                continue
            view, inset_turn_id, _scale, _margin, _border, _solo = _view_at(
                max(0.0, t - 1e-6), focus_events, turns, track_names, track_windows
            )
            main_label = next_label("holdmain")
            # hold.get("view", view) falls back to the ambient resolved
            # `view` only when the event itself has no "view" key at all
            # (a hand-edited/pre-existing timeline -- the `Hold` keyword
            # always writes one) -- the schema restricts an explicit
            # "view" to "actor"/"observer", so a "track:<name>" result here
            # can only come from that fallback, never from an explicit
            # value, and only when _view_at() just confirmed the track is
            # actually available at `t` (see its own track_windows
            # docstring).
            resolved = hold.get("view", view)
            if resolved == "actor" and inset_turn_id is not None:
                frozen_turn_slice(main_label, inset_turn_id, t, hold["duration"])
            elif resolved.startswith("track:"):
                track = track_defs_by_name[resolved[len("track:") :]]
                frozen_track_slice(main_label, track, t, hold["duration"])
            else:
                frozen_observer_slice(main_label, t, hold["duration"])
            segment_labels.append(main_label)

    for index in range(len(boundaries) - 1):
        start, end = boundaries[index], boundaries[index + 1]
        if end - start <= 1e-6:
            continue

        emit_chapters_at(start)
        emit_holds_at(start)

        view, inset_turn_id, scale, margin, border, solo = _view_at(
            (start + end) / 2, focus_events, turns, track_names, track_windows
        )
        label = next_label("seg")
        main_track_name = view[len("track:") :] if view.startswith("track:") else None
        if main_track_name is not None:
            main_track = track_defs_by_name[main_track_name]
            main_label = next_label("main")
            track_slice(main_label, main_track, start, end)
            label = main_label

            if not solo:
                # Observer: always available (the whole-take recording) as
                # a PiP -- bottom-right matches the engine's own single-PiP
                # default corner, so a take with only one other screen
                # actually showing at a given moment still reads the same
                # way a classic 2-screen (observer+actor) take always has.
                observer_raw = next_label("inset")
                observer_slice(observer_raw, start, end)
                observer_padded = next_label("inset")
                pad_inset(observer_raw, observer_padded, scale, border)
                combined = next_label("seg")
                overlay(label, observer_padded, margin, combined, "bottom-right")
                label = combined

                # Actor: a second PiP, bottom-left, only when a turn is live
                # or was left live-focused into a gap -- same availability
                # rule (and live-vs-frozen choice) the observer-main branch
                # below already applies to its own actor inset.
                if inset_turn_id is not None:
                    actor_raw = next_label("inset")
                    _turn_start, turn_end = turns_by_id[inset_turn_id]
                    if end <= turn_end + 1e-6:
                        turn_slice(actor_raw, inset_turn_id, start, end)
                    else:
                        frozen_turn_slice(
                            actor_raw, inset_turn_id, turn_end, end - start
                        )
                    actor_padded = next_label("inset")
                    pad_inset(actor_raw, actor_padded, scale, border)
                    combined2 = next_label("seg")
                    overlay(label, actor_padded, margin, combined2, "bottom-left")
                    label = combined2
        elif view == "actor":
            main_label = next_label("main")
            _turn_start, turn_end = turns_by_id[inset_turn_id]
            if end <= turn_end + 1e-6:
                # The turn is still open for this whole segment: live footage.
                turn_slice(main_label, inset_turn_id, start, end)
            else:
                # Focus was flipped to "actor" and never flipped back before
                # the turn closed (e.g. a story deliberately ending on the
                # actor's last frame) -- the turn has no more live footage
                # past its own turn_end, so this segment would otherwise ask
                # turn_slice() to trim a range starting at or beyond the
                # clip's own recorded length (rel_start >= rel_end), handing
                # ffmpeg a backwards/empty trim and silently producing a
                # near-zero-length segment -- exactly the inset branch below
                # already guards against with this same turn_end clamp.
                frozen_turn_slice(main_label, inset_turn_id, turn_end, end - start)
            if solo:
                filters.append(f"[{main_label}]null[{label}]")
            else:
                inset_label = next_label("inset")
                observer_slice(f"{inset_label}raw", start, end)
                pad_inset(f"{inset_label}raw", inset_label, scale, border)
                overlay(main_label, inset_label, margin, label)
        else:
            main_label = next_label("main")
            observer_slice(main_label, start, end)
            if solo or inset_turn_id is None:
                filters.append(f"[{main_label}]null[{label}]")
            else:
                inset_label = next_label("inset")
                _turn_start, turn_end = turns_by_id[inset_turn_id]
                if end <= turn_end + 1e-6:
                    # The actor's own turn is technically still open (focus
                    # was flipped mid-turn): show their live footage.
                    turn_slice(f"{inset_label}raw", inset_turn_id, start, end)
                else:
                    frozen_turn_slice(
                        f"{inset_label}raw", inset_turn_id, turn_end, end - start
                    )
                pad_inset(f"{inset_label}raw", inset_label, scale, border)
                overlay(main_label, inset_label, margin, label)

        # External PiP tracks: composited only onto this live segment's own
        # `label`, never onto a chapter/hold segment (those are appended to
        # segment_labels directly, above/below this loop, and never reach
        # here) -- see the module docstring's "External PiP tracks" section
        # for why that is exactly what makes a track vanish behind a title
        # card for free. Each track layers onto the previous one's result.
        # `solo` turns this off too -- a solo segment shows only `label`,
        # no inset of any kind, track or otherwise.
        for track in [] if solo else track_defs:
            if track["name"] == main_track_name:
                continue  # already rendered full-frame as the main view above
            if start < track["offset"] - 1e-6:
                continue  # hasn't started yet: nothing to show for it here
            track_raw = next_label("trackraw")
            track_slice(track_raw, track, start, end)
            track_padded = next_label("trackpad")
            pad_fn = pad_inset_faded if track["fade"] else pad_inset
            pad_fn(track_raw, track_padded, track["scale"], track["border"])
            combined = next_label("seg")
            overlay(label, track_padded, track["margin"], combined, track["corner"])
            label = combined

        segment_labels.append(label)

    # A chapter/hold clamped onto the final boundary (observer_duration) is
    # never the `start` of an [index, index+1) interval above, since it IS
    # the end of the last one -- emit it here instead of losing it.
    emit_chapters_at(boundaries[-1])
    emit_holds_at(boundaries[-1])

    if not segment_labels:
        raise ComposeError("Nothing to compose: the timeline has no segments")

    concat_inputs = "".join(f"[{label}]" for label in segment_labels)
    filters.append(
        f"{concat_inputs}concat=n={len(segment_labels)}:v=1:a=0,format=yuv420p[out]"
    )

    ffmpeg(
        "-y",
        "-v",
        "error",
        "-nostats",
        *inputs,
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[out]",
        "-c:v",
        "libvpx-vp9",
        "-deadline",
        "good",
        "-b:v",
        "0",
        "-crf",
        "32",
        "-an",
        output,
        capture=False,
    )

    if caption_events:
        write_captions_vtt(
            output.with_suffix(".vtt"),
            caption_events,
            chapter_events,
            hold_events,
            shift,
        )

    return output
