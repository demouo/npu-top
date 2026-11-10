#!/usr/bin/env python3
"""A read-only terminal dashboard for Ascend NPU telemetry."""

from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import termios
import threading
import time
import tty
from typing import Callable

from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


BG = "#0b1220"
PANEL = "#101b2b"
EDGE = "#26364c"
FG = "#e4edf7"
MUTED = "#8295ad"
CYAN = "#67d4ed"
GREEN = "#7ee2b8"
PURPLE = "#b6a0fa"
AMBER = "#f2c078"
RED = "#ff8091"
HISTORY_SECONDS = 60
__version__ = "0.1.0"


@dataclass(frozen=True)
class Device:
    board: int
    chip: int
    physical: int
    name: str
    health: str
    power: float | None
    temperature: float | None
    utilization: float | None
    memory_used: float | None
    memory_total: float | None
    memory_kind: str


@dataclass(frozen=True)
class Process:
    board: int
    chip: int
    pid: int
    name: str
    memory: float | None


@dataclass(frozen=True)
class Snapshot:
    devices: list[Device]
    processes: list[Process]
    sampled_at: float
    duration: float
    processes_available: bool
    layout: str = ""
    util_label: str = "AI CORE"


def number(value: str) -> float | None:
    try:
        result = float(value)
    except ValueError:
        return None
    return result if math.isfinite(result) and result >= 0 else None


VALUE_PATTERN = r"(?:[+-]?(?:\d+(?:\.\d*)?|\.\d+)|N/A|NA|nan|[+-]?inf|-)"
INCOMPLETE = "Could not read complete NPU metrics; inspect 'npu-smi info' and driver access"


def table_lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip().startswith("|")]


