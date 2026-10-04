"""screencast.verify tests against real ffmpeg output on tiny synthetic
clips -- skipped when ffmpeg/ffprobe aren't on PATH. Also manually verified
end-to-end against the real toy take composed for #4/#5/#6 (see the #7
commit message)."""

from screencast.compose import compose
from screencast.timeline import Timeline
from screencast.verify import detect_black_intervals
from screencast.verify import frame_luma_range
from screencast.verify import parse_vtt_cue_times
from screencast.verify import predicted_duration
from screencast.verify import verify
import pytest
import shutil
import subprocess


requires_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH",
)


def make_clip(path, duration, color="blue", size="320x180"):
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size}:d={duration}:r=25",
            "-c:v",
            "libvpx",
            "-deadline",
            "realtime",
            "-cpu-used",
            "16",
            str(path),
        ],
        check=True,
    )


@requires_ffmpeg
def test_frame_luma_range_is_near_zero_for_a_solid_color(tmp_path):
    clip = tmp_path / "solid.webm"
    make_clip(clip, 1.0, color="black")
    assert frame_luma_range(clip, 0.5) < 4


@requires_ffmpeg
def test_frame_luma_range_is_large_for_content(tmp_path):
    clip = tmp_path / "checkers.webm"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:duration=1:rate=25",
            str(clip),
        ],
        check=True,
    )
    assert frame_luma_range(clip, 0.5) > 20


@requires_ffmpeg
def test_detect_black_intervals_finds_near_pure_black(tmp_path):
    clip = tmp_path / "black.webm"
    make_clip(clip, 2.0, color="black")
    intervals = detect_black_intervals(clip, min_duration=0.5)
    assert intervals
    assert intervals[0]["duration"] > 0.5


@requires_ffmpeg
def test_detect_black_intervals_ignores_a_dark_but_not_black_theme(tmp_path):
    """The project's own title cards are a dark navy (#0f172a) -- the
    default blackdetect threshold would false-positive on every take."""
    clip = tmp_path / "navy.webm"
    make_clip(clip, 2.0, color="0x0f172a")
    intervals = detect_black_intervals(clip, min_duration=0.5)
    assert intervals == []


def make_animated_clip(path, duration, color="blue"):
    """Per-frame random noise over a solid color, standing in for real
    recorded footage (which always has at least cursor movement) -- unlike
    make_clip()'s flat color (or, it turns out, ffmpeg's own `testsrc`,
    whose motion is too gradual to clear freezedetect's noise floor over a
    1s window), this reliably does not itself look like dead air or a
    blank frame to freezedetect/frame_luma_range. `-crf 20 -b:v 4M` keeps
    enough bitrate that the noise survives frame to frame -- a more
    aggressively compressed encode converges consecutive frames to the
    same predicted content despite the spatial noise, which reads as
    frozen. `-cpu-used 5` keeps this fast regardless (~0.4s/clip here vs.
    ~30s at the encoder's default effort)."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x180:d={duration}:r=25",
            "-vf",
            "noise=alls=40:allf=t+u",
            "-c:v",
            "libvpx",
            "-deadline",
            "good",
            "-cpu-used",
            "5",
            "-crf",
            "20",
            "-b:v",
            "4M",
            str(path),
        ],
        check=True,
    )


def make_flat_then_animated_clip(path, flat_seconds, total_duration, color="white"):
    """A clip that is one flat colour for `flat_seconds` (a page that has not
    painted yet), then animated."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x180:d={flat_seconds}:r=25",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:s=320x180:d={total_duration - flat_seconds}:r=25",
            "-filter_complex",
            "[1:v]noise=alls=40:allf=t+u[b];[0:v][b]concat=n=2:v=1:a=0[out]",
            "-map",
            "[out]",
            "-c:v",
            "libvpx",
            "-deadline",
            "good",
            "-cpu-used",
            "5",
            "-crf",
            "20",
            "-b:v",
            "4M",
            str(path),
        ],
        check=True,
    )


