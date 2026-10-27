"""The pure dashboard view model (TASK-010, ADR-008): formatting and every text-level AC."""

import dataclasses
import logging
import re
import string
from datetime import UTC, datetime, timedelta, timezone

import pytest

from pc_handoff.detection.provider import ConnectionType
from pc_handoff.domain.models import PcStatus
from pc_handoff.persistence import config_store
from pc_handoff.persistence.paths import DataPaths
from pc_handoff.ui import view_model as vm
from pc_handoff.ui.view_model import (
    ACTIVE_COLUMNS,
    DISCONNECTED_COLUMNS,
    ActiveRow,
    DisconnectedRow,
    NoticeItem,
    buildViewModel,
    configWarningLine,
    displayOrUnknown,
    escapeMarkdown,
    formatDuration,
    formatTime,
    sanitize,
    utcOffsetLabel,
    visibleNotices,
)
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.builders import trackedActive, trackedDisconnected
from tests.fakes.dashboard_snapshots import healthySnapshot, notice, unknownSnapshot

C = FAKE_CLOCK_START  # alice's connect time / usagePeriodStart
JST = timezone(timedelta(hours=9))


def local(dt: datetime) -> str:
    """The expected display of ``dt``, independent of the test host's time zone."""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def alice(**overrides):
    fields = {
        "connectTime": C,
        "startEventTimestamp": C,
        "clientName": "ALICE-LAPTOP",
        "clientAddress": "192.168.1.20",
    }
    fields.update(overrides)
    return trackedActive(2, "alice", **fields)


# --- formatting helpers -------------------------------------------------------------------


class TestFormatDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "00:00:00"),
            (5, "00:00:05"),
            (65, "00:01:05"),
            (3725, "01:02:05"),
            (86399, "23:59:59"),
            (97389, "27:03:09"),  # hours are not capped at 24 (ADR-008)
            (360000, "100:00:00"),
        ],
    )
    def test_hh_mm_ss_with_unbounded_hours(self, seconds, expected):
        assert formatDuration(seconds) == expected

    def test_a_negative_duration_shows_zero(self):
        # A backward wall-clock step can put now before the period start.
        assert formatDuration(-30) == "00:00:00"

    def test_fractional_seconds_are_truncated(self):
        assert formatDuration(59.9) == "00:00:59"


class TestFormatTime:
    def test_local_time_with_seconds_precision(self):
        dt = datetime(2026, 10, 21, 9, 5, 7, 999999, tzinfo=JST)
        assert formatTime(dt) == local(dt.replace(microsecond=0))
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", formatTime(dt))

    def test_converts_to_the_host_time_zone(self):
        utc = datetime(2026, 10, 21, 0, 0, 0, tzinfo=UTC)
        assert formatTime(utc) == utc.astimezone().strftime("%Y-%m-%d %H:%M:%S")

    @pytest.mark.parametrize(
        ("zone", "expected"),
        [
            (timezone(timedelta(hours=9)), "2026-10-21 09:00:00"),
            (timezone(timedelta(hours=-5, minutes=-30)), "2026-10-20 18:30:00"),
            (UTC, "2026-10-21 00:00:00"),
        ],
    )
    def test_converts_to_the_local_zone_whatever_the_host(self, monkeypatch, zone, expected):
        # Critic TASK-010 N6: pin the local zone, so the conversion is checked even on a
        # host whose own zone would make a missing conversion invisible.
        monkeypatch.setattr(vm, "localZone", lambda: zone)
        utc = datetime(2026, 10, 21, 0, 0, 0, tzinfo=UTC)
        assert formatTime(utc) == expected
        assert formatTime(utc.astimezone(JST)) == expected

    @pytest.mark.parametrize(
        ("zone", "label"),
        [
            (timezone(timedelta(hours=9)), "UTC+09:00"),
            (timezone(timedelta(hours=-5, minutes=-30)), "UTC-05:30"),
        ],
    )
    def test_the_footer_names_the_local_offset_whatever_the_host(self, monkeypatch, zone, label):
        monkeypatch.setattr(vm, "localZone", lambda: zone)
        model = buildViewModel(healthySnapshot([], C), C)
        assert model.footer == f"Times are shown in this PC's local time ({label})."
        assert model.lastUpdated == C.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")

    def test_the_default_local_zone_is_the_host_zone(self):
        assert vm.localZone() is None  # datetime.astimezone(None) = the host's zone

    def test_utc_offset_label(self):
        assert utcOffsetLabel(timedelta(hours=9)) == "UTC+09:00"
        assert utcOffsetLabel(timedelta(hours=-5, minutes=-30)) == "UTC-05:30"
        assert utcOffsetLabel(timedelta(0)) == "UTC+00:00"
        assert utcOffsetLabel(None) == "UTC"