def split_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def clean_name(value: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", value)


def memory_columns_from(header: str) -> list[str]:
    return [name.upper() for name in re.findall(r"(HBM|Memory|DDR)-Usage\((?:MB|MiB)\)", header, re.IGNORECASE)]


def memory_measurement(text: str, memory_columns: list[str]) -> tuple[float | None, float | None, str]:
    pairs = re.findall(rf"({VALUE_PATTERN})\s*/\s*({VALUE_PATTERN})", text, re.IGNORECASE)
    if not memory_columns or len(pairs) != len(memory_columns):
        raise ValueError("Unsupported npu-smi memory layout; inspect 'npu-smi info'")
    index = memory_columns.index("HBM") if "HBM" in memory_columns else 0
    used, total = map(number, pairs[index])
    return used, total, memory_columns[index]


def process_row(cells: list[str]) -> Process | None:
    """Read one process row from either the 4-column or the 5-column process table."""
    if len(cells) == 4 and re.fullmatch(r"\d+\s+\d+", cells[0]) and cells[1].isdigit():
        board, chip = (int(part) for part in cells[0].split())
        return Process(board, chip, int(cells[1]), clean_name(cells[2]), number(cells[3]))
    if len(cells) == 5 and cells[0].isdigit() and cells[1].isdigit():
        return Process(int(cells[0]), 0, int(cells[1]), clean_name(cells[2]), number(cells[3]))
    return None


def parse_npu_name_layout(lines: list[str], duration: float) -> Snapshot:
    """NPU Name + Chip Phy-ID rows with AICore(%): 910, 910B, 910C, and 310-series output."""
    devices, processes = [], []
    pending = None
    process_section = False
    memory_columns = []
    for line in lines:
        cells = split_cells(line)
        if re.search(r"Process\s+id", line, re.IGNORECASE):
            process_section = True
            continue
        if process_section:
            process = process_row(cells)
            if process is not None:
                processes.append(process)
            continue
        if len(cells) == 3 and "AICore(%)" in cells[2]:
            memory_columns = memory_columns_from(cells[2])
            continue
        if len(cells) != 3:
            continue
        identity = cells[0].split()
        metrics = cells[2].split()
        if len(identity) >= 2 and identity[0].isdigit() and not identity[1].isdigit():
            if pending is not None or len(metrics) < 2:
                raise ValueError("npu-smi returned incomplete device information")
            pending = (int(identity[0]), " ".join(identity[1:]), cells[1], number(metrics[0]), number(metrics[1]))
        elif pending is not None and len(identity) == 2 and all(part.isdigit() for part in identity):
            used, total, kind = memory_measurement(cells[2], memory_columns)
            board, name, health, power, temperature = pending
            devices.append(Device(board, int(identity[0]), int(identity[1]), name, health,
                                  power, temperature, number(metrics[0]) if metrics else None,
                                  used, total, kind))
            pending = None
    if pending is not None or not devices:
        raise ValueError(INCOMPLETE)
    devices.sort(key=lambda device: (device.board, device.chip))
    return Snapshot(devices, processes, time.time(), duration, process_section, "ascend_910", "AI CORE")


def parse_npu_id_layout(lines: list[str], duration: float) -> Snapshot:
    """NPU ID | Name | Health | NPU Util(%) rows: npu-smi 25.1 output such as Ascend 950DT."""
    devices, processes = [], []
    pending = None
    process_section = False
    memory_columns = []
    for line in lines:
        cells = split_cells(line)
        if re.search(r"Process\s+id", line, re.IGNORECASE):
            process_section = True
            continue
        if process_section:
            process = process_row(cells)
            if process is not None:
                processes.append(process)
            continue
        if len(cells) == 4 and "NPU Util(%)" in cells[3]:
            memory_columns = memory_columns_from(cells[3])
            continue
        if len(cells) != 4:
            continue
        metrics = cells[3].split()
        if cells[0].isdigit() and cells[1] and not cells[1].isdigit():
            if pending is not None or len(metrics) < 2:
                raise ValueError("npu-smi returned incomplete device information")
            pending = (int(cells[0]), cells[1], cells[2], number(metrics[0]), number(metrics[1]))
        elif pending is not None and not cells[0] and not cells[1]:
            used, total, kind = memory_measurement(cells[3], memory_columns)
            board, name, health, power, temperature = pending
            devices.append(Device(board, 0, board, name, health, power, temperature,
                                  number(metrics[0]) if metrics else None, used, total, kind))
            pending = None
    if pending is not None or not devices:
        raise ValueError(INCOMPLETE)
    devices.sort(key=lambda device: (device.board, device.chip))
    return Snapshot(devices, processes, time.time(), duration, process_section, "ascend_950", "NPU UTIL")


@dataclass(frozen=True)
class LayoutSpec:
    key: str
    description: str
    detect: Callable[[list[str]], bool]
    parse: Callable[[list[str], float], Snapshot]


LAYOUTS: list[LayoutSpec] = []


def register_layout(layout: LayoutSpec) -> LayoutSpec:
    LAYOUTS.append(layout)
    return layout


def load_layout_plugins(directories: list[str]) -> None:
    """Load external layout adapters. Each ``*.py`` file must define ``LAYOUT``."""
    for directory in directories:
        path = Path(directory)
        if not path.is_dir():
            raise ValueError(f"Plugin directory not found: {directory}")
        for file in sorted(path.glob("*.py")):
            spec = importlib.util.spec_from_file_location(f"npu_top_layout_{file.stem}", file)
            if spec is None or spec.loader is None:
                raise ValueError(f"Could not load layout plugin: {file}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            layout = getattr(module, "LAYOUT", None)
            key = getattr(layout, "key", None)
            detect = getattr(layout, "detect", None)
            parse = getattr(layout, "parse", None)
            if not isinstance(key, str) or not key or not callable(detect) or not callable(parse):
                raise ValueError(f"Layout plugin {file} must define LAYOUT with a string key, detect, and parse")
            if any(existing.key == key for existing in LAYOUTS):
                raise ValueError(f"Layout plugin {file} reuses layout key {key!r}")
            register_layout(LayoutSpec(key, str(getattr(layout, "description", "")), detect, parse))


register_layout(LayoutSpec("ascend_910", "NPU Name and Chip Phy-ID rows with AICore(%)",
                           lambda lines: any("AICore(%)" in line for line in lines), parse_npu_name_layout))
register_layout(LayoutSpec("ascend_950", "NPU ID, Name, and Health rows with NPU Util(%)",
                           lambda lines: any("NPU Util(%)" in line for line in lines), parse_npu_id_layout))


def parse_info(output: str, duration: float = 0, layout: str | None = None) -> Snapshot:
    lines = table_lines(output)
    candidates = [candidate for candidate in LAYOUTS if layout is None or candidate.key == layout]
    if not candidates:
        raise ValueError(f"Unknown layout {layout!r}; available: {', '.join(item.key for item in LAYOUTS)}")
    for candidate in candidates:
        if candidate.detect(lines):
            snapshot = candidate.parse(lines, duration)
            return snapshot if snapshot.layout else replace(snapshot, layout=candidate.key)
    if layout is not None:
        raise ValueError(f"npu-smi output does not match layout {layout!r}")
    raise ValueError(INCOMPLETE)


def collect(command: str, stop: threading.Event, layout: str | None = None) -> Snapshot | None:
    started = time.monotonic()
    with subprocess.Popen([command, "info"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, errors="replace", env={**os.environ, "LC_ALL": "C"}) as process:
        try:
            while not stop.is_set():
                try:
                    output, error = process.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() - started >= 10:
                        raise RuntimeError("npu-smi query exceeded 10 seconds")
            else:
                return None
            if process.returncode:
                detail = " ".join((error or output).split())[:180]
                raise RuntimeError(f"npu-smi exited with code {process.returncode}: {detail or 'no error details'}")
            return parse_info(output, time.monotonic() - started, layout)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()


def poll(command: str, interval: float, stop: threading.Event, updates: queue.SimpleQueue,
         layout: str | None = None) -> None:
    while not stop.is_set():
        started = time.monotonic()
        try:
            snapshot = collect(command, stop, layout)
            if snapshot is not None:
                updates.put((snapshot, None))
        except (OSError, ValueError, RuntimeError) as error:
            updates.put((None, str(error)))
        stop.wait(max(0.1, interval - (time.monotonic() - started)))


def meter(percent: float | None, width: int, color: str) -> Text:
    if percent is None:
        return Text("·" * width, style=EDGE)
    filled = round(max(0, min(100, percent)) * width / 100)
    result = Text("━" * filled, style=color)
    result.append("─" * (width - filled), style=EDGE)
    return result


def sparkline(points: deque, now: float, width: int) -> Text:
    buckets: list[list[float]] = [[] for _ in range(width)]
    for sampled_at, value in points:
        index = int((sampled_at - (now - HISTORY_SECONDS)) / HISTORY_SECONDS * width)
        if 0 <= index < width and value is not None and sampled_at > now - HISTORY_SECONDS:
            buckets[index].append(value)
    result = Text()
    for bucket in buckets:
        if not bucket:
            result.append("·", style=EDGE)
        else:
            value = max(0, min(100, sum(bucket) / len(bucket)))
            result.append("▁▂▃▄▅▆▇█"[min(7, int(value * 8 / 100))],
                          style=GREEN if value >= 80 else CYAN)
    return result


def summary(devices: list[Device], compact: bool, util_label: str = "AI CORE"):
    usage = [d.utilization for d in devices if d.utilization is not None]
    memory = [d for d in devices if d.memory_used is not None and d.memory_total is not None]
    temperatures = [d.temperature for d in devices if d.temperature is not None]
    # npu-smi reports board power on the first die; do not count a board twice.
    power_by_board = {}
    for device in devices:
        if device.power is not None:
            power_by_board.setdefault(device.board, device.power)
    average = f"{sum(usage) / len(usage):.1f}%" if usage else "N/A"
    memory_value = f"{sum(d.memory_used for d in memory) / 1024:.1f} GiB" if memory else "N/A"
    memory_kind = "HBM" if devices and all(d.memory_kind == "HBM" for d in devices) else "MEMORY"
    memory_total = f"of {sum(d.memory_total for d in memory) / 1024:,.0f} GiB {memory_kind}" if memory else "Memory unavailable"
    peak = f"{max(temperatures):.0f}°C" if temperatures else "N/A"
    watts = f"{sum(power_by_board.values()) / 1000:.2f} kW" if power_by_board else "N/A"
    cards = [
        (util_label, average, f"{sum(value > 0 for value in usage)} active / {len(usage)} dies", CYAN),
        (f"{memory_kind} USED", memory_value, memory_total, PURPLE),
        ("PEAK TEMP", peak, f"{len(temperatures)} dies reported", AMBER),
        ("BOARD POWER", watts, f"{len(power_by_board)} boards reported", GREEN),
    ]
    table = Table.grid(expand=True, padding=(0, 1))
    for _ in cards:
        table.add_column(ratio=1)
    if compact:
        table.add_row(*[Text.assemble((label + "\n", MUTED), (value, f"bold {color}"))
                       for label, value, _, color in cards])
        return table
    table.add_row(*[
        Panel(Text.assemble((value + "\n", f"bold {color}"), (detail, MUTED)),
              title=Text(label, style=MUTED), title_align="left", border_style=EDGE,
              style=f"{FG} on {PANEL}", box=box.ROUNDED, height=5)
        for label, value, detail, color in cards
    ])
    return table


def device_table(devices: list[Device], history: dict, now: float, width: int,
                 util_label: str = "AI CORE") -> Table:
    table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1), show_edge=False,
                  border_style=EDGE, header_style=MUTED)
    table.add_column("DIE", no_wrap=True, width=5)
    if width >= 90:
        table.add_column("CARD.CHIP", no_wrap=True, width=9)
    table.add_column(util_label, no_wrap=True, ratio=1)
    memory_label = "HBM GiB" if all(d.memory_kind == "HBM" for d in devices) else "MEM GiB"
    table.add_column(memory_label, no_wrap=True, ratio=1)
    table.add_column("TEMP", no_wrap=True, justify="right", width=5)
    if width >= 100:
        table.add_column("BOARD W", no_wrap=True, justify="right", width=7)
    if width >= 115:
        table.add_column(f"{util_label} · 60s", no_wrap=True, width=18)
    for device in devices:
        color = GREEN if device.health.upper() == "OK" else (AMBER if device.health in {"-", "N/A"} else RED)
        row = [Text.assemble(("● ", color), (f"{device.physical:02}", f"bold {FG}"))]
        if width >= 90:
            row.append(Text(f"{device.board}.{device.chip}", style=MUTED))
        usage = meter(device.utilization, 10 if width >= 100 else 6, CYAN)
        usage.append(f" {device.utilization:5.1f}%" if device.utilization is not None else "   N/A", style=FG)
        row.append(usage)
        if device.memory_used is None or device.memory_total is None:
            memory = Text("N/A", style=MUTED)
        else:
            percent = device.memory_used / device.memory_total * 100 if device.memory_total else None
            color = AMBER if percent is not None and percent >= 90 else PURPLE
            memory = meter(percent, 7, color) if width >= 115 else Text()
            memory.append(f" {device.memory_used / 1024:4.1f}/{device.memory_total / 1024:.0f}", style=FG)
        row.append(memory)
        temp = device.temperature
        row.append(Text(f"{temp:.0f}°" if temp is not None else "N/A",
                        style=RED if temp is not None and temp >= 85 else (AMBER if temp is not None and temp >= 75 else GREEN)))
        if width >= 100:
            row.append(Text(f"{device.power:.0f}" if device.power is not None else "·", style=MUTED))
        if width >= 115:
            row.append(sparkline(history.get(device.physical, deque()), now, 18))
        table.add_row(*row, style=f"on {PANEL}" if device.board % 2 == 0 else f"on {BG}")
    return table


def process_panel(processes: list[Process], height: int, available: bool) -> Panel:
    if not available:
        content = Text("Process information unavailable", style=MUTED)
    elif not processes:
        content = Text.assemble(("●  ", GREEN), ("No running NPU processes", MUTED))
    else:
        table = Table(box=None, expand=True, padding=(0, 1), header_style=MUTED)
        for label in ("CARD.CHIP", "PID", "PROCESS", "MEM MiB"):
            table.add_column(label, no_wrap=True, overflow="ellipsis", ratio=3 if label == "PROCESS" else 1)
        limit = max(1, height - 4)
        for process in sorted(processes, key=lambda p: p.memory or 0, reverse=True)[:limit]:
            table.add_row(f"{process.board}.{process.chip}", str(process.pid), Text(process.name, style=FG),
                          f"{process.memory:,.0f}" if process.memory is not None else "N/A")
        remaining = len(processes) - limit
        note = Text(f"  +{remaining} more allocations" if remaining > 0 else "", style=MUTED)
        content = Group(table, note)
    return Panel(content, title=Text("PROCESSES", style=MUTED), title_align="left",
                 subtitle=Text(f"{len({p.pid for p in processes})} unique PIDs" if available else "Not reported by driver", style=MUTED),
                 border_style=EDGE, box=box.ROUNDED, padding=(0, 1))


def dashboard(snapshot: Snapshot | None, error: str | None, history: dict,
              interval: float, width: int, height: int, page: int = 0) -> Layout:
    now = time.time()
    devices = snapshot.devices if snapshot else []
    compact = width < 100 or height < 30
    stats_height = 2 if compact else 5
    process_height = min(8, height - 30) if height >= 36 and width >= 90 else 0
    rows_per_page = max(1, height - 3 - stats_height - process_height - 2 - 4)
    pages = max(1, math.ceil(len(devices) / rows_per_page))
    page %= pages
    header = Table.grid(expand=True)
    header.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    header.add_column(justify="right", no_wrap=True)
    state, state_color = ("RETRY", AMBER) if error else (("LIVE", GREEN) if snapshot else ("WAIT", MUTED))
    header.add_row(Text.assemble((" NPU TOP", f"bold {FG}"), ("  /  ASCEND", CYAN)),
                   Text.assemble((f"● {state}  ", state_color), (datetime.now().strftime("%H:%M:%S") + " ", MUTED)))
    boards = len({d.board for d in devices})
    description = f"{boards} boards / {len(devices)} dies" if snapshot else "connecting to npu-smi"
    header.add_row(Text(f" {socket.gethostname()}  ·  {description}", style=MUTED),
                   Text(f"every {interval:g}s ", style=MUTED))
    if width < 60 or height < 16:
        return Layout(Panel(Text("Resize the terminal to at least 60 columns by 16 rows.\nPress q to quit.", style=AMBER),
                            style=f"{FG} on {BG}", border_style=EDGE), size=height)
    selected = devices[page * rows_per_page:(page + 1) * rows_per_page]
    util_label = snapshot.util_label if snapshot else "AI CORE"
    content = device_table(selected, history, now, width, util_label) if snapshot else Text("\n  Reading NPU telemetry…", style=MUTED)
    bad_health = [d for d in devices if d.health.upper() != "OK"]
    health = f"{len(bad_health)} CHECK HEALTH" if bad_health else ("ALL HEALTHY" if devices else "CONNECTING")
    panel = Panel(content, title=Text("DEVICES", style=MUTED), title_align="left",
                  subtitle=Text(f"{health}  ·  {page + 1}/{pages}", style=AMBER if bad_health else MUTED),
                  border_style=EDGE, box=box.ROUNDED, padding=(0, 1))
    if error:
        status = Text(" RETRY · " + error + (f" · last success {now - snapshot.sampled_at:.0f}s ago" if snapshot else ""),
                      style=AMBER, overflow="ellipsis", no_wrap=True)
    elif snapshot:
        process_count = f"{len({p.pid for p in snapshot.processes})} processes" if snapshot.processes_available else "processes unavailable"
        status = Text(f" Updated {datetime.fromtimestamp(snapshot.sampled_at):%H:%M:%S}"
                      f"  ·  query {snapshot.duration:.2f}s  ·  {process_count}"
                      "  ·  board power shown once", style=MUTED, no_wrap=True, overflow="ellipsis")
    else:
        status = Text(" Waiting for the first sample…", style=MUTED)
    controls = Text.assemble((" q ", f"bold {CYAN}"), ("quit   ", MUTED),
                             ("← → / n p ", f"bold {CYAN}"), ("page   ", MUTED),
                             ("Ctrl+C ", f"bold {CYAN}"), ("exit", MUTED))
    layout = Layout(size=height)
    sections = [Layout(header, size=3), Layout(summary(devices, compact, util_label), size=stats_height), Layout(panel)]
    if process_height:
        sections.append(Layout(process_panel(snapshot.processes if snapshot else [], process_height,
                                             snapshot.processes_available if snapshot else False), size=process_height))
    sections.append(Layout(Group(status, controls), size=2))
    layout.split_column(*sections)
    return layout


@contextmanager
def keyboard():
    original = termios.tcgetattr(sys.stdin) if sys.stdin.isatty() else None
    try:
        if original is not None:
            tty.setcbreak(sys.stdin.fileno())
        yield
    finally:
        if original is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original)