def make_take(take_dir, observer_duration=4.0, with_turn=True):
    take_dir.mkdir(parents=True, exist_ok=True)
    make_animated_clip(take_dir / "observer.webm", observer_duration)
    timeline = Timeline.new("observer.webm")
    if with_turn:
        make_animated_clip(take_dir / "author.webm", 1.5)
        timeline.add_actor_clip("author", "author.webm", offset=1.0, duration=1.5)
        timeline.add_event({"type": "turn_start", "time": 1.0, "actor": "author"})
        timeline.add_event({"type": "turn_end", "time": 2.5, "actor": "author"})
    timeline.save(take_dir / "timeline.json")
    return timeline


@requires_ffmpeg
def test_predicted_duration_adds_chapter_and_hold_durations(tmp_path):
    timeline = Timeline.new("observer.webm")
    timeline.add_event(
        {
            "type": "chapter",
            "time": 0.0,
            "eyebrow": "e",
            "title": "t",
            "subtitle": "s",
            "duration": 3.0,
        }
    )
    timeline.add_event({"type": "hold", "time": 1.0, "duration": 2.0})
    assert predicted_duration(timeline, observer_duration=5.0) == 10.0


@requires_ffmpeg
def test_predicted_duration_excludes_a_recorded_hold(tmp_path):
    """(regression, PR #14 follow-up review) A "recorded" hold (the
    return-to-observer wait) is real elapsed time already inside the
    observer's own footage -- the composer never inserts a synthetic
    freeze for it, so predicted_duration must not add it either, or
    verify's duration check would fail a perfectly healthy take."""
    timeline = Timeline.new("observer.webm")
    timeline.add_event({"type": "hold", "time": 1.0, "duration": 2.0})
    timeline.add_event({"type": "hold", "time": 4.0, "duration": 6.0, "recorded": True})
    assert predicted_duration(timeline, observer_duration=5.0) == 7.0


@requires_ffmpeg
def test_predicted_duration_subtracts_a_dropped_leading_chapter(tmp_path):
    """(regression) When the take's first chapter fires at or before its
    own turn starts, compose() opens directly on the card and drops the
    raw observer footage ahead of it (compose._leading_shift) -- counting
    the observer's *full* measured length here would overstate the
    expected total by exactly that dropped lead-in, which `verify()`'s own
    duration check would then flag on an otherwise perfectly healthy
    take (see its DURATION_TOLERANCE)."""
    timeline = Timeline.new("observer.webm")
    timeline.add_event({"type": "turn_start", "time": 6.0, "actor": "author"})
    timeline.add_event(
        {
            "type": "chapter",
            "time": 6.0,
            "eyebrow": "e",
            "title": "t",
            "subtitle": "s",
            "duration": 8.0,
        }
    )
    # observer=20.0, lead-in dropped=6.0, chapter inserted=8.0 -> 20-6+8=22.0,
    # not the naive 20.0+8.0=28.0 the old formula would have predicted.
    assert predicted_duration(timeline, observer_duration=20.0) == 22.0


def test_predicted_duration_excludes_a_hold_inside_the_dropped_lead_in():
    """compose() drops a hold's boundary along with the rest of the lead-in,
    so its freeze is never inserted and must not be expected either."""
    timeline = Timeline.new("observer.webm")
    timeline.add_event({"type": "hold", "time": 2.0, "duration": 3.0})
    timeline.add_event({"type": "turn_start", "time": 6.0, "actor": "author"})
    timeline.add_event(
        {
            "type": "chapter",
            "time": 6.0,
            "eyebrow": "e",
            "title": "t",
            "subtitle": "s",
            "duration": 8.0,
        }
    )
    assert predicted_duration(timeline, observer_duration=20.0) == 22.0


def _take_with_waits(take_dir, waits):
    """A clean composed take whose timeline also records `waits` (a list of
    (time, duration, keyword))."""
    timeline = make_take(take_dir)
    for time_, duration, keyword in waits:
        timeline.add_event(
            {"type": "wait", "time": time_, "duration": duration, "keyword": keyword}
        )
    timeline.save(take_dir / "timeline.json")
    compose(take_dir)
    return verify(take_dir)