class TestSanitizeAndUnknown:
    def test_control_characters_are_removed(self):
        raw = "a\x00b\x07c\x1fd\x7fe\tf\ng\rh"
        assert sanitize(raw) == "abcdefgh"

    def test_printable_and_non_ascii_text_is_kept(self):
        assert sanitize("**bob**<script>x</script> 山田 é") == "**bob**<script>x</script> 山田 é"

    @pytest.mark.parametrize(
        "char",
        [
            "‮",  # RIGHT-TO-LEFT OVERRIDE
            "‪",  # LEFT-TO-RIGHT EMBEDDING
            "​",  # ZERO WIDTH SPACE
            "‎",  # LEFT-TO-RIGHT MARK
            "⁦",  # LEFT-TO-RIGHT ISOLATE
            "⁧",  # RIGHT-TO-LEFT ISOLATE
            "⁨",  # FIRST STRONG ISOLATE
            "⁩",  # POP DIRECTIONAL ISOLATE
            "",  # NEXT LINE (C1 control)
            "",  # CONTROL SEQUENCE INTRODUCER (C1 control)
            "﻿",  # ZERO WIDTH NO-BREAK SPACE / BOM
            "­",  # SOFT HYPHEN
        ],
    )
    def test_invisible_unicode_controls_are_removed(self, char):
        # Critic TASK-010 N1: an RDP client picks its own client name; bidi overrides and
        # zero-width characters could make it display as another name or IP.
        assert sanitize(f"PC{char}-1") == "PC-1"

    def test_a_bidi_spoofed_client_name_shows_its_real_character_order(self):
        assert sanitize("ALICE-‮GOTPAL") == "ALICE-GOTPAL"

    @pytest.mark.parametrize(
        "text",
        [
            "山田太郎",
            "やまだ たろう",
            "ﾔﾏﾀﾞ",
            "José Müller-Åström",
            "Ελένη",
            "Владимир",
            "محمد",
            "שלום",
            "😀 🎉",
            "é",  # combining accent (Mn) is kept
            "a b",  # no-break space (Zs) is kept
        ],
    )
    def test_names_in_any_script_are_unchanged(self, text):
        assert sanitize(text) == text

    @pytest.mark.parametrize("value", [None, "", "   ", "\x07"])
    def test_missing_values_show_unknown(self, value):
        assert displayOrUnknown(value) == "Unknown"

    def test_present_values_are_sanitized(self):
        assert displayOrUnknown("PC\x07-1") == "PC-1"


class TestEscapeMarkdown:
    def test_every_ascii_punctuation_character_is_backslash_escaped(self):
        # Superset of the spec list (\`*_{}[]()#+-.!|<>~): also $ (LaTeX), : (emoji and
        # colour directives), & (HTML entities) and the rest of ASCII punctuation.
        for char in string.punctuation:
            assert escapeMarkdown(char) == "\\" + char

    def test_spec_characters_are_escaped(self):
        for char in "\\`*_{}[]()#+-.!|<>~":
            assert escapeMarkdown(f"a{char}b") == f"a\\{char}b"

    def test_letters_digits_spaces_and_non_ascii_are_unchanged(self):
        assert escapeMarkdown("alice 42 山田") == "alice 42 山田"

    def test_unescaping_restores_the_literal_text(self):
        raw = "**bold** [link](javascript:alert(1)) `code` <script>x</script> $x$ :red[a]"
        assert re.sub(r"\\(.)", r"\1", escapeMarkdown(raw)) == raw
        assert "**" not in escapeMarkdown(raw)


