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


def _view_at(t, focus_events, turns, tolerance=1e-6):
    """Resolve (kind, turn_id_or_None, scale, margin, border) in effect at
    time `t`. `turns` is a list of (turn_id, actor, start, end) sorted by
    start -- keyed by `turn_id` (a clip, i.e. one turn), not by `actor`
    name, since one actor can play more than one turn (e.g. "reception" in
    contact_form.robot has three) and two turns never share a clip."""
    active_turn = None
    most_recent_turn = None
    for turn_id, actor, start, end in turns:
        if start - tolerance <= t < end + tolerance:
            active_turn = (turn_id, actor, start, end)
        if start <= t + tolerance:
            most_recent_turn = (turn_id, actor, start, end)

    # The most recent focus event at or before `t`, scoped to the active
    # turn's own window when inside one (a focus event from a previous
    # turn or gap does not leak into a later turn's default).
    scope_start = active_turn[2] if active_turn else 0.0
    latest_focus = None
    for event in focus_events:
        if event["time"] <= t + tolerance and event["time"] >= scope_start - tolerance:
            latest_focus = event

    if active_turn:
        turn_id = active_turn[0]
        if latest_focus is None:
            return ("actor", turn_id, DEFAULT_SCALE, DEFAULT_MARGIN, DEFAULT_BORDER)
        if latest_focus["view"] == "actor":
            return (
                "actor",
                turn_id,
                latest_focus.get("scale", DEFAULT_SCALE),
                latest_focus.get("margin", DEFAULT_MARGIN),
                latest_focus.get("border", DEFAULT_BORDER),
            )
        return (
            "observer",
            most_recent_turn[0] if most_recent_turn else None,
            latest_focus.get("scale", DEFAULT_SCALE),
            latest_focus.get("margin", DEFAULT_MARGIN),
            latest_focus.get("border", DEFAULT_BORDER),
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
    )


def _output_time(raw_time, chapter_events, hold_events):
    """Map `raw_time` (observer-clock, the same clock every timeline event
    uses) to its position in the composed output: `raw_time` itself, plus
    the duration of every chapter/hold segment the composer actually
    inserts at or before it (title cards, and a non-"recorded" hold's
    synthetic freeze -- a "recorded" hold adds nothing, since no segment
    is inserted for it; see predicted_duration()'s own docstring for why)."""
    inserted = sum(e["duration"] for e in chapter_events if e["time"] <= raw_time)
    inserted += sum(
        e["duration"]
        for e in hold_events
        if e["time"] <= raw_time and not e.get("recorded")
    )
    return raw_time + inserted


def _format_vtt_timestamp(seconds):
    seconds = max(0.0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, remainder = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{remainder:06.3f}"


def write_captions_vtt(vtt_path, caption_events, chapter_events, hold_events):
    """Write a WebVTT sidecar for `caption_events` (schema v2's optional
    "caption" event type) at `vtt_path`, mapping each one's raw time to
    its actual position in the composed output via `_output_time()`.
    `duration` shifts the cue's start the same way but is not itself
    stretched by an insertion that happens to fall *inside* it -- a
    caption is expected to describe one continuous stretch of real
    footage, not straddle a title card or hold."""
    lines = ["WEBVTT", ""]
    for index, event in enumerate(
        sorted(caption_events, key=lambda e: e["time"]), start=1
    ):
        start = _output_time(event["time"], chapter_events, hold_events)
        end = start + float(event.get("duration", DEFAULT_CAPTION_DURATION))
        lines.append(str(index))
        lines.append(f"{_format_vtt_timestamp(start)} --> {_format_vtt_timestamp(end)}")
        lines.append(event["text"])
        lines.append("")
    vtt_path = Path(vtt_path)
    vtt_path.write_text("\n".join(lines))
    return vtt_path


def compose(take_dir, output=None):
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

    boundaries = {0.0, observer_duration}
    for _turn_id, _actor, start, end in turns:
        boundaries.add(start)
        boundaries.add(end)
    for event in focus_events + chapter_events + hold_events:
        boundaries.add(event["time"])
    boundaries = sorted(boundaries)

    output = Path(output) if output else take_dir / "output.webm"
    title_dir = take_dir / "titles"

    filters = []
    segment_labels = []
    inputs = ["-i", str(observer_video)]
    input_index_by_turn = {}
    for turn_id, clip in enumerate(clips):
        input_index_by_turn[turn_id] = len(inputs) // 2
        inputs += ["-i", str(clip["path"])]
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

    def overlay(main, inset, margin, dst):
        filters.append(f"[{main}][{inset}]overlay=W-w-{margin}:H-h-{margin}[{dst}]")

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
            view, inset_turn_id, _scale, _margin, _border = _view_at(
                max(0.0, t - 1e-6), focus_events, turns
            )
            main_label = next_label("holdmain")
            if hold.get("view", view) == "actor" and inset_turn_id is not None:
                frozen_turn_slice(main_label, inset_turn_id, t, hold["duration"])
            else:
                frozen_observer_slice(main_label, t, hold["duration"])
            segment_labels.append(main_label)

    for index in range(len(boundaries) - 1):
        start, end = boundaries[index], boundaries[index + 1]
        if end - start <= 1e-6:
            continue

        emit_chapters_at(start)
        emit_holds_at(start)

        view, inset_turn_id, scale, margin, border = _view_at(
            (start + end) / 2, focus_events, turns
        )
        label = next_label("seg")
        if view == "actor":
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
            inset_label = next_label("inset")
            observer_slice(f"{inset_label}raw", start, end)
            pad_inset(f"{inset_label}raw", inset_label, scale, border)
            overlay(main_label, inset_label, margin, label)
        else:
            main_label = next_label("main")
            observer_slice(main_label, start, end)
            if inset_turn_id is None:
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
            output.with_suffix(".vtt"), caption_events, chapter_events, hold_events
        )

    return output