@requires_ffmpeg
def test_verify_flags_one_long_wait_as_a_dead_air_error(tmp_path):
    """(datakurre/collective.bpmproxy#15) Dead air is judged from the
    timeline: a single wait past DEAD_AIR_MAX_WAIT is a story that stood
    still on screen, and fails verify."""
    report = _take_with_waits(tmp_path / "take", [(1.0, 12.0, "Wait For Mail")])
    assert not report["ok"]
    finding = next(f for f in report["findings"] if f["check"] == "dead_air")
    assert finding["severity"] == "error"
    assert "Wait For Mail waited 12.0s" in finding["message"]
    assert report["longest_wait"] == 12.0


@requires_ffmpeg
def test_verify_accepts_the_waits_of_a_healthy_take(tmp_path):
    """Measured on real takes: the longest wait is about 2.4s and the total
    1-7s. Waits like that must never fail a take."""
    report = _take_with_waits(
        tmp_path / "take", [(0.5, 2.4, "Wait For Task"), (2.0, 1.1, "Sleep")]
    )
    assert not any(f["check"] == "dead_air" for f in report["findings"])
    assert report["ok"]
    assert report["waited_seconds"] == 3.5


@requires_ffmpeg
def test_verify_warns_when_waiting_adds_up_though_no_wait_is_long(tmp_path):
    report = _take_with_waits(
        tmp_path / "take", [(0.2 * i, 8.0, "Sleep") for i in range(4)]
    )  # 32s in total, none over the single-wait limit
    finding = next(f for f in report["findings"] if f["check"] == "dead_air")
    assert finding["severity"] == "warning"
    assert report["ok"]


@requires_ffmpeg
def test_verify_samples_each_turn_of_a_repeated_actor_at_its_own_midpoint(
    tmp_path, monkeypatch
):
    """(regression) The empty-inset check paired every turn_start with the
    *first* turn_end of the same actor, so a second turn's midpoint fell
    between its own start and its first turn's end -- before it started."""
    take_dir = tmp_path / "take"
    take_dir.mkdir()
    make_animated_clip(take_dir / "observer.webm", 8.0)
    make_animated_clip(take_dir / "reception-1.webm", 2.0)
    make_animated_clip(take_dir / "reception-2.webm", 2.0)
    timeline = Timeline.new("observer.webm")
    timeline.add_actor_clip("reception", "reception-1.webm", offset=1.0)
    timeline.add_event({"type": "turn_start", "time": 1.0, "actor": "reception"})
    timeline.add_event({"type": "turn_end", "time": 3.0, "actor": "reception"})
    timeline.add_actor_clip("reception", "reception-2.webm", offset=5.0)
    timeline.add_event({"type": "turn_start", "time": 5.0, "actor": "reception"})
    timeline.add_event({"type": "turn_end", "time": 7.0, "actor": "reception"})
    timeline.save(take_dir / "timeline.json")
    compose(take_dir)

    import screencast.verify as verify_module

    real_frame_luma_range = verify_module.frame_luma_range
    observer_samples = []

    def spy(video, at, *args, **kwargs):
        if video.name == "observer.webm":
            observer_samples.append(at)
        return real_frame_luma_range(video, at, *args, **kwargs)

    monkeypatch.setattr(verify_module, "frame_luma_range", spy)
    verify(take_dir)
    assert observer_samples == [pytest.approx(2.0), pytest.approx(6.0)]


@requires_ffmpeg
def test_verify_passes_a_clean_composed_take(tmp_path):
    take_dir = tmp_path / "take"
    make_take(take_dir)
    compose(take_dir)

    report = verify(take_dir)
    assert report["ok"], report["findings"]
    assert (take_dir / "contact-sheet.png").exists()