def interval_value(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0.5:
        raise argparse.ArgumentTypeError("The interval must be a finite number of at least 0.5 seconds")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="npu-top", description="Read-only Ascend NPU dashboard. Press q to quit, or use arrow keys to change pages.")
    parser.add_argument("-i", "--interval", type=interval_value, default=2.0, metavar="SECONDS", help="Sampling interval, default: 2 seconds")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--once", action="store_true", help="Print one snapshot and exit")
    modes.add_argument("--json", action="store_true", help="Print one snapshot as JSON and exit")
    modes.add_argument("--doctor", action="store_true", help="Check driver access and output compatibility")
    parser.add_argument("--npu-smi", default=os.environ.get("NPU_SMI"), metavar="PATH", help="Path to npu-smi; defaults to NPU_SMI, PATH, then the standard driver directory")
    parser.add_argument("--layout", default=None, metavar="KEY", help="Force one layout instead of detecting it; --doctor lists the layouts")
    parser.add_argument("--plugins", action="append", default=[], metavar="DIR", help="Directory of layout plugin files; can repeat. NPU_TOP_PLUGINS adds more")
    parser.add_argument("--no-color", action="store_true", help="Disable colors")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    console = Console(style=f"{FG} on {BG}", no_color=args.no_color, stderr=args.json)
    command = shutil.which(args.npu_smi or "npu-smi")
    if command is None and not args.npu_smi:
        command = shutil.which("/usr/local/Ascend/driver/tools/npu-smi")
    if command is None:
        console.print("npu-smi was not found. Install the Ascend driver or set --npu-smi /path/to/npu-smi.", style=RED)
        return 1
    plugin_directories = list(args.plugins)
    plugin_directories += [entry for entry in os.environ.get("NPU_TOP_PLUGINS", "").split(os.pathsep) if entry]
    try:
        load_layout_plugins(plugin_directories)
    except (OSError, ValueError, ImportError, SyntaxError) as error:
        console.print(Text(str(error), style=RED))
        return 1
    if args.layout is not None and all(item.key != args.layout for item in LAYOUTS):
        console.print(Text(f"Unknown layout {args.layout!r}; available: {', '.join(item.key for item in LAYOUTS)}", style=RED))
        return 1
    stop = threading.Event()
    if args.once or args.json or args.doctor or not (sys.stdout.isatty() and sys.stdin.isatty()):
        try:
            snapshot = collect(command, stop, args.layout)
        except (OSError, ValueError, RuntimeError) as error:
            console.print(Text(str(error), style=RED))
            return 1
        if args.json:
            print(json.dumps(asdict(snapshot), ensure_ascii=False, allow_nan=False))
            return 0
        if args.doctor:
            console.print(Text(f"OK: npu-smi at {command}\nBoards: {len({d.board for d in snapshot.devices})}; "
                               f"dies: {len(snapshot.devices)}; query: {snapshot.duration:.2f}s.\n"
                               f"Memory types: {', '.join(sorted({d.memory_kind for d in snapshot.devices}))}.\n"
                               f"Process table: {'available' if snapshot.processes_available else 'not reported'}.\n"
                               f"Layout: {snapshot.layout} ({snapshot.util_label}).\n"
                               f"Available layouts: {', '.join(item.key for item in LAYOUTS)}."))
            return 0
        history = {d.physical: deque([(snapshot.sampled_at, d.utilization)]) for d in snapshot.devices}
        height = max(38, len(snapshot.devices) + 22) if console.width >= 90 else max(16, len(snapshot.devices) + 11)
        console.print(dashboard(snapshot, None, history, args.interval, console.width, height), height=height)
        return 0
    updates = queue.SimpleQueue()
    worker = threading.Thread(target=poll, args=(command, args.interval, stop, updates, args.layout), daemon=True)
    snapshot, error, history, page = None, None, {}, 0
    worker.start()
    try:
        with keyboard(), Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                while not updates.empty():
                    received, error = updates.get_nowait()
                    if received is not None:
                        snapshot = received
                        for device in snapshot.devices:
                            points = history.setdefault(device.physical, deque())
                            points.append((snapshot.sampled_at, device.utilization))
                for points in history.values():
                    while points and points[0][0] < time.time() - HISTORY_SECONDS:
                        points.popleft()
                live.update(dashboard(snapshot, error, history, args.interval,
                                      console.width, console.height, page), refresh=True)
                if select.select([sys.stdin], [], [], 0.25)[0]:
                    keys = os.read(sys.stdin.fileno(), 64).lower()
                    if not keys or b"q" in keys or b"\x04" in keys:
                        break
                    if b"n" in keys or b"\x1b[c" in keys:
                        page += 1
                    if b"p" in keys or b"\x1b[d" in keys:
                        page -= 1
    finally:
        stop.set()
        worker.join(timeout=2)
    return 0


def cli() -> int:
    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        return main()
    except KeyboardInterrupt:
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(cli())
