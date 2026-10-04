"""`python -m screencast verify DIR/` -- automated take verification,
replacing the manual "Verifying a take" steps duplicated across each
docs/*-scenario.md.

Checks:

- **ffprobe**: exactly one 1920x1080, 25fps video stream on the composed
  output, and its duration matches what the timeline predicts (the
  observer's own measured length, less any lead-in the composer drops,
  plus every chapter/hold duration it inserts -- see
  predicted_duration()), within DURATION_TOLERANCE.
- **Contact sheet**: a `rows x cols` tile built at a sampling rate derived
  from the *real* measured duration (`fps >= rows*cols / duration`), so a
  longer take than the last one still gets full coverage instead of a
  stale, undersized rate.
- **Dead air**, judged from the timeline: the library records a `wait`
  event around every waiting keyword (`Sleep`, `Wait Until Keyword
  Succeeds`, the engine's own waits, and any keyword tagged
  `screencast:wait`). A single wait longer than DEAD_AIR_MAX_WAIT is an
  error -- the story stood still on screen for that long -- and total
  waiting above DEAD_AIR_TOTAL_WAIT is a warning. Pixels cannot decide
  this: `freezedetect` compares whole frames and cannot see cursor-only
  motion at 1920x1080, so it called ordinary human-paced turns "frozen".
- **Blank frames**: `blackdetect` on the composed output, tuned to
  near-pure black (`pix_th=0.02`) rather than the default's
  dark-theme-triggering 10% luma threshold, since this project's own
  title cards are a dark navy that would otherwise false-positive on
  every take -- plus a uniform-color sample (the same technique as
  "Empty insets", below, applied to each actor clip's own opening moment
  instead of the observer's) since a blank turn-opening frame is just as
  often a plain white Plone loading page (or any other flat color a
  missing font renders as) as it is black, and blackdetect alone would
  never see it.
- **Empty insets**: for each actor turn, one raw grayscale sample from the
  observer's own footage at the turn's real-time midpoint (the frame that
  becomes that turn's inset). A near-zero luma range means a blank/dead
  inset -- also what missing fonts look like, per the browser skill.
- **Captions**: only when the timeline has at least one `caption` event
  (see library.py's `Caption` keyword and compose.py's `write_captions_vtt`).
  Fails if `compose()` should have written a WebVTT sidecar but didn't, or
  if any cue in it ends after the composed output's own measured duration
  -- a caption time that was never correctly mapped past an inserted title
  card/hold would show this way.

Returns a JSON-serializable report (`{"ok": bool, "findings": [...],
...}`); `main()`/the CLI also write it to `report.json` and the contact
sheet to `contact-sheet.png` in the take directory.
"""

from pathlib import Path
from screencast.compose import _inserted_holds
from screencast.compose import _leading_shift
from screencast.compose import ffprobe_duration
from screencast.timeline import Timeline
import json
import re
import subprocess


DURATION_TOLERANCE = 1.5  # seconds; see screencast.timeline.OVERLAP_TOLERANCE
# Dead air is judged from the timeline (the `wait` events the library records
# around every waiting keyword), not from pixels: freezedetect cannot see
# cursor-only motion at 1920x1080, so it called ordinary human-paced turns
# "frozen". A pixel check has no usable signal here even as a coarse one: on a
# live contact_form take the longest frozen stretch of a healthy take (16.7s,
# a title card merging with the static page after it) was longer than that
# of a take with a deliberate 12s Sleep (15.9s). The longest wait in a healthy
# live take is about 3.4s and the total 1-9s (contact_form, review_process,
# renovation_project), so a single wait beyond 10s is a story that stood
# still on screen and needs fixing.
DEAD_AIR_MAX_WAIT = 10.0  # seconds, one wait: an error
DEAD_AIR_TOTAL_WAIT = 30.0  # seconds, all waits in the take: a warning
BLANK_LUMA_RANGE = 4  # max:min luma spread below this counts as "uniform"
# Sampling exactly at a turn's start boundary risks a compressed keyframe
# seek landing just before the composer's hard cut there and reading the
# previous segment's content instead -- a small offset into the turn avoids
# that while still sampling what the turn visibly opens on.
TURN_OPEN_SAMPLE_OFFSET = 0.15
EXPECTED_SIZE = (1920, 1080)
EXPECTED_FPS = 25.0