@requires_ffmpeg
def test_verify_flags_a_blank_composed_output(tmp_path):
    take_dir = tmp_path / "take"
    make_take(take_dir, with_turn=False)
    # Compose normally to get a correctly *sized* and *timed* file, then
    # replace its content with a near-black clip of the same duration --
    # this is what a font/rendering failure looks like: right shape, wrong
    # content.
    output = compose(take_dir)
    from screencast.compose import ffprobe_duration

    duration = ffprobe_duration(output)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=1920x1080:d={duration}:r=25",
            "-c:v",
            "libvpx-vp9",
            str(output),
        ],
        check=True,
    )

    report = verify(take_dir)
    assert not report["ok"]
    assert any(f["check"] == "blank_frame" for f in report["findings"])


@requires_ffmpeg
def test_verify_flags_a_white_turn_opening_frame_blackdetect_misses(tmp_path):
    """(regression, PR #14 review finding #10) blackdetect only catches
    near-black frames -- a turn's own opening frame is just as likely to be
    a plain white Plone loading page (or any other flat color a missing
    font renders as), which blackdetect cannot see at all. The "author"
    clip is a flat white color throughout; the observer stays animated,
    proving this check samples the actor's own clip directly rather than
    the composed output (whose every turn segment overlays a bordered
    observer inset that would otherwise mask a blank main view)."""
    take_dir = tmp_path / "take"
    take_dir.mkdir(parents=True, exist_ok=True)
    make_animated_clip(take_dir / "observer.webm", 4.0)
    make_clip(take_dir / "author.webm", 1.5, color="white")
    timeline = Timeline.new("observer.webm")
    timeline.add_actor_clip("author", "author.webm", offset=1.0, duration=1.5)
    timeline.add_event({"type": "turn_start", "time": 1.0, "actor": "author"})
    timeline.add_event({"type": "turn_end", "time": 2.5, "actor": "author"})
    timeline.save(take_dir / "timeline.json")
    output = compose(take_dir)

    # blackdetect itself genuinely finds nothing here -- confirms this is
    # the new uniform-color check catching it, not a coincidental overlap.
    assert detect_black_intervals(output) == []

    report = verify(take_dir)
    assert not report["ok"]
    assert any(
        f["check"] == "blank_frame" and "author" in f["message"]
        for f in report["findings"]
    )


@requires_ffmpeg
def test_verify_flags_wrong_frame_size(tmp_path):
    take_dir = tmp_path / "take"
    make_take(take_dir, with_turn=False)
    make_clip(take_dir / "output.webm", 4.0, size="640x360")

    report = verify(take_dir)
    assert not report["ok"]
    assert any(f["check"] == "stream" for f in report["findings"])


@requires_ffmpeg
def test_verify_flags_duration_mismatch(tmp_path):
    take_dir = tmp_path / "take"
    make_take(take_dir, with_turn=False)
    make_clip(take_dir / "output.webm", 10.0, size="1920x1080")

    report = verify(take_dir)
    assert not report["ok"]
    assert any(f["check"] == "duration" for f in report["findings"])


def test_parse_vtt_cue_times_reads_start_and_end_seconds(tmp_path):
    vtt_path = tmp_path / "output.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n1\n00:00:01.000 --> 00:00:03.500\nFirst\n\n"
        "2\n00:01:02.250 --> 00:01:04.000\nSecond\n"
    )
    assert parse_vtt_cue_times(vtt_path) == [(1.0, 3.5), (62.25, 64.0)]


@requires_ffmpeg
def test_verify_flags_a_caption_event_with_no_composed_vtt(tmp_path):
    """A caption on the timeline, but compose() either never ran again
    after it was added, or (hypothetically) failed to write the sidecar --
    either way this must not pass silently."""
    take_dir = tmp_path / "take"
    make_take(take_dir, with_turn=False)
    timeline = Timeline.load(take_dir / "timeline.json")
    timeline.add_event({"type": "caption", "time": 0.5, "text": "Hi", "duration": 1.0})
    timeline.save(take_dir / "timeline.json")
    output = compose(take_dir)
    output.with_suffix(".vtt").unlink(missing_ok=True)

    report = verify(take_dir)
    assert not report["ok"]
    assert any(f["check"] == "captions" for f in report["findings"])