# --- REQ AC scenarios ---------------------------------------------------------------------


class TestAc1InUseWithLiveDuration:
    def test_badge_and_row(self):
        now = C + timedelta(seconds=3725)
        model = buildViewModel(healthySnapshot([alice()], now - timedelta(seconds=3)), now)
        assert (model.badgeText, model.badgeKind) == ("🔴 In Use — alice (192.168.1.20)", "error")
        assert model.unavailableBanner is None
        assert model.lastKnownLine is None
        assert model.activeHeading == "Active sessions"
        assert model.activeRows == (
            ActiveRow("alice", "RDP", "ALICE-LAPTOP", "192.168.1.20", local(C), "01:02:05"),
        )
        assert model.emptyActiveMessage is None
        assert model.disconnectedRows == ()

    def test_the_duration_ticks_with_now(self):
        snapshot = healthySnapshot([alice()], C)
        first = buildViewModel(snapshot, C + timedelta(seconds=10))
        later = buildViewModel(snapshot, C + timedelta(seconds=20))
        assert [r.duration for r in (first.activeRows[0], later.activeRows[0])] == [
            "00:00:10",
            "00:00:20",
        ]

    def test_row_cells_follow_the_column_order(self):
        row = ActiveRow("u", "RDP", "c", "ip", "start", "dur")
        assert ACTIVE_COLUMNS == (
            "User",
            "Connection type",
            "Client name",
            "Client IP",
            "Usage period start",
            "Duration",
        )
        assert row.cells() == ("u", "RDP", "c", "ip", "start", "dur")
        assert DISCONNECTED_COLUMNS == ("User", "Disconnected since")
        assert DisconnectedRow("bob", "x").cells() == ("bob", "x")


class TestAc2EmptyState:
    def test_available_with_the_empty_message_and_no_disconnected_section(self):
        model = buildViewModel(healthySnapshot([], C), C + timedelta(seconds=1))
        assert (model.badgeText, model.badgeKind) == ("🟢 Available", "success")
        assert model.activeRows == ()
        assert model.emptyActiveMessage == "No active user sessions"
        assert model.disconnectedRows == ()
        assert model.unavailableBanner is None


class TestAc3DisconnectedListedSeparately:
    D = C + timedelta(minutes=30)

    def sessions(self):
        return [
            trackedDisconnected(2, "alice", disconnectedSince=self.D),
            trackedDisconnected(3, "bob", disconnectedSince=self.D + timedelta(minutes=5)),
        ]

    def test_disconnected_sessions_do_not_count_as_active(self):
        model = buildViewModel(healthySnapshot(self.sessions(), self.D), self.D)
        assert model.badgeText == "🟢 Available"
        assert model.emptyActiveMessage == "No active user sessions"
        assert model.disconnectedHeading == "Logged on but disconnected (not in use)"
        assert model.disconnectedRows == (
            DisconnectedRow("alice", local(self.D)),
            DisconnectedRow("bob", local(self.D + timedelta(minutes=5))),
        )

    def test_variant_with_carol_active(self):
        carol = trackedActive(4, "carol", connectTime=C, startEventTimestamp=C)
        model = buildViewModel(healthySnapshot([*self.sessions(), carol], self.D), self.D)
        assert (model.badgeText, model.badgeKind) == ("🔴 In Use — carol (192.168.1.20)", "error")
        assert [r.user for r in model.activeRows] == ["carol"]
        assert model.emptyActiveMessage is None
        assert [r.user for r in model.disconnectedRows] == ["alice", "bob"]

    def test_a_null_disconnected_since_shows_unknown(self):
        # TASK-005 F-note / TASK-009 forward constraint: the schema accepts null here.
        bob = dataclasses.replace(
            trackedDisconnected(3, "bob", disconnectedSince=self.D), disconnectedSince=None
        )
        model = buildViewModel(healthySnapshot([bob], self.D), self.D)
        assert model.disconnectedRows == (DisconnectedRow("bob", "unknown"),)