class VerifyError(ValueError):
    pass


def ffprobe_streams(video):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v",
            "-show_entries",
            "stream=width,height,r_frame_rate",
            "-of",
            "json",
            str(video),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)["streams"]


def frame_luma_range(video, at, width=64, height=36):
    """The luma (max - min) of one downscaled frame sampled at `at`
    seconds -- near zero means the frame is a uniform color."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{max(0.0, at):.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:{height}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    data = result.stdout
    if not data:
        raise VerifyError(f"Could not sample a frame at {at:.2f}s from {video}")
    return max(data) - min(data)


_BLACK_RE = re.compile(
    r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)\s+black_duration:\s*([\d.]+)"
)
_VTT_CUE_RE = re.compile(
    r"(\d+):(\d+):(\d+(?:\.\d+)?)\s*-->\s*(\d+):(\d+):(\d+(?:\.\d+)?)"
)


def parse_vtt_cue_times(vtt_path):
    """`[(start_seconds, end_seconds), ...]` for every cue in a WebVTT
    file, in file order -- just the timestamp lines, ignoring cue
    identifiers/text."""
    text = Path(vtt_path).read_text()
    cues = []
    for match in _VTT_CUE_RE.finditer(text):
        sh, sm, ss, eh, em, es = match.groups()
        start = int(sh) * 3600 + int(sm) * 60 + float(ss)
        end = int(eh) * 3600 + int(em) * 60 + float(es)
        cues.append((start, end))
    return cues


def detect_black_intervals(video, min_duration=0.5, pic_th=0.98, pix_th=0.02):
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "info",
            "-i",
            str(video),
            "-vf",
            f"blackdetect=d={min_duration}:pic_th={pic_th}:pix_th={pix_th}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    return [
        {
            "start": float(m.group(1)),
            "end": float(m.group(2)),
            "duration": float(m.group(3)),
        }
        for m in _BLACK_RE.finditer(result.stderr)
    ]


def contact_sheet_fps(duration, rows=6, cols=5):
    """The sampling rate at which `rows * cols` frames span the *whole*
    clip. `tile` buffers that many sampled frames before emitting one image,
    so any higher rate silently covers only the clip's opening: the sheet
    would never show the back half, which is where a take's ending (the
    finale, a truncated composite) is. There is deliberately no lower bound
    -- an earlier `max(..., 0.5)` floor limited every take longer than
    `rows * cols / 0.5` = 60 s to its first minute."""
    return (rows * cols) / max(duration, 0.1)


def make_contact_sheet(video, output, rows=6, cols=5, duration=None):
    duration = duration or ffprobe_duration(video)
    fps = contact_sheet_fps(duration, rows, cols)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(video),
            "-vf",
            f"fps={fps},scale=320:-1,tile={cols}x{rows}",
            "-frames:v",
            "1",
            str(output),
        ],
        check=True,
    )
    return Path(output)


def predicted_duration(timeline, observer_duration):
    """What the composer's output duration should be: the observer's own
    measured length, plus every chapter/hold duration it inserts (see
    screencast.compose's module docstring -- inserted segments never
    consume recorded footage, they only add to the output). A "recorded"
    hold (e.g. the return-to-observer wait, see library.py's
    end_actor_turn) is excluded: the composer never inserts a synthetic
    freeze for it, since that time is already real, un-trimmed observer
    footage -- counting it here would overshoot the actual output length.

    Also subtracts compose._leading_shift(): when the take's first chapter
    fires at or before its own turn starts, compose() opens directly on
    that card and drops the raw observer footage ahead of it (Start
    Observer's own load, the first turn's context creation and
    navigation -- see that function's own docstring) instead of rendering
    it as a live segment first. That raw footage is real time the observer
    recording measures but the composed output never includes, so counting
    the observer's full measured length here would overstate the expected
    total by exactly that dropped lead-in -- usually a second or two
    (within DURATION_TOLERANCE, so invisible), but not always: a take
    whose `Start Observing` does real unrecorded-observer-clock setup
    *after* `Start Observer` (e.g. standing up a second track) before the
    first turn opens can easily push the lead-in past a few seconds. A
    hold inside that lead-in is dropped with it (compose._inserted_holds)."""
    chapter_events = timeline.events_of("chapter")
    turn_starts = [e["time"] for e in timeline.events_of("turn_start")]
    shift = _leading_shift(chapter_events, turn_starts)
    inserted = sum(e["duration"] for e in chapter_events)
    inserted += sum(
        e["duration"] for e in _inserted_holds(timeline.events_of("hold"), shift)
    )
    return observer_duration - shift + inserted


def verify(take_dir, output_video=None, contact_sheet=None, rows=6, cols=5):
    take_dir = Path(take_dir)
    timeline = Timeline.load(take_dir / "timeline.json")
    observer_duration = ffprobe_duration(take_dir / timeline.observer["video"])
    expected_duration = predicted_duration(timeline, observer_duration)

    output_video = Path(output_video) if output_video else take_dir / "output.webm"
    if not output_video.exists():
        raise VerifyError(f"No composed output at {output_video}")

    findings = []

    streams = ffprobe_streams(output_video)
    if len(streams) != 1:
        findings.append(
            {
                "check": "stream",
                "severity": "error",
                "message": f"Expected exactly one video stream, found {len(streams)}",
            }
        )
    else:
        stream = streams[0]
        size = (stream.get("width"), stream.get("height"))
        fps = _parse_rate(stream.get("r_frame_rate"))
        if size != EXPECTED_SIZE:
            findings.append(
                {
                    "check": "stream",
                    "severity": "error",
                    "message": f"Expected {EXPECTED_SIZE[0]}x{EXPECTED_SIZE[1]}, got {size[0]}x{size[1]}",
                }
            )
        if fps is None or abs(fps - EXPECTED_FPS) > 0.1:
            findings.append(
                {
                    "check": "stream",
                    "severity": "error",
                    "message": f"Expected {EXPECTED_FPS}fps, got {fps}",
                }
            )

    actual_duration = ffprobe_duration(output_video)
    if abs(actual_duration - expected_duration) > DURATION_TOLERANCE:
        findings.append(
            {
                "check": "duration",
                "severity": "error",
                "message": (
                    f"Expected ~{expected_duration:.2f}s (observer "
                    f"{observer_duration:.2f}s + inserted segments), got "
                    f"{actual_duration:.2f}s"
                ),
            }
        )

    waits = timeline.events_of("wait")
    waited = sum(wait["duration"] for wait in waits)
    longest_wait = max((wait["duration"] for wait in waits), default=0.0)
    for wait in waits:
        if wait["duration"] > DEAD_AIR_MAX_WAIT:
            findings.append(
                {
                    "check": "dead_air",
                    "severity": "error",
                    "message": (
                        f"{wait.get('keyword') or 'A wait'} waited "
                        f"{wait['duration']:.1f}s, {wait['time']:.0f}s into "
                        "the take: nothing is driven on screen for that long "
                        f"(limit {DEAD_AIR_MAX_WAIT:g}s)"
                    ),
                }
            )
    if waited > DEAD_AIR_TOTAL_WAIT:
        findings.append(
            {
                "check": "dead_air",
                "severity": "warning",
                "message": (
                    f"{waited:.1f}s spent waiting across {len(waits)} waits "
                    f"(warning above {DEAD_AIR_TOTAL_WAIT:g}s): the take "
                    "spends much of its length with nothing on screen"
                ),
            }
        )

    black_intervals = detect_black_intervals(output_video)
    for interval in black_intervals:
        findings.append(
            {
                "check": "blank_frame",
                "severity": "error",
                "message": (
                    f"Near-black video from {interval['start']:.2f}s to "
                    f"{interval['end']:.2f}s ({interval['duration']:.2f}s) -- "
                    "also what missing fonts look like"
                ),
            }
        )

    # blackdetect only catches near-pure-black -- a turn's own opening frame
    # can just as easily be a plain white Plone loading page, or any other
    # flat color a missing font renders as. Sample each actor clip's own
    # opening moment directly, the same uniform-color technique empty_inset
    # below already uses on the observer's footage: the composed output
    # itself is a poor sampling target here, since every turn's segment
    # overlays the observer as a bordered inset, and that border alone
    # keeps the composited frame's luma range well above BLANK_LUMA_RANGE
    # regardless of whether the actor's own content is blank.
    # The composer enters a turn's clip at its turn_start mark, not at the
    # clip's first frame: a page paints a fraction of a second after its
    # context opens, and that blank lead-in is cut. Sample just after where
    # the composer actually enters the clip (turn_start events pair with the
    # actor clips in order, as in compose), so a lead-in it never shows is
    # not reported -- a fixed offset into the raw clip failed a healthy take
    # whenever the page painted slightly slower than usual.
    starts = sorted(timeline.events_of("turn_start"), key=lambda e: e["time"])
    paired = len(starts) == len(timeline.actors)
    for index, clip in enumerate(timeline.actors):
        clip_path = take_dir / clip["video"]
        clip_duration = ffprobe_duration(clip_path)
        entered = max(0.0, starts[index]["time"] - clip["offset"]) if paired else 0.0
        at = min(entered + TURN_OPEN_SAMPLE_OFFSET, max(0.0, clip_duration - 0.05))
        luma_range = frame_luma_range(clip_path, at)
        if luma_range < BLANK_LUMA_RANGE:
            findings.append(
                {
                    "check": "blank_frame",
                    "severity": "error",
                    "message": (
                        f"{clip['actor']!r}'s turn ({clip['video']}) opens "
                        f"at {at:.2f}s on a near-uniform color frame (luma "
                        f"range {luma_range}) -- also what missing fonts "
                        "look like"
                    ),
                }
            )

    # Pair each turn_start with the turn_end that follows it, not with the
    # first turn_end of the same actor: an actor with several turns would
    # otherwise have every later turn paired with their first turn's end,
    # sampling a midpoint before the turn even started.
    remaining_ends = sorted(timeline.events_of("turn_end"), key=lambda e: e["time"])
    for start_event in sorted(
        timeline.events_of("turn_start"), key=lambda e: e["time"]
    ):
        end_event = next(
            (
                e
                for e in remaining_ends
                if e["actor"] == start_event["actor"]
                and e["time"] >= start_event["time"]
            ),
            None,
        )
        if end_event is None:
            continue
        remaining_ends.remove(end_event)
        midpoint = min(
            (start_event["time"] + end_event["time"]) / 2, observer_duration - 0.05
        )
        if midpoint < 0:
            continue
        luma_range = frame_luma_range(take_dir / timeline.observer["video"], midpoint)
        if luma_range < BLANK_LUMA_RANGE:
            findings.append(
                {
                    "check": "empty_inset",
                    "severity": "error",
                    "message": (
                        f"Observer frame at {midpoint:.2f}s (the inset during "
                        f"{start_event['actor']!r}'s turn) is a near-uniform "
                        f"color (luma range {luma_range})"
                    ),
                }
            )

    caption_events = timeline.events_of("caption")
    if caption_events:
        vtt_path = output_video.with_suffix(".vtt")
        if not vtt_path.exists():
            findings.append(
                {
                    "check": "captions",
                    "severity": "error",
                    "message": (
                        f"{len(caption_events)} caption event(s) on the "
                        f"timeline, but no {vtt_path.name} was composed"
                    ),
                }
            )
        else:
            for start, end in parse_vtt_cue_times(vtt_path):
                if end > actual_duration + DURATION_TOLERANCE:
                    findings.append(
                        {
                            "check": "captions",
                            "severity": "error",
                            "message": (
                                f"Caption cue {start:.2f}s-{end:.2f}s ends "
                                f"after the output's own {actual_duration:.2f}s "
                                "duration"
                            ),
                        }
                    )

    sheet_path = (
        Path(contact_sheet) if contact_sheet else take_dir / "contact-sheet.png"
    )
    make_contact_sheet(
        output_video, sheet_path, rows=rows, cols=cols, duration=actual_duration
    )

    return {
        "ok": not any(f["severity"] == "error" for f in findings),
        "observer_duration": observer_duration,
        "expected_duration": expected_duration,
        "actual_duration": actual_duration,
        "waited_seconds": round(waited, 3),
        "longest_wait": round(longest_wait, 3),
        "contact_sheet": str(sheet_path),
        "findings": findings,
    }


def _parse_rate(value):
    if not value:
        return None
    if "/" in value:
        num, _, den = value.partition("/")
        den = float(den) or 1.0
        return float(num) / den
    return float(value)