@requires_ffmpeg
def test_verify_flags_a_caption_cue_past_the_outputs_own_duration(tmp_path):
    """(regression) A cue that was never correctly mapped past an inserted
    title card/hold would run past where the composed output actually
    ends -- this is what that looks like on disk, independent of whether
    compose() itself has a mapping bug."""
    take_dir = tmp_path / "take"
    make_take(take_dir, with_turn=False)
    output = compose(take_dir)

    from screencast.compose import ffprobe_duration

    duration = ffprobe_duration(output)
    output.with_suffix(".vtt").write_text(
        "WEBVTT\n\n1\n00:00:00.000 --> " + f"00:00:{duration + 5.0:06.3f}\nToo long\n"
    )
    timeline = Timeline.load(take_dir / "timeline.json")
    timeline.add_event(
        {"type": "caption", "time": 0.0, "text": "Too long", "duration": 5.0}
    )
    timeline.save(take_dir / "timeline.json")

    report = verify(take_dir)
    assert not report["ok"]
    assert any(f["check"] == "captions" for f in report["findings"])


@requires_ffmpeg
def test_verify_passes_a_take_with_a_correctly_mapped_caption(tmp_path):
    take_dir = tmp_path / "take"
    make_take(take_dir, with_turn=False)
    timeline = Timeline.load(take_dir / "timeline.json")
    timeline.add_event({"type": "caption", "time": 0.5, "text": "Hi", "duration": 1.0})
    timeline.save(take_dir / "timeline.json")
    compose(take_dir)

    report = verify(take_dir)
    assert report["ok"], report["findings"]
    assert not any(f["check"] == "captions" for f in report["findings"])


def test_contact_sheet_fps_spans_the_whole_clip_however_long():
    """(found reviewing a real 170 s take) A `max(..., 0.5)` floor made the
    sheet cover only the first 60 s of any longer take."""
    from screencast.verify import contact_sheet_fps

    for duration in (10, 60, 170, 900):
        fps = contact_sheet_fps(duration, rows=6, cols=5)
        assert 30 / fps == pytest.approx(duration)  # 30 frames span it all


def _take_with_deferred_turn_start(take_dir, flat_seconds):
    """An observer, plus one actor clip that is flat white for
    `flat_seconds` and then animated, whose turn_start is deferred 0.5s
    into the clip (the first page load)."""
    take_dir.mkdir(parents=True, exist_ok=True)
    make_animated_clip(take_dir / "observer.webm", 5.0)
    make_flat_then_animated_clip(take_dir / "author.webm", flat_seconds, 3.0)
    timeline = Timeline.new("observer.webm")
    timeline.add_actor_clip("author", "author.webm", offset=1.0, duration=3.0)
    timeline.add_event({"type": "turn_start", "time": 1.5, "actor": "author"})
    timeline.add_event({"type": "turn_end", "time": 4.0, "actor": "author"})
    timeline.save(take_dir / "timeline.json")
    compose(take_dir)


@requires_ffmpeg
def test_verify_ignores_a_blank_lead_in_the_composer_never_shows(tmp_path):
    """(regression, found by a live contact_form take) The clip is blank for
    its first 0.4s, but the composer enters it at turn_start, 0.5s in, so
    the viewer never sees that. Sampling a fixed 0.15s into the raw clip
    flagged a healthy take, whenever the page painted a little slower."""
    take_dir = tmp_path / "take"
    _take_with_deferred_turn_start(take_dir, flat_seconds=0.4)
    report = verify(take_dir)
    assert not any(f["check"] == "blank_frame" for f in report["findings"])


@requires_ffmpeg
def test_verify_still_flags_a_page_that_is_flat_at_the_turn_start(tmp_path):
    """The other half: a page still blank *after* turn_start is exactly what
    the check is for."""
    take_dir = tmp_path / "take"
    _take_with_deferred_turn_start(take_dir, flat_seconds=1.5)
    report = verify(take_dir)
    assert any(
        f["check"] == "blank_frame" and "author" in f["message"]
        for f in report["findings"]
    )