class TestAc4UnknownFieldsAndApproximateDurations:
    def test_console_session_without_client_information(self):
        erin = trackedActive(
            1,
            "erin",
            connectTime=C,
            startEventTimestamp=C,
            connectionType=ConnectionType.CONSOLE,
            clientName=None,
            clientAddress=None,
        )
        model = buildViewModel(healthySnapshot([erin], C), C + timedelta(seconds=61))
        assert model.badgeText == "🔴 In Use — erin (N/A)"
        [row] = model.activeRows
        assert (row.user, row.connectionType, row.clientName, row.clientIp) == (
            "erin",
            "Console",
            "N/A",
            "N/A",
        )
        assert row.duration == "00:01:01"

    def test_an_empty_client_name_also_shows_unknown(self):
        model = buildViewModel(healthySnapshot([alice(clientName="", clientAddress="")], C), C)
        assert (model.activeRows[0].clientName, model.activeRows[0].clientIp) == (
            "Unknown",
            "Unknown",
        )

    def test_an_approximate_duration_is_computed_from_t1_and_marked(self):
        t1 = C + timedelta(minutes=10)
        approx = trackedActive(3, "dave", connectTime=None, startEventTimestamp=t1)
        model = buildViewModel(healthySnapshot([approx], t1), t1 + timedelta(seconds=90))
        [row] = model.activeRows
        assert row.periodStart == local(t1)
        assert row.duration == "00:01:30 (approx.)"
        assert row.duration.endswith(" (approx.)")


class TestAc5UnknownStateIsFrozen:
    P = C + timedelta(hours=1, minutes=2, seconds=3)

    def snapshot(self):
        return unknownSnapshot(
            [alice()],
            self.P,
            lastKnownStatus=PcStatus.IN_USE,
            failedAt=self.P + timedelta(seconds=5),
        )

    def test_banner_last_known_line_and_heading(self):
        model = buildViewModel(self.snapshot(), self.P + timedelta(seconds=600))
        assert (model.badgeText, model.badgeKind) == ("Unknown", "neutral")
        assert model.unavailableBanner == "Session information is currently unavailable"
        assert model.lastKnownLine == f"Last known status: In Use at {local(self.P)}"
        assert model.activeHeading == f"Last known sessions (as of {local(self.P)})"

    def test_durations_are_frozen_at_the_last_successful_poll(self):
        frozen = formatDuration(int((self.P - C).total_seconds()))
        assert frozen == "01:02:03"
        for later in (600, 900):
            model = buildViewModel(self.snapshot(), self.P + timedelta(seconds=later))
            assert model.activeRows[0].duration == frozen

    def test_approximate_rows_stay_marked_while_frozen(self):
        approx = trackedActive(3, "dave", connectTime=None, startEventTimestamp=C)
        snapshot = unknownSnapshot([approx], self.P, lastKnownStatus=PcStatus.IN_USE)
        model = buildViewModel(snapshot, self.P + timedelta(seconds=600))
        assert model.activeRows[0].duration == "01:02:03 (approx.)"

    def test_last_known_disconnected_sessions_stay_listed(self):
        bob = trackedDisconnected(3, "bob", disconnectedSince=C)
        snapshot = unknownSnapshot([alice(), bob], self.P, lastKnownStatus=PcStatus.IN_USE)
        model = buildViewModel(snapshot, self.P + timedelta(seconds=600))
        assert model.disconnectedRows == (DisconnectedRow("bob", local(C)),)


