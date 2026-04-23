#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import bisect
import datetime as dt
import json
import logging
import os
import queue
import re
import signal
import socket
import threading
import time
import tkinter as tk
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import ttk
from typing import Any, Optional

from aiohttp import web
from openpyxl import load_workbook
import websockets


CR = "\r"
CR_BYTES = b"\r"
DEFAULT_CROSSMGR_PORT = 8765
DEFAULT_JCHIP_PORT = 53136
DEFAULT_WEB_PORT = 8865
RECENT_WINDOW_SECONDS = 10.0
DISTANCE_TO_FINISH_METERS = 50.0
MAX_PASSINGS = 500
GROUP_GAP_SECONDS = 2.0
MAX_GROUPS = 200
GROUP_MAX_AGE_SECONDS = 30.0
LOCAL_READ_DEDUP_SECONDS = 8.0
RE_TIME = re.compile(r"^\d\d:\d\d:\d\d\.\d+")
RE_SPEED = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(km/h|kph|mph)?", re.I)
STATUS_TIMEOUTS = {
    "rfid": 15.0,
    "announcer": 15.0,
    "lapcounter": 15.0,
}


def ordinal(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def format_clock(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    total = int(seconds)
    frac = int(round((seconds - total) * 100))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{sign}{h}:{m:02d}:{s:02d}"
    if frac:
        return f"{sign}{m}:{s:02d}.{frac:02d}"
    return f"{sign}{m}:{s:02d}"


def format_eta(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    return format_clock(seconds)


def format_elapsed_hms(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def parse_speed_mps(value: Any) -> Optional[float]:
    if not value:
        return None
    match = RE_SPEED.search(str(value))
    if not match:
        return None
    speed = float(match.group(1))
    unit = (match.group(2) or "km/h").lower()
    if unit in {"km/h", "kph"}:
        return speed / 3.6
    if unit == "mph":
        return speed * 0.44704
    return None


def as_utc_iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.isoformat()


def parse_host_port(value: str, default_port: int) -> tuple[str, int]:
    if ":" not in value:
        return value, default_port
    host, port = value.rsplit(":", 1)
    return host, int(port)


@dataclass(slots=True)
class RiderInfo:
    bib: str
    first_name: str = ""
    last_name: str = ""
    team: str = ""
    category: str = ""
    tag: str = ""

    @property
    def full_name(self) -> str:
        return " ".join(p for p in [self.first_name, self.last_name] if p).strip() or self.bib


@dataclass(slots=True)
class CategoryConfig:
    name: str
    numbers: str = ""
    race_laps: Optional[int] = None
    race_distance: Optional[float] = None
    start_offset: str = ""
    gender: str = ""
    category_type: str = ""

    def merge_live(self, detail: dict[str, Any]) -> "CategoryConfig":
        return CategoryConfig(
            name=str(detail.get("name") or self.name or "").strip(),
            numbers=self.numbers,
            race_laps=int(detail["laps"]) if detail.get("laps") not in (None, "") else self.race_laps,
            race_distance=float(detail["raceDistance"]) if detail.get("raceDistance") not in (None, "") else self.race_distance,
            start_offset=str(detail.get("startOffset") if detail.get("startOffset") not in (None, "") else self.start_offset),
            gender=str(detail.get("gender") or self.gender or "").strip(),
            category_type=str(detail.get("catType") or self.category_type or "").strip(),
        )


@dataclass(slots=True)
class PassingRecord:
    bib: str
    tag: str
    seen_at: dt.datetime
    received_at: dt.datetime
    race_time: Optional[float]
    note: str
    gap: str
    lap: Optional[int]
    name: str
    category: str
    team: str
    eta_seconds: Optional[float]
    category_rank: Optional[int]
    reader: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "bib": self.bib,
            "tag": self.tag,
            "seen_at": as_utc_iso(self.seen_at),
            "received_at": as_utc_iso(self.received_at),
            "time": format_clock(self.race_time),
            "note": self.note,
            "gap": self.gap,
            "lap": self.lap,
            "name": self.name,
            "category": self.category,
            "team": self.team,
            "eta": format_eta(self.eta_seconds),
            "category_rank": self.category_rank,
            "reader": self.reader,
        }


@dataclass(slots=True)
class SubsystemStatus:
    name: str
    endpoint: str = ""
    state: str = "waiting"
    updated_at: Optional[dt.datetime] = None
    detail: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "endpoint": self.endpoint,
            "state": self.state,
            "updated_at": as_utc_iso(self.updated_at) if self.updated_at else "",
            "detail": self.detail,
        }


@dataclass(slots=True)
class RiderRuntimeState:
    bib: str
    predicted_lap: Optional[int] = None
    last_local_read_race_time: Optional[float] = None
    last_local_read_seen_at: Optional[dt.datetime] = None
    visible_in_results: bool = True
    category_hint: str = ""


GROUP_TREE_HEIGHT = 14


class RaceConfig:
    def __init__(self) -> None:
        self.event_name = ""
        self.event_date = ""
        self.timezone = ""
        self.categories: dict[str, CategoryConfig] = {}
        self.riders_by_tag: dict[str, RiderInfo] = {}
        self.riders_by_bib: dict[str, RiderInfo] = {}

    @classmethod
    def load(cls, xlsx_path: Path) -> "RaceConfig":
        wb = load_workbook(xlsx_path, data_only=True, read_only=True)
        cfg = cls()

        ws = wb["Registration"]
        headers = [str(v).strip() if v is not None else "" for v in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
        for row in ws.iter_rows(min_row=2, values_only=True):
            rec = dict(zip(headers, row))
            bib = str(rec.get("Bib#") or "").strip()
            tag = str(rec.get("Tag") or "").strip().upper()
            if not bib:
                continue
            rider = RiderInfo(
                bib=bib,
                first_name=str(rec.get("FirstName") or "").strip(),
                last_name=str(rec.get("LastName") or "").strip(),
                team=str(rec.get("Team") or "").strip(),
                category=str(rec.get("Category") or "").strip(),
                tag=tag,
            )
            cfg.riders_by_bib[bib] = rider
            if tag:
                cfg.riders_by_tag[tag] = rider

        ws = wb["--CrossMgr-Categories"]
        headers = [str(v).strip() if v is not None else "" for v in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
        for row in ws.iter_rows(min_row=2, values_only=True):
            rec = dict(zip(headers, row))
            name = str(rec.get("Name") or "").strip()
            if not name:
                continue
            cfg.categories[name] = CategoryConfig(
                name=name,
                numbers=str(rec.get("Numbers") or "").strip(),
                race_laps=int(rec["Race Laps"]) if rec.get("Race Laps") not in (None, "") else None,
                race_distance=float(rec["Race Distance"]) if rec.get("Race Distance") not in (None, "") else None,
                start_offset=str(rec.get("Start Offset") or "").strip(),
                gender=str(rec.get("Gender") or "").strip(),
                category_type=str(rec.get("Category Type") or "").strip(),
            )

        ws = wb["--CrossMgr-Properties"]
        headers = [str(v).strip() if v is not None else "" for v in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
        row = next(ws.iter_rows(min_row=2, max_row=2, values_only=True), None)
        if row:
            rec = dict(zip(headers, row))
            cfg.event_name = str(rec.get("Event Name") or "").strip()
            cfg.event_date = str(rec.get("Event Date") or "").strip()
            cfg.timezone = str(rec.get("TimeZone") or "").strip()

        return cfg


class SharedState:
    def __init__(self, config: RaceConfig, show_unknown_tags: bool = False) -> None:
        self.config = config
        self.show_unknown_tags = show_unknown_tags
        self.lock = threading.RLock()
        self.passings: deque[PassingRecord] = deque(maxlen=MAX_PASSINGS)
        self.results_by_bib: dict[str, dict[str, Any]] = {}
        self.rider_runtime: dict[str, RiderRuntimeState] = {}
        self.category_details: dict[str, dict[str, Any]] = {}
        self.latest_lap_refresh: dict[str, Any] = {}
        self.reference: dict[str, Any] = {}
        self._last_version_count: Optional[int] = None
        self._last_cur_race_time: Optional[float] = None
        self.reader_status: dict[str, str] = {}
        self.reader_clock_diff: dict[str, float] = {}
        self.race_clock_wall: Optional[float] = None
        self.race_clock_value: Optional[float] = None
        self.last_announcer_update: Optional[dt.datetime] = None
        self.last_lapcounter_update: Optional[dt.datetime] = None
        self.started_at = dt.datetime.now().astimezone()
        self.subsystems: dict[str, SubsystemStatus] = {
            "rfid": SubsystemStatus(name="RFID", endpoint="JChip :53136"),
            "announcer": SubsystemStatus(name="Announcer"),
            "lapcounter": SubsystemStatus(name="LapCounter"),
        }

    def effective_category_config(self, category: str) -> CategoryConfig:
        base = self.config.categories.get(category, CategoryConfig(name=category))
        detail = self._get_category_detail(category)
        if detail:
            return base.merge_live(detail)
        return base

    def current_race_time(self) -> Optional[float]:
        with self.lock:
            if self.race_clock_value is None:
                return None
            if self.race_clock_wall is None:
                return self.race_clock_value
            return max(0.0, self.race_clock_value + (time.time() - self.race_clock_wall))

    def set_subsystem_status(self, key: str, state: str, endpoint: Optional[str] = None, detail: str = "") -> None:
        with self.lock:
            status = self.subsystems[key]
            if endpoint:
                status.endpoint = endpoint
            status.state = state
            status.updated_at = dt.datetime.now().astimezone()
            status.detail = detail

    def _effective_subsystem_state(self, key: str) -> SubsystemStatus:
        with self.lock:
            status = self.subsystems[key]
            effective = SubsystemStatus(
                name=status.name,
                endpoint=status.endpoint,
                state=status.state,
                updated_at=status.updated_at,
                detail=status.detail,
            )
            timeout = STATUS_TIMEOUTS.get(key)
            if timeout and effective.updated_at and effective.state == "connected":
                age = (dt.datetime.now().astimezone() - effective.updated_at).total_seconds()
                if age > timeout:
                    effective.state = "timedout"
            return effective

    def set_reader_status(self, reader: str, status: str) -> None:
        with self.lock:
            self.reader_status[reader] = status
        mapped = "connected" if status == "connected" else "timedout"
        self.set_subsystem_status("rfid", mapped, detail=reader)

    def set_reader_clock_diff(self, reader: str, delta_seconds: float) -> None:
        with self.lock:
            self.reader_clock_diff[reader] = delta_seconds

    def _reset_for_new_race_locked(self) -> None:
        self.passings.clear()
        self.results_by_bib.clear()
        self.category_details.clear()
        self.latest_lap_refresh = {}
        self.rider_runtime.clear()
        self.last_announcer_update = None
        self.last_lapcounter_update = None

    def reset_for_reader_reconnect(self, reader: str = "") -> None:
        with self.lock:
            self.passings.clear()
            self.rider_runtime.clear()
        logging.getLogger("lapsrv.state").warning(
            "reader reconnect reset local RFID state%s",
            f": {reader}" if reader else "",
        )

    def update_reference(self, ref: dict[str, Any]) -> None:
        with self.lock:
            new_version_count = None
            if (ref or {}).get("versionCount") is not None:
                try:
                    new_version_count = int((ref or {}).get("versionCount"))
                except (TypeError, ValueError):
                    new_version_count = None
            new_cur_race_time = None
            if (ref or {}).get("curRaceTime") is not None:
                try:
                    new_cur_race_time = float((ref or {}).get("curRaceTime"))
                except (TypeError, ValueError):
                    new_cur_race_time = None

            should_reset = False
            if self._last_version_count is not None and new_version_count is not None and new_version_count < self._last_version_count:
                should_reset = True
            elif (
                self._last_cur_race_time is not None
                and new_cur_race_time is not None
                and self._last_cur_race_time > 30.0
                and new_cur_race_time < 5.0
                and new_cur_race_time + 10.0 < self._last_cur_race_time
            ):
                should_reset = True

            if should_reset:
                logging.getLogger("lapsrv.state").info(
                    "detected new race, resetting volatile state: version %s->%s race_time %.2f->%.2f",
                    self._last_version_count,
                    new_version_count,
                    self._last_cur_race_time if self._last_cur_race_time is not None else -1.0,
                    new_cur_race_time if new_cur_race_time is not None else -1.0,
                )
                self._reset_for_new_race_locked()
                self.reference = {}

            self.reference.update(ref or {})
            cur_race_time = self.reference.get("curRaceTime")
            if cur_race_time is not None:
                try:
                    self.race_clock_value = float(cur_race_time)
                    self.race_clock_wall = time.time()
                except (TypeError, ValueError):
                    pass
            self._last_version_count = new_version_count if new_version_count is not None else self._last_version_count
            self._last_cur_race_time = new_cur_race_time if new_cur_race_time is not None else self._last_cur_race_time

    def update_announcer(self, msg: dict[str, Any]) -> None:
        cmd = msg.get("cmd")
        ref = msg.get("reference") or {}
        self.update_reference(ref)
        with self.lock:
            if cmd == "baseline":
                self.results_by_bib = {str(k): v for k, v in (msg.get("info") or {}).items()}
                self.category_details = {str(k): v for k, v in (msg.get("categoryDetails") or {}).items()}
            elif cmd == "ram":
                self._apply_ram(self.results_by_bib, msg.get("infoRAM") or {})
                self._apply_ram(self.category_details, msg.get("categoryRAM") or {})
            self._refresh_rider_runtime_from_announcer()
            self.last_announcer_update = dt.datetime.now().astimezone()
            self.subsystems["announcer"].updated_at = self.last_announcer_update
        logging.getLogger("lapsrv.crossmgr").debug(
            "announcer %s: info=%d category_details=%d ref_keys=%s",
            cmd,
            len(self.results_by_bib),
            len(self.category_details),
            sorted((ref or {}).keys()),
        )

    def update_lapcounter(self, msg: dict[str, Any]) -> None:
        self.update_reference(msg)
        with self.lock:
            self.latest_lap_refresh = msg
            self.last_lapcounter_update = dt.datetime.now().astimezone()
            self.subsystems["lapcounter"].updated_at = self.last_lapcounter_update
        logging.getLogger("lapsrv.crossmgr").debug(
            "lapcounter refresh: labels=%s curRaceTime=%s",
            msg.get("labels"),
            msg.get("curRaceTime"),
        )

    @staticmethod
    def _apply_ram(dest: dict[str, Any], ram: dict[str, Any]) -> None:
        for key, value in (ram.get("a") or {}).items():
            dest[str(key)] = value
        for key, value in (ram.get("m") or {}).items():
            dest[str(key)] = value
        for key in (ram.get("r") or []):
            dest.pop(str(key), None)

    def _get_category_detail(self, category: str) -> Optional[dict[str, Any]]:
        detail = self.category_details.get(category)
        if detail:
            return detail
        for candidate in self.category_details.values():
            if isinstance(candidate, dict) and candidate.get("name") == category:
                return candidate
        return None

    def _sorted_category_details(self) -> list[dict[str, Any]]:
        details = [v for v in self.category_details.values() if isinstance(v, dict)]
        details.sort(key=lambda d: (int(d.get("iSort", 999999)), str(d.get("name") or "")))
        return details

    def _lapcounter_label_map(self) -> dict[str, list[Any]]:
        labels = self.latest_lap_refresh.get("labels") or []
        if not labels:
            return {}
        categories = self._sorted_category_details()
        if len(categories) == len(labels) + 1:
            categories = categories[1:]
        elif len(categories) > len(labels):
            categories = categories[: len(labels)]
        elif len(labels) > len(categories):
            labels = labels[: len(categories)]
        mapping: dict[str, list[Any]] = {}
        for detail, label_row in zip(categories, labels):
            name = str(detail.get("name") or "").strip()
            if name:
                mapping[name] = label_row
        return mapping

    def _lapcounter_display_for_category(self, category: str) -> Optional[dict[str, Any]]:
        label_row = self._lapcounter_label_map().get(category)
        if not label_row:
            return None
        label = str(label_row[0]) if len(label_row) >= 1 and label_row[0] is not None else ""
        flash = bool(label_row[1]) if len(label_row) >= 2 else False
        lap_start = float(label_row[2]) if len(label_row) >= 3 and label_row[2] is not None else None
        return {"label": label, "flash": flash, "lap_start": lap_start}

    def _rider_state(self, bib: str) -> RiderRuntimeState:
        bib = str(bib)
        state = self.rider_runtime.get(bib)
        if state is None:
            state = RiderRuntimeState(bib=bib)
            self.rider_runtime[bib] = state
        return state

    def _refresh_rider_runtime_from_announcer(self) -> None:
        visible_bibs: set[str] = set()
        for candidate in self.category_details.values():
            if not isinstance(candidate, dict):
                continue
            name = str(candidate.get("name") or "").strip()
            if not name or name.lower() == "all":
                continue
            for bib in (candidate.get("pos") or []):
                bib = str(bib)
                visible_bibs.add(bib)
                state = self._rider_state(bib)
                state.visible_in_results = True
                state.category_hint = name

        if visible_bibs:
            all_bibs = set(self.rider_runtime) | set(self.results_by_bib) | set(self.config.riders_by_bib)
            for bib in all_bibs:
                state = self._rider_state(bib)
                if bib not in visible_bibs:
                    state.visible_in_results = False

        current_race_time = self.current_race_time()
        if current_race_time is None:
            return
        for bib in set(self.results_by_bib) | visible_bibs:
            progress = self._authoritative_progress_for_bib(bib, current_race_time)
            auth_lap = progress.get("recorded_lap")
            if auth_lap is None:
                continue
            state = self._rider_state(bib)
            if state.predicted_lap is None or auth_lap >= state.predicted_lap:
                state.predicted_lap = int(auth_lap)

    def _category_name_for_bib(self, bib: str) -> Optional[str]:
        bib = str(bib)
        state = self.rider_runtime.get(bib)
        if state and state.category_hint and state.category_hint.lower() != "all":
            return state.category_hint
        for candidate in self.category_details.values():
            if not isinstance(candidate, dict):
                continue
            name = str(candidate.get("name") or "").strip()
            if not name or name.lower() == "all":
                continue
            pos = [str(v) for v in (candidate.get("pos") or [])]
            if bib in pos:
                return name
        return None

    def _effective_category_name(self, rider: RiderInfo, result: dict[str, Any]) -> str:
        result_category = str(result.get("category") or "").strip()
        if result_category.lower() == "all":
            result_category = ""
        result_category_name = str(result.get("categoryName") or "").strip()
        if result_category_name.lower() == "all":
            result_category_name = ""
        return (
            self._category_name_for_bib(rider.bib)
            or result_category_name
            or result_category
            or rider.category
            or "Unknown"
        )

    def _race_times(self, result: dict[str, Any]) -> list[float]:
        return [float(v) for v in (result.get("raceTimes") or []) if v is not None]

    def _interp_flags(self, result: dict[str, Any]) -> list[bool]:
        return [bool(v) for v in (result.get("interp") or [])]

    def _authoritative_progress_for_bib(self, bib: str, current_race_time: Optional[float]) -> dict[str, Optional[float]]:
        result = self.results_by_bib.get(str(bib))
        if not result:
            return {"recorded_lap": None, "recorded_time": None, "expected_lap": None, "expected_time": None}
        return self._result_progress(result, current_race_time)

    def _announcer_recorded_map(self, current_race_time: Optional[float]) -> dict[str, dict[str, Any]]:
        if current_race_time is None:
            return {}
        recorded: dict[str, dict[str, Any]] = {}
        for bib, result in self.results_by_bib.items():
            if not result:
                continue
            status = str(result.get("status") or "").strip()
            race_times = self._race_times(result)
            if status != "Finisher" or len(race_times) < 2:
                continue
            progress = self._result_progress(result, current_race_time)
            recorded_lap = progress.get("recorded_lap")
            recorded_time = progress.get("recorded_time")
            if recorded_lap is None or recorded_time is None or recorded_lap < 1:
                continue
            recorded[str(bib)] = {"lap": int(recorded_lap), "t": float(recorded_time)}
        return recorded

    def _result_progress(self, result: dict[str, Any], current_race_time: Optional[float]) -> dict[str, Optional[float]]:
        if current_race_time is None or not result:
            return {"recorded_lap": None, "recorded_time": None, "expected_lap": None, "expected_time": None}
        race_times = self._race_times(result)
        if not race_times:
            return {"recorded_lap": 0, "recorded_time": None, "expected_lap": None, "expected_time": None}
        is_time_trial = bool(self.reference.get("isTimeTrial"))
        offset = float(result.get("startTime") or 0.0) if is_time_trial else 0.0
        target = current_race_time - offset
        i = bisect.bisect_left(race_times, target)
        interp = self._interp_flags(result)

        expected_lap: Optional[int] = None
        expected_time: Optional[float] = None
        lap = i
        if lap > 1 and lap - 1 < len(interp) and interp[lap - 1]:
            lap -= 1
        if lap < len(race_times) and lap < len(interp) and interp[lap]:
            expected_lap = lap
            expected_time = race_times[lap] + offset

        lap = i - 1
        while lap > 0 and lap < len(interp) and interp[lap]:
            lap -= 1
        recorded_lap = max(lap, 0)
        recorded_time = None
        if recorded_lap < len(race_times) and (recorded_lap == 0 or recorded_lap >= len(interp) or not interp[recorded_lap]):
            recorded_time = race_times[recorded_lap] + offset

        return {
            "recorded_lap": recorded_lap,
            "recorded_time": recorded_time,
            "expected_lap": expected_lap,
            "expected_time": expected_time,
        }

    def _lap_for_rider(self, bib: str, current_race_time: Optional[float]) -> Optional[int]:
        bib = str(bib)
        state = self._rider_state(bib)
        progress = self._authoritative_progress_for_bib(bib, current_race_time)
        lap = progress.get("recorded_lap")
        if state.predicted_lap is not None and (lap is None or state.predicted_lap > lap):
            return int(state.predicted_lap)
        return int(lap) if lap is not None else state.predicted_lap

    def _rank_for_rider(self, rider: RiderInfo) -> Optional[int]:
        result = self.results_by_bib.get(rider.bib, {})
        category = self._effective_category_name(rider, result)
        detail = self._get_category_detail(category)
        if not detail:
            return None
        pos = [str(v) for v in (detail.get("pos") or [])]
        try:
            return pos.index(rider.bib) + 1
        except ValueError:
            return None

    def _category_bibs(self, category: str) -> list[str]:
        bibs: list[str] = []
        seen: set[str] = set()
        detail = self._get_category_detail(category)
        if detail:
            for bib in (detail.get("pos") or []):
                bib_s = str(bib)
                if bib_s and bib_s not in seen:
                    seen.add(bib_s)
                    bibs.append(bib_s)
        for bib, rider in self.config.riders_by_bib.items():
            result = self.results_by_bib.get(bib, {})
            effective = self._effective_category_name(rider, result)
            if effective == category and bib not in seen:
                seen.add(bib)
                bibs.append(bib)
        return bibs

    def _rank_sort_key_for_bib(self, bib: str, category: str, current_race_time: Optional[float]) -> tuple[int, float, int, str]:
        rider = self.config.riders_by_bib.get(str(bib))
        if not rider:
            return (999999, float("inf"), 999999, str(bib))
        lap = self._lap_for_rider(rider.bib, current_race_time)
        if lap is None:
            lap = -1
        state = self._rider_state(rider.bib)
        lap_entry_time: Optional[float] = state.last_local_read_race_time
        progress = self._authoritative_progress_for_bib(rider.bib, current_race_time)
        recorded_time = progress.get("recorded_time")
        recorded_lap = progress.get("recorded_lap")
        if lap_entry_time is None and recorded_time is not None and recorded_lap is not None:
            lap_entry_time = float(recorded_time)
        fallback_rank = self._rank_for_rider(rider) or 999999
        return (-int(lap), float(lap_entry_time) if lap_entry_time is not None else float("inf"), fallback_rank, rider.bib)

    def _display_rank_for_bib(self, bib: str, category: str, current_race_time: Optional[float]) -> Optional[int]:
        bibs = self._category_bibs(category)
        if not bibs:
            return None
        ordered = sorted(bibs, key=lambda b: self._rank_sort_key_for_bib(b, category, current_race_time))
        try:
            return ordered.index(str(bib)) + 1
        except ValueError:
            return None

    def _leader_lap(self, rider: RiderInfo, current_race_time: Optional[float]) -> Optional[int]:
        result = self.results_by_bib.get(rider.bib, {})
        category = self._effective_category_name(rider, result)
        detail = self._get_category_detail(category)
        if not detail or current_race_time is None:
            return None
        bibs = self._category_bibs(category)
        if not bibs:
            return None
        leader_bib = min(bibs, key=lambda b: self._rank_sort_key_for_bib(b, category, current_race_time))
        return self._lap_for_rider(leader_bib, current_race_time)

    def _note_for_rider(self, rider: RiderInfo) -> tuple[str, Optional[int]]:
        rank = self._display_rank_for_bib(rider.bib, rider.category or self._effective_category_name(rider, self.results_by_bib.get(rider.bib, {})), self.current_race_time())
        if rank == 1:
            return "1st", rank
        if rank:
            return ordinal(rank), rank
        return "", None

    def _rider_rank_and_lap_deficit(self, bib: str, current_race_time: Optional[float]) -> tuple[Optional[int], int]:
        rider = self.config.riders_by_bib.get(str(bib))
        if not rider:
            return None, 0
        category = self._effective_category_name(rider, self.results_by_bib.get(rider.bib, {}))
        rank = self._display_rank_for_bib(rider.bib, category, current_race_time)
        rider_lap = self._lap_for_rider(rider.bib, current_race_time)
        leader_lap = self._leader_lap(rider, current_race_time)
        lap_deficit = 0
        if rider_lap is not None and leader_lap is not None and leader_lap > rider_lap:
            lap_deficit = leader_lap - rider_lap
        return rank, lap_deficit

    def _group_note_for_category(
        self, group_passings: list[PassingRecord], category: str, current_race_time: Optional[float], lap_deficit_filter: int = 0
    ) -> tuple[str, bool, bool]:
        category_passings = [p for p in group_passings if p.category == category and p.bib not in {"?", ""}]
        ranks: list[int] = []
        best_rank: Optional[int] = None
        for passing in category_passings:
            rank, lap_deficit = self._rider_rank_and_lap_deficit(passing.bib, current_race_time)
            if not rank or lap_deficit != lap_deficit_filter:
                continue
            ranks.append(rank)
            if best_rank is None or rank < best_rank:
                best_rank = rank
        if not ranks or best_rank is None:
            return "", False, lap_deficit_filter > 0
        worst_rank = max(ranks)
        if best_rank == worst_rank:
            note = "1st" if best_rank == 1 else ordinal(best_rank)
        else:
            start = "1st" if best_rank == 1 else ordinal(best_rank)
            note = f"{start}:{ordinal(worst_rank)}"
        return note, best_rank == 1, lap_deficit_filter > 0

    def _gap_for_rider(self, rider: RiderInfo, category: str, lap: Optional[int], current_race_time: Optional[float]) -> str:
        rank = self._rank_for_rider(rider)
        leader_lap = self._leader_lap(rider, current_race_time)
        if lap is not None and leader_lap is not None and leader_lap > lap:
            return f"-{leader_lap - lap} laps"
        if not rank or rank <= 1:
            return ""
        with self.lock:
            previous_same_cat = next(
                (p for p in reversed(self.passings) if p.category == category and p.bib != rider.bib),
                None,
            )
        if previous_same_cat and previous_same_cat.race_time is not None and current_race_time is not None:
            delta = abs((previous_same_cat.race_time or 0.0) - current_race_time)
            return format_clock(delta)
        detail = self._get_category_detail(category)
        if detail:
            pos = [str(v) for v in (detail.get("pos") or [])]
            gap_values = detail.get("gapValue") or []
            try:
                idx = pos.index(rider.bib)
                if idx < len(gap_values):
                    gap = gap_values[idx]
                    if isinstance(gap, (int, float)):
                        return format_clock(float(gap)) if float(gap) > 0 else str(gap)
                    if gap is not None:
                        return str(gap)
            except ValueError:
                pass
        return ""

    def _eta_seconds(self, rider: RiderInfo, category: str, result: dict[str, Any], lap: Optional[int]) -> Optional[float]:
        progress = self._authoritative_progress_for_bib(rider.bib, self.current_race_time())
        expected_time = progress.get("expected_time")
        current_race_time = self.current_race_time()
        if expected_time is not None and current_race_time is not None:
            return expected_time - current_race_time
        speed = parse_speed_mps(result.get("speed"))
        if speed and speed > 0:
            return DISTANCE_TO_FINISH_METERS / speed
        race_times = self._race_times(result)
        if lap is None or lap <= 0:
            return None
        lap_distance_km = result.get("lapDistance")
        if lap_distance_km in (None, ""):
            detail = self._get_category_detail(category)
            if detail:
                lap_distance_km = detail.get("lapDistance")
        cat_cfg = self.effective_category_config(category)
        if (not cat_cfg.name or cat_cfg.name == category) and rider.category and rider.category != category:
            fallback_cfg = self.config.categories.get(rider.category)
            if fallback_cfg:
                cat_cfg = fallback_cfg
        if lap_distance_km in (None, "") and cat_cfg and cat_cfg.race_distance:
            lap_distance_km = cat_cfg.race_distance
        if lap_distance_km in (None, "") or lap >= len(race_times):
            return None
        lap_time = race_times[lap] - race_times[lap - 1]
        if lap_time <= 0:
            return None
        lap_distance_m = float(lap_distance_km) * 1000.0
        speed = lap_distance_m / lap_time
        if speed <= 0:
            return None
        return DISTANCE_TO_FINISH_METERS / speed

    def record_passing(self, tag: str, seen_at: dt.datetime, reader: str = "") -> Optional[PassingRecord]:
        tag = tag.strip().upper()
        received_at = dt.datetime.now().astimezone()
        self.set_subsystem_status("rfid", "connected", detail=reader or tag)
        with self.lock:
            rider = self.config.riders_by_tag.get(tag)
            current_race_time = self.current_race_time()
            race_time = None
            if current_race_time is not None:
                if self.race_clock_wall is not None:
                    race_time = max(0.0, self.race_clock_value + (seen_at.timestamp() - self.race_clock_wall))
                else:
                    race_time = current_race_time

            if rider:
                state = self._rider_state(rider.bib)
                if race_time is not None:
                    auth_progress = self._authoritative_progress_for_bib(rider.bib, race_time)
                    auth_lap = auth_progress.get("recorded_lap")
                    new_predicted_lap = auth_lap if auth_lap is not None else state.predicted_lap
                    is_new_local_read = (
                        state.last_local_read_race_time is None
                        or abs(race_time - state.last_local_read_race_time) >= LOCAL_READ_DEDUP_SECONDS
                    )
                    if is_new_local_read:
                        if auth_lap is not None:
                            new_predicted_lap = max(new_predicted_lap or 0, auth_lap + 1)
                        elif state.predicted_lap is not None:
                            new_predicted_lap = state.predicted_lap + 1
                    state.predicted_lap = int(new_predicted_lap) if new_predicted_lap is not None else state.predicted_lap
                    state.last_local_read_race_time = race_time
                    state.last_local_read_seen_at = seen_at
                result = self.results_by_bib.get(rider.bib, {})
                category = self._effective_category_name(rider, result)
                if category and category != "Unknown":
                    state.category_hint = category
                if not state.visible_in_results:
                    return None
                lap = self._lap_for_rider(rider.bib, race_time if race_time is not None else current_race_time)
                note, rank = self._note_for_rider(rider)
                gap = self._gap_for_rider(rider, category, lap, race_time if race_time is not None else current_race_time)
                eta = self._eta_seconds(rider, category, result, lap)
                record = PassingRecord(
                    bib=rider.bib,
                    tag=tag,
                    seen_at=seen_at,
                    received_at=received_at,
                    race_time=race_time,
                    note=note,
                    gap=gap,
                    lap=lap,
                    name=rider.full_name,
                    category=category,
                    team=rider.team,
                    eta_seconds=eta,
                    category_rank=rank,
                    reader=reader,
                )
            else:
                if not self.show_unknown_tags:
                    return None
                record = PassingRecord(
                    bib=tag[:12],
                    tag=tag,
                    seen_at=seen_at,
                    received_at=received_at,
                    race_time=race_time,
                    note="Unknown tag",
                    gap="",
                    lap=None,
                    name="Unknown rider",
                    category="Unknown",
                    team="",
                    eta_seconds=None,
                    category_rank=None,
                    reader=reader,
                )
            self.passings.append(record)
            return record

    def _category_laps_to_go(self, category: str, current_race_time: Optional[float]) -> Optional[int]:
        detail = self._get_category_detail(category)
        if not detail:
            return None
        cfg = self.effective_category_config(category)
        total_laps = detail.get("laps")
        if total_laps in (None, "", 0):
            total_laps = cfg.race_laps if cfg and cfg.race_laps is not None else None
        if total_laps is None:
            return None
        pos = [str(v) for v in (detail.get("pos") or [])]
        if not pos:
            return int(total_laps)
        leader_result = self.results_by_bib.get(pos[0], {})
        leader_progress = self._result_progress(leader_result, current_race_time)
        leader_lap = leader_progress.get("expected_lap")
        if leader_lap is None:
            leader_lap = leader_progress.get("recorded_lap")
        if leader_lap is None:
            return int(total_laps)
        return max(int(total_laps) - leader_lap, 0)

    def _category_display_summary(self, category: str, current_race_time: Optional[float]) -> dict[str, Any]:
        lapcounter_display = self._lapcounter_display_for_category(category)
        laps_to_go = self._category_laps_to_go(category, current_race_time)
        lap_label = None
        if lapcounter_display:
            lap_label = lapcounter_display["label"]
            try:
                laps_to_go = int(lap_label)
            except (TypeError, ValueError):
                pass
        return {
            "laps_to_go": laps_to_go,
            "lap_label": lap_label,
            "bell": (lap_label == "1") if lap_label is not None else laps_to_go == 1,
        }

    def _category_display_order(self) -> list[str]:
        raw_names: list[str] = []
        raw_names.extend(name for name in self.config.categories.keys() if name and name.lower() != "all")
        for detail in self._sorted_category_details():
            name = str(detail.get("name") or "").strip()
            if name and name.lower() != "all":
                raw_names.append(name)

        names: list[str] = []
        seen: set[str] = set()
        for name in raw_names:
            if name in seen:
                continue
            seen.add(name)
            names.append(name)

        gendered_prefixes = {name.split(" (", 1)[0] for name in names if " (" in name}
        ordered: list[str] = []
        for name in names:
            if " (" not in name and name in gendered_prefixes:
                continue
            ordered.append(name)
        return ordered

    def _category_header_status(self, category: str, current_race_time: Optional[float]) -> dict[str, Any]:
        summary = self._category_display_summary(category, current_race_time)
        lap_text = summary["lap_label"] if summary["lap_label"] not in (None, "") else summary["laps_to_go"]
        if summary["bell"]:
            return {"text": "bell", "is_bell": True}
        return {"text": "" if lap_text in (None, "") else str(lap_text), "is_bell": False}

    def _build_group_tables(self) -> tuple[dict[str, Any], dict[str, Any]]:
        with self.lock:
            passings = list(self.passings)
            current_race_time = self.current_race_time()
            category_order = self._category_display_order()
        header_status = {category: self._category_header_status(category, current_race_time) for category in category_order}
        announcer_recorded = self._announcer_recorded_map(current_race_time)
        empty = {"headers": category_order, "header_status": header_status, "rows": []}
        if not passings:
            return empty, empty

        groups: list[dict[str, Any]] = []
        current_group: Optional[dict[str, Any]] = None
        prev_seen_at: Optional[dt.datetime] = None
        for passing in passings:
            if current_group is None:
                current_group = {"passings": [passing], "start": passing.seen_at, "end": passing.seen_at}
                groups.append(current_group)
            else:
                gap = (passing.seen_at - prev_seen_at).total_seconds() if prev_seen_at else 0.0
                if gap >= GROUP_GAP_SECONDS:
                    current_group = {"passings": [passing], "start": passing.seen_at, "end": passing.seen_at}
                    groups.append(current_group)
                else:
                    current_group["passings"].append(passing)
                    current_group["end"] = passing.seen_at
            prev_seen_at = passing.seen_at
        cutoff_seen_at = dt.datetime.now().astimezone() - dt.timedelta(seconds=GROUP_MAX_AGE_SECONDS)
        groups = [group for group in groups if group.get("end") and group["end"] >= cutoff_seen_at]
        groups = groups[-MAX_GROUPS:]

        rows: list[dict[str, Any]] = []
        previous_group_time: Optional[float] = None
        for group in groups:
            group_passings: list[PassingRecord] = group["passings"]
            group_race_time = next((p.race_time for p in group_passings if p.race_time is not None), None)
            gap_seconds = None
            if group_race_time is not None and previous_group_time is not None:
                gap_seconds = group_race_time - previous_group_time
            if group_race_time is not None:
                previous_group_time = group_race_time

            per_category: dict[str, dict[int, dict[str, Any]]] = {}
            for passing in group_passings:
                category = passing.category
                if category not in category_order:
                    if any(name.startswith(f"{category} (") for name in category_order):
                        continue
                    category_order.append(category)
                _rank, lap_deficit = self._rider_rank_and_lap_deficit(passing.bib, current_race_time)
                cat_rows = per_category.setdefault(category, {})
                info = cat_rows.setdefault(lap_deficit, {"count": 0, "bibs": []})
                info["count"] += 1
                if passing.bib not in info["bibs"]:
                    info["bibs"].append(passing.bib)

            row_deficits = sorted({deficit for deficits in per_category.values() for deficit in deficits.keys()})
            for row_index, lap_deficit in enumerate(row_deficits):
                cells: dict[str, dict[str, Any]] = {}
                row_bibs: list[str] = []
                for category, deficit_rows in per_category.items():
                    info = deficit_rows.get(lap_deficit)
                    if not info:
                        continue
                    count = int(info["count"])
                    note, is_lead, is_lapped = self._group_note_for_category(
                        group_passings, category, current_race_time, lap_deficit_filter=lap_deficit
                    )
                    parts = [note]
                    if count > 1:
                        parts.append(f"({count})")
                    cells[category] = {
                        "text": " ".join(part for part in parts if part),
                        "note": note,
                        "count": count,
                        "is_lead": is_lead,
                        "is_lapped": is_lapped,
                    }
                    row_bibs.extend(str(b) for b in info["bibs"])
                visible_cells = {category: cells[category] for category in category_order if category in cells}
                row_is_past = False
                for bib in row_bibs:
                    rec = announcer_recorded.get(str(bib))
                    if not rec:
                        continue
                    if group_race_time is None or rec.get("t") >= group_race_time:
                        row_is_past = True
                        break
                rows.append(
                    {
                        "elapsed_text": format_elapsed_hms(group_race_time) if row_index == 0 else "",
                        "group_gap": format_elapsed_hms(gap_seconds) if gap_seconds is not None and row_index == 0 else "",
                        "total": sum(cell.get("count", 0) for cell in visible_cells.values()),
                        "cells": visible_cells,
                        "is_past": row_is_past,
                    }
                )
        header_status = {category: self._category_header_status(category, current_race_time) for category in category_order}
        recent_rows = [dict(r) for r in rows if not r.get("is_past")]
        past_rows = [dict(r) for r in rows if r.get("is_past")]
        past_rows.reverse()
        for r in recent_rows:
            r.pop("is_past", None)
        for r in past_rows:
            r.pop("is_past", None)
        return (
            {"headers": category_order, "header_status": header_status, "rows": recent_rows},
            {"headers": category_order, "header_status": header_status, "rows": past_rows},
        )

    def recent_group_table(self) -> dict[str, Any]:
        recent, _past = self._build_group_tables()
        return recent

    def past_group_table(self) -> dict[str, Any]:
        _recent, past = self._build_group_tables()
        return past

    def category_passings(self, category: str) -> list[dict[str, Any]]:
        with self.lock:
            return [p.as_dict() for p in self.passings if p.category == category]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            current_race_time = self.current_race_time()
            return {
                "event_name": self.config.event_name,
                "event_date": self.config.event_date,
                "timezone": self.config.timezone,
                "current_race_time": format_clock(current_race_time),
                "recent_group_table": self.recent_group_table(),
                "past_group_table": self.past_group_table(),
                "passings": [p.as_dict() for p in self.passings],
                "readers": dict(self.reader_status),
                "reader_clock_diff": {k: round(v, 3) for k, v in self.reader_clock_diff.items()},
                "last_announcer_update": as_utc_iso(self.last_announcer_update) if self.last_announcer_update else "",
                "last_lapcounter_update": as_utc_iso(self.last_lapcounter_update) if self.last_lapcounter_update else "",
                "latest_lap_refresh": self.latest_lap_refresh,
                "subsystems": {k: self._effective_subsystem_state(k).snapshot() for k in self.subsystems},
            }


class JChipServer:
    def __init__(self, state: SharedState, host: str, port: int) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.server: Optional[asyncio.AbstractServer] = None
        self.logger = logging.getLogger("lapsrv.jchip")
        self.date_today = dt.date.today()
        self.last_timestamp: Optional[dt.datetime] = None
        self.same_count = 0
        self.client_tasks: set[asyncio.Task[Any]] = set()
        self.client_writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self.handle_client, self.host, self.port)
        socknames = ", ".join(str(sock.getsockname()) for sock in (self.server.sockets or []))
        self.logger.info("JChip listener started on %s", socknames)
        self.state.set_subsystem_status("rfid", "waiting", endpoint=socknames)

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for writer in list(self.client_writers):
            writer.close()
        for writer in list(self.client_writers):
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                pass
        if self.client_tasks:
            for task in list(self.client_tasks):
                task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(*list(self.client_tasks), return_exceptions=True), timeout=1.0)
            except Exception:
                pass

    def _parse_time(self, t_str: str, day: int = 0) -> dt.datetime:
        hh, mm, ss = t_str.split(":")
        base = dt.datetime.combine(self.date_today, dt.time()) + dt.timedelta(
            seconds=(float(hh) * 3600.0) + (float(mm) * 60.0) + float(ss)
        )
        timestamp = base + dt.timedelta(days=day)
        if self.last_timestamp is None:
            self.last_timestamp = timestamp - dt.timedelta(microseconds=10)
        if timestamp == self.last_timestamp:
            self.same_count += 1
        else:
            self.same_count = 0
            self.last_timestamp = timestamp
        if self.same_count:
            timestamp += dt.timedelta(microseconds=10 * self.same_count)
        return timestamp.astimezone()

    def _parse_tag_line(self, line: str) -> tuple[str, dt.datetime]:
        i_space = line.find(" ")
        if i_space < 0:
            raise ValueError(f"invalid D record: {line!r}")
        tag = line[2:i_space].strip().upper()
        i_colon = line.find(":")
        if i_colon < 2:
            raise ValueError(f"missing time in D record: {line!r}")
        match = RE_TIME.match(line[i_colon - 2 :])
        if not match:
            raise ValueError(f"invalid time in D record: {line!r}")
        t_str = match.group(0)
        day = 0
        i_second_field = line.find(" ", i_colon)
        if i_second_field >= 0 and i_second_field + 2 < len(line):
            try:
                day = int(line[i_second_field + 2 : i_second_field + 3])
            except ValueError:
                day = 0
        if "date=" in line:
            i_date = line.index("date=") + 5
            year = int(line[i_date : i_date + 4])
            month = int(line[i_date + 4 : i_date + 6])
            day_of_month = int(line[i_date + 6 : i_date + 8])
            tag_date = dt.date(year, month, day_of_month)
            day = (tag_date - self.date_today).days
        return tag, self._parse_time(t_str, day)

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self.client_tasks.add(task)
        self.client_writers.add(writer)
        addr = writer.get_extra_info("peername")
        reader_name = f"{addr}"
        time_adjust = 0.0
        self.logger.info("reader connected: %s", addr)
        self.state.reset_for_reader_reconnect(reader_name)
        try:
            while not reader.at_eof():
                raw = await reader.readuntil(CR_BYTES)
                line = raw[:-1].decode(errors="ignore").strip()
                if not line:
                    continue
                self.logger.warning("reader rx: %s %s", reader_name, line)
                if line.startswith("N"):
                    reader_name = line[5:].strip() or reader_name
                    self.logger.info("reader identified: %s", reader_name)
                    self.state.set_reader_status(reader_name, "connected")
                    writer.write(f"GT{CR}".encode())
                    await writer.drain()
                    self.logger.info("sent GT to reader: %s", reader_name)
                elif line.startswith("GT"):
                    now = dt.datetime.now()
                    hh = int(line[3:5])
                    mm = int(line[5:7])
                    ss = int(line[7:9])
                    hs = int(line[9:11])
                    if "date=" in line:
                        i_date = line.index("date=") + 5
                        jchip_now = dt.datetime(
                            int(line[i_date : i_date + 4]),
                            int(line[i_date + 4 : i_date + 6]),
                            int(line[i_date + 6 : i_date + 8]),
                            hh,
                            mm,
                            ss,
                            hs * 10000,
                        )
                        self.date_today = jchip_now.date()
                    else:
                        jchip_now = dt.datetime.combine(now.date(), dt.time(hh, mm, ss, hs * 10000))
                    time_adjust = (now - jchip_now).total_seconds()
                    self.state.set_reader_clock_diff(reader_name, time_adjust)
                    writer.write(f"S0000{CR}".encode())
                    await writer.drain()
                    self.logger.info("sent S0000 to reader: %s", reader_name)
                elif line.startswith("D"):
                    tag, seen_at = self._parse_tag_line(line)
                    seen_at = seen_at + dt.timedelta(seconds=time_adjust)
                    record = self.state.record_passing(tag, seen_at.astimezone(), reader_name)
                    if record is not None:
                        self.logger.info(
                            "passing: reader=%s tag=%s bib=%s cat=%s time=%s",
                            reader_name,
                            record.tag,
                            record.bib,
                            record.category,
                            format_clock(record.race_time),
                        )
                else:
                    self.logger.debug("ignored reader line from %s: %s", reader_name, line)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except asyncio.CancelledError:
            pass
        except Exception:
            self.logger.exception("reader handler failed for %s", reader_name)
            self.state.set_subsystem_status("rfid", "error", detail=reader_name)
        finally:
            self.state.set_reader_status(reader_name, "disconnected")
            self.client_writers.discard(writer)
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                pass
            if task is not None:
                self.client_tasks.discard(task)
            self.logger.info("reader disconnected: %s", reader_name)


class CrossMgrClient:
    def __init__(self, state: SharedState, host: str, port: int) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.logger = logging.getLogger("lapsrv.crossmgr")
        self.stop_event = asyncio.Event()
        self._announcer_ws: Any = None
        self._lapcounter_ws: Any = None

    async def run(self) -> None:
        await asyncio.gather(
            self._run_announcer(),
            self._run_lapcounter(),
        )

    async def stop(self) -> None:
        self.stop_event.set()
        for ws in (self._announcer_ws, self._lapcounter_ws):
            if ws is not None:
                try:
                    await asyncio.wait_for(ws.close(), timeout=1.0)
                except Exception:
                    pass

    async def _run_announcer(self) -> None:
        url = f"ws://{self.host}:{self.port + 1}/"
        while not self.stop_event.is_set():
            try:
                self.logger.info("connecting announcer websocket %s", url)
                self.state.set_subsystem_status("announcer", "waiting", endpoint=self.host)
                async with websockets.connect(url, open_timeout=5, ping_interval=None) as ws:
                    self._announcer_ws = ws
                    self.state.set_subsystem_status("announcer", "connected", endpoint=self.host)
                    await ws.send(json.dumps({"cmd": "send_baseline", "raceName": "CurrentResults"}))
                    async for raw in ws:
                        if self.stop_event.is_set():
                            break
                        msg = json.loads(raw)
                        if msg.get("cmd") in {"baseline", "ram"}:
                            self.state.update_announcer(msg)
                self._announcer_ws = None
            except asyncio.CancelledError:
                self._announcer_ws = None
                raise
            except Exception as exc:
                self.logger.warning("announcer websocket error: %s", exc)
                self.state.set_subsystem_status("announcer", "error", endpoint=self.host, detail=str(exc))
                await asyncio.sleep(5)

    async def _run_lapcounter(self) -> None:
        url = f"ws://{self.host}:{self.port + 2}/"
        while not self.stop_event.is_set():
            try:
                self.logger.info("connecting lapcounter websocket %s", url)
                self.state.set_subsystem_status("lapcounter", "waiting", endpoint=self.host)
                async with websockets.connect(url, open_timeout=5, ping_interval=None) as ws:
                    self._lapcounter_ws = ws
                    self.state.set_subsystem_status("lapcounter", "connected", endpoint=self.host)
                    async for raw in ws:
                        if self.stop_event.is_set():
                            break
                        msg = json.loads(raw)
                        if msg.get("cmd") == "refresh":
                            self.state.update_lapcounter(msg)
                self._lapcounter_ws = None
            except asyncio.CancelledError:
                self._lapcounter_ws = None
                raise
            except Exception as exc:
                self.logger.warning("lapcounter websocket error: %s", exc)
                self.state.set_subsystem_status("lapcounter", "error", endpoint=self.host, detail=str(exc))
                await asyncio.sleep(5)


class WebServer:
    def __init__(self, state: SharedState, host: str, port: int) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.logger = logging.getLogger("lapsrv.web")
        self._routes()

    def _routes(self) -> None:
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/category/{name}", self.handle_category)
        self.app.router.add_get("/api/state", self.handle_state)
        self.app.router.add_get("/api/category/{name}", self.handle_category_state)

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        self.logger.info("web server started on http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def handle_state(self, request: web.Request) -> web.Response:
        return web.json_response(self.state.snapshot())

    async def handle_category_state(self, request: web.Request) -> web.Response:
        category = request.match_info["name"]
        snapshot = self.state.snapshot()
        snapshot["passings"] = self.state.category_passings(category)
        snapshot["selected_category"] = category
        return web.json_response(snapshot)

    async def handle_index(self, request: web.Request) -> web.Response:
        return web.Response(text=self._html(category=None), content_type="text/html")

    async def handle_category(self, request: web.Request) -> web.Response:
        return web.Response(text=self._html(category=request.match_info["name"]), content_type="text/html")

    def _html(self, category: Optional[str]) -> str:
        category_js = json.dumps(category)
        api_path = "/api/state" if category is None else f"/api/category/{category}"
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>lapsrv</title>
<style>
:root {{
  --bg: #f8fafc;
  --panel: #ffffff;
  --panel-2: #e5eef7;
  --fg: #0f172a;
  --muted: #475569;
  --accent: #b45309;
  --ok: #15803d;
  --border: #cbd5e1;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  font-family: "Trebuchet MS", "Segoe UI", sans-serif;
  background: linear-gradient(180deg, #fefefe 0%, #eef6ff 100%);
  color: var(--fg);
}}
body.compact-mode .default-view {{ display: none; }}
body:not(.compact-mode) .compact-view {{ display: none; }}
main {{
  max-width: 1400px;
  margin: 0 auto;
  padding: 16px;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}}
.title {{
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}}
.meta {{ color: var(--muted); font-size: 0.95rem; }}
.statusbar {{
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
}}
.toolbar {{
  display: flex;
  gap: 8px;
  align-items: flex-start;
  flex-wrap: wrap;
  margin-top: -4px;
}}
.view-toggle, .audio-toggle {{
  border: 1px solid rgba(250,204,21,.45);
  background: rgba(250,204,21,.12);
  color: #92400e;
  border-radius: 999px;
  padding: 7px 12px;
  font-size: 0.92rem;
  font-weight: 700;
  cursor: pointer;
}}
.audio-toggle.active {{
  background: rgba(21,128,61,.12);
  border-color: rgba(21,128,61,.35);
  color: #166534;
}}
.badge {{
  display: inline-flex;
  gap: 6px;
  align-items: center;
  border-radius: 999px;
  padding: 6px 10px;
  font-size: 0.92rem;
  font-weight: 700;
  border: 1px solid transparent;
}}
.state-connected {{
  background: rgba(34,197,94,.12);
  color: #166534;
  border-color: rgba(34,197,94,.24);
}}
.state-waiting, .state-timedout {{
  background: rgba(245,158,11,.14);
  color: #92400e;
  border-color: rgba(245,158,11,.28);
}}
.state-error {{
  background: rgba(239,68,68,.12);
  color: #991b1b;
  border-color: rgba(239,68,68,.24);
}}
.group-wrap {{
  margin: 16px 0 18px;
  background: rgba(255,255,255,.96);
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}}
.group-title {{
  padding: 10px 14px;
  font-size: 1.05rem;
  font-weight: 700;
  color: var(--muted);
  background: rgba(229,238,247,.98);
  border-bottom: 1px solid var(--border);
}}
.group-rows {{
  overflow-y: auto;
  user-select: text;
  -webkit-user-select: text;
  background: rgba(255,255,255,.98);
}}
.recent-group-rows {{
  height: 48vh;
}}
body.compact-mode .recent-group-rows {{
  height: auto;
  flex: 1 1 auto;
}}
.past-group-rows {{
  height: 26vh;
}}
.group-table {{
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}}
.group-table th, .group-table td {{
  padding: 8px 10px;
  border-bottom: 1px solid rgba(203,213,225,.95);
  font-size: 0.98rem;
  vertical-align: top;
}}
.group-table th {{
  position: sticky;
  top: 0;
  background: rgba(31,41,55,.98);
  z-index: 1;
}}
.group-table thead tr:nth-child(2) th {{
  top: 42px;
}}
.group-cell.lead, .group-header-status.bell {{
  background: #facc15;
  color: #111827;
  font-weight: 700;
}}
.group-cell-content {{
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
}}
.group-cell-note {{
  min-width: 0;
}}
.group-cell-count {{
  margin-left: auto;
  text-align: right;
  min-width: 1ch;
}}
.group-cell.lapped {{
  background: rgba(226, 232, 240, 0.16);
  color: #64748b;
}}
.group-cell.lapped .group-cell-content {{
  justify-content: flex-end;
}}
.group-header-status {{
  color: #e5e7eb;
  font-weight: 700;
}}
.group-empty {{ color: #6b7280; }}
.compact-wrap {{
  margin: 16px 0 0;
  background: rgba(255,255,255,.96);
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
}}
.compact-table {{
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}}
.compact-table th:nth-child(1), .compact-table td:nth-child(1) {{
  width: 70px;
  white-space: nowrap;
}}
.compact-table th:nth-child(2), .compact-table td:nth-child(2) {{
  width: 56px;
  white-space: nowrap;
}}
.compact-table th, .compact-table td {{
  padding: 8px 10px;
  border-bottom: 1px solid rgba(203,213,225,.95);
  font-size: 1.02rem;
  vertical-align: top;
  background: rgba(255,255,255,.98);
}}
.compact-table th {{
  background: rgba(229,238,247,.98);
  position: sticky;
  top: 0;
  z-index: 1;
}}
.compact-group-cell {{
  font-weight: 700;
  letter-spacing: 0.01em;
  font-size: 2.3rem;
  line-height: 1.1;
}}
.compact-group-lead td, .compact-group-lead .compact-group-cell {{
  background: #facc15;
  color: #111827;
}}
.compact-group-bell td, .compact-group-bell .compact-group-cell {{
  background: #fbcfe8;
  color: #111827;
}}
.compact-group-next td {{
  font-size: 1.2rem;
  line-height: 1.05;
}}
.compact-group-next .compact-group-cell {{
  font-size: 4.4rem;
  line-height: 0.96;
}}
.compact-group-next .compact-hide-meta {{
  display: none;
}}
.compact-separator td {{
  padding: 0;
  border-bottom: none;
  height: 14px;
  background: transparent;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  background: rgba(17,24,39,.92);
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
}}
th, td {{
  padding: 10px 8px;
  border-bottom: 1px solid var(--border);
  text-align: left;
  font-size: 0.95rem;
}}
th {{
  background: rgba(31,41,55,.98);
  position: sticky;
  top: 0;
}}
tr:last-child td {{ border-bottom: none; }}
a {{ color: #92400e; text-decoration: none; }}
.note {{ color: var(--accent); font-weight: 700; }}
.bell {{ color: var(--ok); font-size: 1.2rem; }}
.table-wrap {{
  max-height: 24vh;
  overflow-y: auto;
  border-radius: 14px;
}}
.small-table th, .small-table td {{
  padding: 7px 6px;
  font-size: 0.8rem;
}}
body.is-mobile .recent-group-rows {{
  height: 52vh;
}}
body.is-mobile.compact-mode .recent-group-rows {{
  height: auto;
}}
body.is-mobile .past-group-rows {{
  height: 22vh;
}}
body.is-mobile .compact-table th:nth-child(1), body.is-mobile .compact-table td:nth-child(1) {{
  width: 62px;
}}
body.is-mobile .compact-table th:nth-child(2), body.is-mobile .compact-table td:nth-child(2) {{
  width: 48px;
}}
body.is-mobile .compact-table th, body.is-mobile .compact-table td {{
  font-size: 0.95rem;
  padding: 8px 6px;
}}
body.is-mobile .compact-group-cell {{
  font-size: 2.1rem;
  line-height: 1.05;
}}
body.is-mobile .compact-group-next td {{
  font-size: 1.1rem;
}}
body.is-mobile .compact-group-next .compact-group-cell {{
  font-size: 4rem;
  line-height: 0.94;
}}
body.is-mobile th:nth-child(6), body.is-mobile td:nth-child(6),
body.is-mobile th:nth-child(8), body.is-mobile td:nth-child(8) {{ display: none; }}
body.is-desktop-portrait {{
  font-size: 200%;
}}
body.is-desktop-portrait main {{
  max-width: none;
  padding: 20px;
}}
body.is-desktop-portrait #event {{
  font-size: 1.1rem !important;
}}
body.is-desktop-portrait .view-toggle {{
  font-size: 1.6rem;
  padding: 10px 18px;
}}
body.is-desktop-portrait .group-table th, body.is-desktop-portrait .group-table td,
body.is-desktop-portrait .compact-table th, body.is-desktop-portrait .compact-table td,
body.is-desktop-portrait th, body.is-desktop-portrait td {{
  font-size: inherit;
}}
body.is-desktop-portrait .compact-table th:nth-child(1), body.is-desktop-portrait .compact-table td:nth-child(1) {{
  width: 120px;
}}
body.is-desktop-portrait .compact-table th:nth-child(2), body.is-desktop-portrait .compact-table td:nth-child(2) {{
  width: 96px;
}}
body.is-desktop-portrait .compact-group-cell {{
  font-size: 2.8rem;
  line-height: 1.0;
}}
body.is-desktop-portrait .compact-group-next .compact-group-cell {{
  font-size: 4.2rem;
  line-height: 0.94;
}}
</style>
</head>
<body>
<main>
  <div class="title">
    <div>
      <h1 id="event" style="margin:0;font-size:1.1rem;line-height:1.1;">lapsrv</h1>
    </div>
    <div class="toolbar">
      <button class="view-toggle" id="viewToggle" type="button">Compact View</button>
      <button class="audio-toggle" id="audioToggle" type="button">Tone Off</button>
    </div>
  </div>
  <div class="compact-view compact-wrap">
    <div class="group-rows recent-group-rows" id="compactRecentRows"></div>
  </div>
  <div class="default-view group-wrap">
    <div class="group-title">Recent Groups</div>
    <div class="group-rows recent-group-rows" id="recentGroupRows"></div>
  </div>
  <div class="default-view group-wrap">
    <div class="group-title">Past Groups</div>
    <div class="group-rows past-group-rows" id="pastGroupRows"></div>
  </div>
  <div class="default-view table-wrap" id="tableWrap">
    <table class="small-table">
      <thead>
        <tr>
          <th>Bib</th>
          <th>Note</th>
          <th>Time</th>
          <th>Gap</th>
          <th>Lap</th>
          <th>Name</th>
          <th>Category</th>
          <th>Team</th>
          <th>ETA</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
</main>
<script>
const selectedCategory = {category_js};
const apiPath = {json.dumps(api_path)};
let autoScroll = true;
let compactMode = localStorage.getItem('lapsrv_compact_mode') === '1';
let audioCtx = null;
let lastBellToneKey = '';
let audioEnabled = false;
function esc(s) {{
  return (s ?? '').toString()
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}}
function shortCategory(name) {{
  return (name || '')
    .replaceAll(' (Men)', '(M)')
    .replaceAll(' (Women)', '(W)')
    .replaceAll(' (Open)', '(O)');
}}
function compactNote(note) {{
  return (note || '').replaceAll('st', '').replaceAll('nd', '').replaceAll('rd', '').replaceAll('th', '').replaceAll(':', '-');
}}
function topGroupBellKey(groupTable) {{
  const headers = groupTable.headers || [];
  const rows = groupTable.rows || [];
  const headerStatus = groupTable.header_status || {{}};
  let inTopGroup = false;
  const bellHeaders = [];
  for (const row of rows) {{
    if (row.elapsed_text) {{
      if (inTopGroup) break;
      inTopGroup = true;
    }}
    if (!inTopGroup) continue;
    for (const h of headers) {{
      const cell = (row.cells || {{}})[h];
      if (!cell) continue;
      const status = headerStatus[h] || {{}};
      if (status.is_bell || status.text === '1') bellHeaders.push(h);
    }}
  }}
  bellHeaders.sort();
  return bellHeaders.join('|');
}}
function ensureAudio() {{
  if (audioCtx) return audioCtx;
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) return null;
  audioCtx = new Ctor();
  return audioCtx;
}}
async function armAudio() {{
  audioEnabled = true;
  updateAudioButton();
  const ctx = ensureAudio();
  if (ctx && ctx.state === 'suspended') await ctx.resume().catch(() => {{}});
}}
function playBellTone() {{
  if (!audioEnabled) return;
  const ctx = ensureAudio();
  if (!ctx) return;
  if (ctx.state === 'suspended') {{
    ctx.resume().catch(() => {{}});
  }}
  const now = ctx.currentTime;
  const freqs = [880, 1174];
  freqs.forEach((freq, i) => {{
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(freq, now);
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(0.08, now + 0.02 + i * 0.18);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.16 + i * 0.18);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(now + i * 0.18);
    osc.stop(now + 0.18 + i * 0.18);
  }});
}}
function maybePlayBellTone(groupTable) {{
  const bellKey = topGroupBellKey(groupTable);
  if (bellKey && bellKey !== lastBellToneKey) playBellTone();
  lastBellToneKey = bellKey;
}}

function compactGroupTableHtml(groupTable) {{
  const headers = groupTable.headers || [];
  const rows = groupTable.rows || [];
  const headerStatus = groupTable.header_status || {{}};
  let currentElapsed = '';
  let currentGap = '';
  let groupIndex = -1;
  const lines = [];
  rows.forEach((row, rowIndex) => {{
    if (row.elapsed_text) {{
      groupIndex += 1;
      if (rowIndex > 0) lines.push('<tr class="compact-separator"><td colspan="3"></td></tr>');
      currentElapsed = row.elapsed_text;
      currentGap = row.group_gap || '';
    }}
    headers.forEach(h => {{
      const cell = (row.cells || {{}})[h];
      if (!cell) return;
      const note = compactNote(cell.note || cell.text || '');
      const count = (cell.count || 0) > 1 ? `:${{cell.count}}` : '';
      const status = headerStatus[h] || {{}};
      const lapsToGo = status.is_bell ? '1' : (status.text || '');
      const lapsText = lapsToGo ? ` (${{esc(lapsToGo)}})` : '';
      const groupText = `${{esc(shortCategory(h))}} ${{esc(note)}}${{esc(count)}}${{lapsText}}`.trim();
      const rowClasses = [];
      if (groupIndex === 0) rowClasses.push('compact-group-next');
      if (status.is_bell) rowClasses.push('compact-group-bell');
      if (cell.is_lead) rowClasses.push('compact-group-lead');
      const rowClass = rowClasses.join(' ');
      if (groupIndex === 0) {{
        lines.push(`<tr class="${{rowClass}}"><td class="compact-group-cell" colspan="3">${{groupText}}</td></tr>`);
      }} else {{
        lines.push(`<tr class="${{rowClass}}"><td>${{esc(currentElapsed)}}</td><td>${{esc(currentGap)}}</td><td class="compact-group-cell">${{groupText}}</td></tr>`);
      }}
    }});
  }});
  return `<table class="compact-table"><thead><tr><th>HH:MM:SS</th><th>Gap</th><th>Group</th></tr></thead><tbody>${{lines.join('')}}</tbody></table>`;
}}
function applyViewMode() {{
  document.body.classList.toggle('compact-mode', compactMode);
  const button = document.getElementById('viewToggle');
  if (button) button.textContent = compactMode ? 'Full View' : 'Compact View';
}}
function updateAudioButton() {{
  const button = document.getElementById('audioToggle');
  if (!button) return;
  button.textContent = audioEnabled ? 'Tone On' : 'Tone Off';
  button.classList.toggle('active', audioEnabled);
}}
function applyDeviceClass() {{
  const ua = navigator.userAgent || '';
  const uaDataMobile = navigator.userAgentData && typeof navigator.userAgentData.mobile === 'boolean'
    ? navigator.userAgentData.mobile
    : null;
  const mobileByUA = /iPhone|Android.+Mobile|Windows Phone|iPod/i.test(ua);
  const mobileByTouch = navigator.maxTouchPoints > 1 && Math.min(window.screen.width, window.screen.height) <= 480;
  const isMobile = uaDataMobile !== null ? uaDataMobile : (mobileByUA || mobileByTouch);
  const isDesktopPortrait = !isMobile && window.innerHeight > window.innerWidth;
  document.body.classList.toggle('is-mobile', isMobile);
  document.body.classList.toggle('is-desktop-portrait', isDesktopPortrait);
}}
function groupTableHtml(groupTable) {{
  const headers = groupTable.headers || [];
  const rows = groupTable.rows || [];
  const headerStatus = groupTable.header_status || {{}};
  const ths = ['<th>HH:MM:SS</th>', '<th>Gap</th>', '<th>Total</th>']
    .concat(headers.map(h => `<th>${{esc(h)}}</th>`))
    .join('');
  const statusTds = ['<th></th>', '<th></th>', '<th></th>']
    .concat(headers.map(h => {{
      const status = headerStatus[h] || {{}};
      const cls = status.is_bell ? 'group-header-status bell' : 'group-header-status';
      return `<th class="${{cls}}">${{esc(status.text || '')}}</th>`;
    }}))
    .join('');
  const trs = rows.map(row => {{
    const tds = [`<td>${{esc(row.elapsed_text || '')}}</td>`, `<td>${{esc(row.group_gap || '')}}</td>`, `<td>${{esc(row.total ?? '')}}</td>`]
      .concat(headers.map(h => {{
        const cell = (row.cells || {{}})[h];
        if (!cell) return '<td class="group-empty"></td>';
        const cls = ['group-cell'];
        if (cell.is_lead) cls.push('lead');
        if (cell.is_lapped) cls.push('lapped');
        const note = esc(cell.note || cell.text || '');
        const count = (cell.count || 0) > 1 ? esc(String(cell.count)) : '';
        return `<td class="${{cls.join(' ')}}"><div class="group-cell-content"><span class="group-cell-note">${{note}}</span><span class="group-cell-count">${{count}}</span></div></td>`;
      }}))
      .join('');
    return `<tr>${{tds}}</tr>`;
  }}).join('');
  return `<table class="group-table"><thead><tr>${{statusTds}}</tr><tr>${{ths}}</tr></thead><tbody>${{trs}}</tbody></table>`;
}}
function statusHtml(s) {{
  const endpoint = s.endpoint ? ` ${{esc(s.endpoint)}}` : '';
  return `<span class="badge state-${{esc(s.state)}}">${{esc(s.name)}} [${{esc(s.state)}}]${{endpoint}}</span>`;
}}
function rowHtml(r) {{
  return `<tr>
    <td>${{esc(r.bib)}}</td>
    <td class="note">${{esc(r.note)}}</td>
    <td>${{esc(r.time)}}</td>
    <td>${{esc(r.gap)}}</td>
    <td>${{esc(r.lap ?? '')}}</td>
    <td>${{esc(r.name)}}</td>
    <td>${{esc(r.category)}}</td>
    <td>${{esc(r.team)}}</td>
    <td>${{esc(r.eta)}}</td>
  </tr>`;
}}
const tableWrap = document.getElementById('tableWrap');
const recentGroupRowsWrap = document.getElementById('recentGroupRows');
const pastGroupRowsWrap = document.getElementById('pastGroupRows');
const compactRecentRowsWrap = document.getElementById('compactRecentRows');
const viewToggle = document.getElementById('viewToggle');
const audioToggle = document.getElementById('audioToggle');
applyDeviceClass();
applyViewMode();
updateAudioButton();
window.addEventListener('resize', applyDeviceClass);
viewToggle.addEventListener('click', () => {{
  compactMode = !compactMode;
  localStorage.setItem('lapsrv_compact_mode', compactMode ? '1' : '0');
  applyViewMode();
}});
audioToggle.addEventListener('click', async () => {{
  if (audioEnabled) {{
    audioEnabled = false;
    updateAudioButton();
    return;
  }}
  await armAudio();
  playBellTone();
}});
tableWrap.addEventListener('scroll', () => {{
  const remaining = tableWrap.scrollHeight - tableWrap.scrollTop - tableWrap.clientHeight;
  autoScroll = remaining < 40;
}});
recentGroupRowsWrap.addEventListener('scroll', () => {{
  // recent pane stays top-anchored
}});
pastGroupRowsWrap.addEventListener('scroll', () => {{
  // debug pane; no auto-follow state needed
}});
async function refresh() {{
  const res = await fetch(apiPath, {{cache: 'no-store'}});
  const data = await res.json();
  document.getElementById('event').textContent = data.event_name || 'lapsrv';
  const subsystems = data.subsystems || {{}};
  const recentGroupTable = data.recent_group_table || {{headers: [], header_status: {{}}, rows: []}};
  recentGroupRowsWrap.innerHTML = groupTableHtml(recentGroupTable);
  compactRecentRowsWrap.innerHTML = compactGroupTableHtml(recentGroupTable);
  maybePlayBellTone(recentGroupTable);
  pastGroupRowsWrap.innerHTML = groupTableHtml(data.past_group_table || {{headers: [], header_status: {{}}, rows: []}});
  document.getElementById('rows').innerHTML = (data.passings || []).map(rowHtml).join('');
  if (autoScroll) {{
    tableWrap.scrollTop = tableWrap.scrollHeight;
  }}
}}
refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>"""


class TkMonitor:
    def __init__(self, state: SharedState, on_close: Optional[callable] = None) -> None:
        self.state = state
        self.on_close = on_close
        self.thread: Optional[threading.Thread] = None
        self.logger = logging.getLogger("lapsrv.tk")
        self._closed = False
        self._after_id: Optional[str] = None

    def start(self) -> None:
        if not os.environ.get("DISPLAY") and os.name != "nt":
            self.logger.warning("DISPLAY is not set, skipping tkinter UI")
            return
        self.thread = threading.Thread(target=self._run, name="lapsrv-tk", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        root = tk.Tk()
        root.title("lapsrv")
        root.geometry("1400x900")

        def handle_close() -> None:
            if self._closed:
                return
            self._closed = True
            if self._after_id is not None:
                try:
                    root.after_cancel(self._after_id)
                except Exception:
                    pass
                self._after_id = None
            if self.on_close:
                try:
                    self.on_close()
                except Exception:
                    self.logger.exception("tk close callback failed")
            try:
                root.quit()
            except Exception:
                pass
            try:
                root.destroy()
            except Exception:
                pass

        root.protocol("WM_DELETE_WINDOW", handle_close)

        header = ttk.Label(root, text="lapsrv", font=("TkDefaultFont", 18, "bold"))
        header.pack(fill="x", padx=8, pady=(8, 4))

        recent_frame = ttk.LabelFrame(root, text="Recent Groups")
        recent_frame.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        recent_group_text = tk.Text(recent_frame, wrap="none", height=15, font=("Courier New", 15), state="disabled")
        recent_group_text.tag_configure("header", font=("Courier New", 15, "bold"))
        recent_group_text.tag_configure("lead", background="#facc15", foreground="#111827")
        recent_group_text.tag_configure("bell", background="#facc15", foreground="#111827")
        recent_group_text.tag_configure("lapped", background="#4b5563", foreground="#e5edf6")
        recent_group_text.tag_configure("normal", foreground="#111827")
        recent_group_scroll = ttk.Scrollbar(recent_frame, orient="vertical", command=recent_group_text.yview)
        recent_group_text.configure(yscrollcommand=recent_group_scroll.set)
        recent_group_text.pack(side="left", fill="both", expand=True)
        recent_group_scroll.pack(side="right", fill="y")

        past_frame = ttk.LabelFrame(root, text="Past Groups")
        past_frame.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        past_group_text = tk.Text(past_frame, wrap="none", height=13, font=("Courier New", 13), state="disabled")
        past_group_text.tag_configure("header", font=("Courier New", 13, "bold"))
        past_group_text.tag_configure("lead", background="#facc15", foreground="#111827")
        past_group_text.tag_configure("bell", background="#facc15", foreground="#111827")
        past_group_text.tag_configure("lapped", background="#4b5563", foreground="#e5edf6")
        past_group_text.tag_configure("normal", foreground="#111827")
        past_group_scroll = ttk.Scrollbar(past_frame, orient="vertical", command=past_group_text.yview)
        past_group_text.configure(yscrollcommand=past_group_scroll.set)
        past_group_text.pack(side="left", fill="both", expand=True)
        past_group_scroll.pack(side="right", fill="y")

        pass_frame = ttk.LabelFrame(root, text="Recent Passings")
        pass_frame.pack(fill="both", expand=False, padx=8, pady=(0, 8))
        cols = ("Bib", "Note", "Time", "Gap", "Lap", "Name", "Category", "Team", "ETA")
        pass_tree = ttk.Treeview(pass_frame, columns=cols, show="headings", height=6)
        for col in cols:
            pass_tree.heading(col, text=col)
            width = 90
            if col in {"Name", "Team", "Category"}:
                width = 180
            pass_tree.column(col, width=width, anchor="w" if col in {"Name", "Team", "Category"} else "center")
        scrollbar = ttk.Scrollbar(pass_frame, orient="vertical", command=pass_tree.yview)
        pass_tree.configure(yscrollcommand=scrollbar.set)
        pass_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def repaint() -> None:
            if self._closed or not root.winfo_exists():
                return
            snap = self.state.snapshot()
            subsystems = snap.get("subsystems", {})
            status_parts = []
            for key in ("rfid", "announcer", "lapcounter"):
                sub = subsystems.get(key)
                if not sub:
                    continue
                endpoint = f" {sub['endpoint']}" if sub.get("endpoint") else ""
                status_parts.append(f"{sub['name']} [{sub['state']}]{{{endpoint.strip()}}}" if endpoint else f"{sub['name']} [{sub['state']}]")
            status_text = "   ".join(status_parts)
            header.config(text=f"{snap['event_name'] or 'lapsrv'}    Race {snap['current_race_time'] or ''}    {status_text}")

            def render_group_text(widget: tk.Text, group_table: dict[str, Any]) -> None:
                headers = group_table.get("headers", [])
                header_status = group_table.get("header_status", {})
                rows = group_table.get("rows", [])
                widths = {"HH:MM:SS": 9, "Gap": 8, "Total": 5}
                for header_name in headers:
                    status_text = (header_status.get(header_name) or {}).get("text", "")
                    max_len = max([len(header_name), len(status_text)] + [len((row.get("cells", {}).get(header_name) or {}).get("text", "")) for row in rows])
                    widths[header_name] = max(12, min(max_len + 2, 28))
                widths["Total"] = max(widths["Total"], max([len("Total")] + [len(str(row.get("total", ""))) for row in rows] if rows else [len("Total")]))
                group_lines = []
                header_line = f'{"HH:MM:SS":<{widths["HH:MM:SS"]}}  {"Gap":<{widths["Gap"]}}  {"Total":<{widths["Total"]}}'
                for header_name in headers:
                    header_line += f'  {header_name:<{widths[header_name]}}'
                status_line = f'{"":<{widths["HH:MM:SS"]}}  {"":<{widths["Gap"]}}  {"":<{widths["Total"]}}'
                bell_ranges = []
                offset = widths["HH:MM:SS"] + 2 + widths["Gap"] + 2 + widths["Total"]
                for header_name in headers:
                    offset += 2
                    status = header_status.get(header_name) or {}
                    status_text = f'{status.get("text", ""):<{widths[header_name]}}'
                    status_line += f'  {status_text}'
                    if status.get("is_bell"):
                        bell_ranges.append((offset, offset + len(status_text)))
                    offset += len(status_text)
                group_lines.append((status_line, [], bell_ranges, []))
                group_lines.append((header_line, [], [], []))
                for row in rows:
                    text_parts = [
                        f'{row.get("elapsed_text", ""):<{widths["HH:MM:SS"]}}',
                        f'{row.get("group_gap", ""):<{widths["Gap"]}}',
                        f'{row.get("total", ""):<{widths["Total"]}}',
                    ]
                    lead_ranges = []
                    lapped_ranges = []
                    for header_name in headers:
                        cell = (row.get("cells", {}) or {}).get(header_name)
                        if cell:
                            note_text = cell.get("note", cell.get("text", ""))
                            count_text = str(cell.get("count", "")) if cell.get("count", 0) > 1 else ""
                            if cell.get("is_lapped"):
                                cell_text = f'{note_text} {count_text}'.rstrip()
                                padded = f'{cell_text:>{widths[header_name]}}'
                            elif count_text:
                                gap_width = max(widths[header_name] - len(note_text) - len(count_text), 1)
                                padded = f'{note_text}{" " * gap_width}{count_text}'
                            else:
                                padded = f'{note_text:<{widths[header_name]}}'
                        else:
                            padded = f'{"":<{widths[header_name]}}'
                        start = sum(len(part) for part in text_parts) + (2 * len(text_parts))
                        text_parts.append(padded)
                        if cell and cell.get("is_lead"):
                            lead_ranges.append((start, start + len(padded)))
                        elif cell and cell.get("is_lapped"):
                            lapped_ranges.append((start, start + len(padded)))
                    group_lines.append(("  ".join(text_parts), lead_ranges, [], lapped_ranges))
                widget.configure(state="normal")
                widget.delete("1.0", "end")
                widget.tag_remove("lead", "1.0", "end")
                widget.tag_remove("bell", "1.0", "end")
                widget.tag_remove("lapped", "1.0", "end")
                for line, lead_ranges, bell_ranges, lapped_ranges in group_lines:
                    start_idx = widget.index("end-1c")
                    widget.insert("end", line + "\n")
                    line_no = start_idx.split('.')[0]
                    for col_start, col_end in lead_ranges:
                        if col_end > col_start:
                            widget.tag_add("lead", f"{line_no}.{col_start}", f"{line_no}.{col_end}")
                    for col_start, col_end in bell_ranges:
                        if col_end > col_start:
                            widget.tag_add("bell", f"{line_no}.{col_start}", f"{line_no}.{col_end}")
                    for col_start, col_end in lapped_ranges:
                        if col_end > col_start:
                            widget.tag_add("lapped", f"{line_no}.{col_start}", f"{line_no}.{col_end}")
                widget.configure(state="disabled")
                widget.see("1.0")

            render_group_text(recent_group_text, snap.get("recent_group_table", {"headers": [], "header_status": {}, "rows": []}))
            render_group_text(past_group_text, snap.get("past_group_table", {"headers": [], "header_status": {}, "rows": []}))
            for item in pass_tree.get_children():
                pass_tree.delete(item)
            for row in snap["passings"]:
                pass_tree.insert(
                    "",
                    "end",
                    values=(
                        row["bib"],
                        row["note"],
                        row["time"],
                        row["gap"],
                        "" if row["lap"] is None else row["lap"],
                        row["name"],
                        row["category"],
                        row["team"],
                        row["eta"],
                    ),
                )
            children = pass_tree.get_children()
            if children:
                pass_tree.see(children[-1])
            if not self._closed and root.winfo_exists():
                self._after_id = root.after(750, repaint)

        try:
            repaint()
            root.mainloop()
        finally:
            handle_close()


class LapsrvApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config = RaceConfig.load(Path(args.xlsx))
        self.state = SharedState(self.config, show_unknown_tags=args.show_unknown_tags)
        self.jchip = JChipServer(self.state, args.listen_host, args.port)
        self.crossmgr = CrossMgrClient(self.state, args.crossmgr_host, args.crossmgr_port)
        self.web = WebServer(self.state, args.web_host, args.web)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.tk = TkMonitor(self.state, on_close=self.request_stop)
        self.stop_event = asyncio.Event()
        self.logger = logging.getLogger("lapsrv.app")

    def request_stop(self) -> None:
        self.logger.info("shutdown requested")
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.stop_event.set)
        else:
            self.stop_event.set()

    async def _stop_with_timeout(self, name: str, awaitable: Any, timeout: float = 2.0) -> None:
        self.logger.info("shutdown: stopping %s", name)
        try:
            await asyncio.wait_for(awaitable, timeout=timeout)
            self.logger.info("shutdown: stopped %s", name)
        except asyncio.TimeoutError:
            self.logger.warning("shutdown: timed out stopping %s", name)
        except Exception:
            self.logger.exception("shutdown: error stopping %s", name)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self.loop = loop
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except NotImplementedError:
                pass

        if not self.args.no_gui:
            self.tk.start()

        await self.jchip.start()
        await self.web.start()
        crossmgr_task = asyncio.create_task(self.crossmgr.run(), name="crossmgr-client")
        try:
            await self.stop_event.wait()
        finally:
            self.logger.info("shutdown: begin")
            await self._stop_with_timeout("crossmgr", self.crossmgr.stop())
            self.logger.info("shutdown: cancelling crossmgr task")
            crossmgr_task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(crossmgr_task, return_exceptions=True), timeout=2.0)
                self.logger.info("shutdown: crossmgr task cancelled")
            except asyncio.TimeoutError:
                self.logger.warning("shutdown: timed out waiting for crossmgr task")
            await self._stop_with_timeout("web", self.web.stop())
            await self._stop_with_timeout("jchip", self.jchip.stop())
            self.logger.info("shutdown: complete")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="lapsrv early-read monitor for CrossMgr races")
    parser.add_argument("--xlsx", required=True, help="CrossMgr XLSX configuration file")
    parser.add_argument("--crossmgr", default="localhost:8765", help="CrossMgr host[:port], default localhost:8765")
    parser.add_argument("--port", type=int, default=DEFAULT_JCHIP_PORT, help=f"JChip listen port, default {DEFAULT_JCHIP_PORT}")
    parser.add_argument("--web", type=int, default=DEFAULT_WEB_PORT, help=f"Web UI port, default {DEFAULT_WEB_PORT}")
    parser.add_argument("--listen-host", default="0.0.0.0", help="JChip listen host, default 0.0.0.0")
    parser.add_argument("--web-host", default="0.0.0.0", help="Web listen host, default 0.0.0.0")
    parser.add_argument("--no-gui", action="store_true", help="Disable the tkinter window")
    parser.add_argument(
        "--show_unknown_tags",
        action="store_true",
        help="Show unknown tags using the first 12 EPC characters; otherwise ignore them",
    )
    parser.add_argument("--log-level", default="INFO", help="Logging level, default INFO")
    return parser


async def async_main(args: argparse.Namespace) -> None:
    app = LapsrvApp(args)
    await app.run()


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    args.crossmgr_host, args.crossmgr_port = parse_host_port(args.crossmgr, DEFAULT_CROSSMGR_PORT)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
