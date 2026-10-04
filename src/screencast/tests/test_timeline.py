from screencast.timeline import Timeline
from screencast.timeline import TimelineError
from screencast.timeline import VERSION
import pytest


def make_timeline():
    timeline = Timeline.new("observer.webm")
    timeline.add_actor_clip("author", "turn-01-author.webm", offset=1.2, duration=10.0)
    timeline.add_event({"type": "turn_start", "time": 1.2, "actor": "author"})
    timeline.add_event(
        {
            "type": "chapter",
            "time": 1.2,
            "eyebrow": "Story · 1/1",
            "title": "Author",
            "subtitle": "Doing a thing",
            "duration": 8.0,
        }
    )
    timeline.add_event({"type": "focus", "time": 9.2, "view": "actor"})
    timeline.add_event({"type": "turn_end", "time": 11.2, "actor": "author"})
    timeline.add_event({"type": "hold", "time": 11.2, "duration": 5.0})
    return timeline


def test_valid_timeline_round_trips_through_disk(tmp_path):
    timeline = make_timeline()
    path = timeline.save(tmp_path / "timeline.json")
    loaded = Timeline.load(path)
    assert loaded.data == timeline.data


def test_events_of_filters_by_type():
    timeline = make_timeline()
    assert [event["type"] for event in timeline.events_of("focus")] == ["focus"]


def test_actor_clip_looks_up_by_name():
    timeline = make_timeline()
    assert timeline.actor_clip("author")["video"] == "turn-01-author.webm"
    with pytest.raises(TimelineError):
        timeline.actor_clip("nobody")


def test_add_track_clip_omits_focusable_and_fade_at_their_defaults():
    """Same convention add_actor_clip's `duration` already follows: a field
    left at its default is omitted rather than written out explicitly, so
    a timeline that never asked for either reads exactly as it did before
    these fields existed."""
    timeline = Timeline.new("observer.webm")
    clip = timeline.add_track_clip("terminal", "terminal.webm", offset=0.0)
    assert "focusable" not in clip
    assert "fade" not in clip
    assert "scale" not in clip
    assert "margin" not in clip
    assert "border" not in clip


def test_add_track_clip_records_focusable_false_and_fade_true():
    timeline = Timeline.new("observer.webm")
    clip = timeline.add_track_clip(
        "terminal", "terminal.webm", offset=0.0, focusable=False, fade=True
    )
    assert clip["focusable"] is False
    assert clip["fade"] is True
    assert timeline.tracks[0] is clip


def test_add_track_clip_records_an_explicit_scale_margin_and_border():
    timeline = Timeline.new("observer.webm")
    clip = timeline.add_track_clip(
        "terminal", "terminal.webm", offset=0.0, scale=0.6, margin=12, border=5
    )
    assert clip["scale"] == 0.6
    assert clip["margin"] == 12
    assert clip["border"] == 5


def test_add_track_clip_records_an_explicit_corner():
    timeline = Timeline.new("observer.webm")
    clip = timeline.add_track_clip(
        "terminal", "terminal.webm", offset=0.0, corner="top-left"
    )
    assert clip["corner"] == "top-left"


def test_add_track_clip_omits_corner_at_its_default():
    timeline = Timeline.new("observer.webm")
    clip = timeline.add_track_clip("terminal", "terminal.webm", offset=0.0)
    assert "corner" not in clip


def test_add_track_clip_rejects_a_name_already_used_on_this_timeline():
    """Focus(view=name) and compose()'s own track_defs_by_name both resolve
    a track by name alone -- two clips sharing one would make either
    unreachable or silently merge, so a reused name is rejected outright,
    even once the earlier track has already closed."""
    timeline = Timeline.new("observer.webm")
    timeline.add_track_clip("terminal", "terminal-1.webm", offset=0.0)
    with pytest.raises(TimelineError):
        timeline.add_track_clip("terminal", "terminal-2.webm", offset=20.0)


def test_wrong_version_is_rejected():
    data = make_timeline().data
    data["version"] = 1
    with pytest.raises(TimelineError):
        Timeline(data)


def test_unknown_top_level_field_is_rejected():
    data = make_timeline().data
    data["bogus"] = True
    with pytest.raises(TimelineError):
        Timeline(data)


def test_unknown_event_type_is_rejected():
    data = make_timeline().data
    data["events"].append({"type": "wipe", "time": 0})
    with pytest.raises(TimelineError):
        Timeline(data)


def test_negative_offset_is_rejected():
    data = make_timeline().data
    data["actors"][0]["offset"] = -1
    with pytest.raises(TimelineError):
        Timeline(data)


def test_current_version_constant_matches_schema():
    assert VERSION == 2