class TestAc6FreshInstallUnknown:
    def test_last_known_status_none_and_no_sessions(self):
        model = buildViewModel(unknownSnapshot([], None, failedAt=C), C)
        assert model.badgeText == "Unknown"
        assert model.unavailableBanner == "Session information is currently unavailable"
        assert model.lastKnownLine == "Last known status: none"
        assert model.activeRows == ()
        assert model.emptyActiveMessage == "No active user sessions"
        assert model.disconnectedRows == ()

    def test_unknown_without_a_last_successful_poll_cannot_freeze_durations(self):
        # Defensive: sessions without P (not produced by the tracker) show no ticking value.
        model = buildViewModel(unknownSnapshot([alice()], None, failedAt=C), C)
        assert model.activeHeading == "Last known sessions"
        assert model.activeRows[0].duration == "Unknown"

    def test_a_last_known_status_without_its_time_shows_unknown_time(self):
        snapshot = dataclasses.replace(
            unknownSnapshot([], None, failedAt=C), lastKnownStatus=PcStatus.AVAILABLE
        )
        model = buildViewModel(snapshot, C)
        assert model.lastKnownLine == "Last known status: Available at Unknown"


class TestAc7Recovery:
    def test_a_healthy_snapshot_has_no_banner(self):
        p = C + timedelta(minutes=5)
        before = buildViewModel(unknownSnapshot([alice()], p, lastKnownStatus=PcStatus.IN_USE), p)
        after = buildViewModel(healthySnapshot([alice()], p + timedelta(seconds=5)), p)
        assert before.unavailableBanner is not None
        assert (after.unavailableBanner, after.lastKnownLine) == (None, None)
        assert after.badgeText == "🔴 In Use — alice (192.168.1.20)"
        assert after.activeHeading == "Active sessions"


class TestAc8WarningsAndNotices:
    WARNINGS = (
        "dashboardRefreshIntervalSeconds: 2 out of range 5-300; default 10 applied",
        "sessionPollIntervalSeconds: wrong type (expected integer); default 5 applied",
        "dashboardPort: 80 out of range 1024-65535; default 8501 applied",
    )

    def test_each_config_warning_names_its_field_and_default(self):
        model = buildViewModel(healthySnapshot([], C, configWarnings=self.WARNINGS), C)
        assert model.configWarningLines == tuple(
            f"Configuration (config.json): {w}" for w in self.WARNINGS
        )
        for line, (field, default) in zip(
            model.configWarningLines,
            [
                ("dashboardRefreshIntervalSeconds", 10),
                ("sessionPollIntervalSeconds", 5),
                ("dashboardPort", 8501),
            ],
            strict=True,
        ):
            assert field in line and f"default {default} applied" in line

    def test_a_warning_that_names_config_json_does_not_name_it_twice(self):
        # Critic TASK-010 N3: config_store's unreadable-file warning starts with the name.
        line = configWarningLine("config.json: could not be read; defaults applied")
        assert line == "Configuration: config.json: could not be read; defaults applied"
        assert line.count("config.json") == 1

    def test_the_real_unreadable_config_warning_names_the_file_once(
        self, tmp_path, monkeypatch, fakeClock
    ):
        def unreadable(path):
            raise PermissionError("denied")

        monkeypatch.setattr(config_store, "readBytes", unreadable)
        result = config_store.loadConfig(
            DataPaths(tmp_path), fakeClock, logging.getLogger("test.view_model")
        )
        [warning] = result.warnings
        model = buildViewModel(healthySnapshot([], C, configWarnings=result.warnings), C)
        assert model.configWarningLines == (f"Configuration: {warning}",)
        assert model.configWarningLines[0].count("config.json") == 1

    def test_config_warning_text_is_sanitized(self):
        assert configWarningLine("foo\x07\n: unknown setting ignored") == (
            "Configuration (config.json): foo: unknown setting ignored"
        )

    def test_notices_keep_their_ids_and_order(self):
        first = notice("Status data was reset (backup: status.json.corrupt-20261021T091502)")
        second = notice(
            "activity_log.json was corrupt and has been reset "
            "(backup: activity_log.json.corrupt-20261021T091502)",
            fileName="activity_log.json",
        )
        model = buildViewModel(healthySnapshot([], C, notices=[first, second]), C)
        assert model.noticeItems == (
            NoticeItem(first.noticeId, first.text),
            NoticeItem(second.noticeId, second.text),
        )

    def test_notice_text_is_sanitized(self):
        model = buildViewModel(healthySnapshot([], C, notices=[notice("a\x1bb")]), C)
        assert model.noticeItems[0].text == "ab"

    def test_visible_notices_skip_the_dismissed_ids(self):
        items = (NoticeItem("n1", "one"), NoticeItem("n2", "two"))
        assert visibleNotices(items, {"n1"}) == (NoticeItem("n2", "two"),)
        assert visibleNotices(items, set()) == items
        assert visibleNotices(items, None) == items


class TestLastUpdatedAndFooter:
    def test_last_updated_is_now_with_the_snapshot_time_alongside(self):
        snapshot = healthySnapshot([], C)
        model = buildViewModel(snapshot, C + timedelta(seconds=10))
        assert model.lastUpdated == local(C + timedelta(seconds=10))
        assert model.dataUpdatedAt == local(C)
        assert model.lastUpdatedLine == (
            f"Last updated: {local(C + timedelta(seconds=10))} (data as of {local(C)})"
        )

    def test_last_updated_advances_even_while_unknown(self):
        snapshot = unknownSnapshot([], None, failedAt=C)
        assert (
            buildViewModel(snapshot, C).lastUpdated
            != buildViewModel(snapshot, C + timedelta(seconds=10)).lastUpdated
        )

    def test_footer_names_the_utc_offset(self):
        model = buildViewModel(healthySnapshot([], C), C)
        offset = utcOffsetLabel(C.astimezone().utcoffset())
        assert model.footer == f"Times are shown in this PC's local time ({offset})."


class TestSafeText:
    def test_user_derived_fields_are_sanitized_but_otherwise_literal(self):
        evil = trackedActive(
            5,
            "**bob**<script>x</script>",
            connectTime=C,
            startEventTimestamp=C,
            clientName="[link](javascript:alert(1))\x07`tick`",
            clientAddress="10.0.0.1\x00",
        )
        bob = trackedDisconnected(6, "_eve_\x1b", disconnectedSince=C)
        model = buildViewModel(healthySnapshot([evil, bob], C), C)
        [row] = model.activeRows
        assert row.user == "**bob**<script>x</script>"
        assert row.clientName == "[link](javascript:alert(1))`tick`"
        assert row.clientIp == "10.0.0.1"
        assert model.disconnectedRows[0].user == "_eve_"

    def test_an_active_session_without_an_open_period_shows_unknown(self):
        # Defensive: the tracker never publishes this (StatusSession refuses it).
        broken = dataclasses.replace(alice(), openPeriod=None)
        model = buildViewModel(healthySnapshot([broken], C), C)
        [row] = model.activeRows
        assert (row.periodStart, row.duration) == ("Unknown", "Unknown")

    def test_the_model_is_immutable(self):
        model = buildViewModel(healthySnapshot([], C), C)
        with pytest.raises(dataclasses.FrozenInstanceError):
            model.badgeText = "x"  # type: ignore[misc]

    def test_module_exposes_the_fixed_texts(self):
        assert vm.UNAVAILABLE_BANNER == "Session information is currently unavailable"
        assert vm.EMPTY_ACTIVE_MESSAGE == "No active user sessions"
        assert vm.DISCONNECTED_HEADING == "Logged on but disconnected (not in use)"


class TestStatusRowColumnWeights:
    def test_weights_hug_content_and_adjust_for_status_length(self):
        wAvailable = vm.statusRowColumnWeights(
            "🟢 Available", "⏳ Next in queue: None", "🕒 Est. wait: Ready now (~0 min)"
        )
        wInUse = vm.statusRowColumnWeights(
            "🔴 In Use — alice (192.168.1.20)",
            "⏳ Next in queue: None",
            "🕒 Est. wait: Ready now (~0 min)",
        )
        # In Use column is significantly wider than Available column
        assert wInUse[0] > wAvailable[0]
        # Spacer column absorbs remaining room
        assert wAvailable[3] > wInUse[3]
        # 4 elements: status, queue, wait, spacer
        assert len(wAvailable) == 4
        assert len(wInUse) == 4
