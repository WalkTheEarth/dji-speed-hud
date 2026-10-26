"""DJI video -> burned speed-HUD video.

Shared backend for the GUI. The stages mirror the original one-off scripts:
index the djmd packet track, parse the protobuf telemetry, build a 1 Hz speed
series, render the HUD frames, and burn them onto the source footage.

Only the *current* speed blanks on a GPS dropout; session min/max stay put.
"""

from __future__ import annotations

import csv
import json
import math
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

import hud

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CREATE_FLAGS = CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _find_tool(name: str) -> str:
    override = os.environ.get(name.upper())
    if override:
        return override
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "darwin":
        for candidate in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
            if Path(candidate).exists():
                return candidate
    return name


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")


def _tool_ready(path: str) -> bool:
    return bool(shutil.which(path) or (Path(path).is_file()))


def check_tools() -> None:
    missing = [name for name, path in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE))
               if not _tool_ready(path)]
    if missing:
        raise PipelineError(
            "Missing " + " and ".join(missing) + ".\n"
            "Install FFmpeg and make sure ffmpeg/ffprobe are on PATH:\n"
            "  Windows: winget install Gyan.FFmpeg\n"
            "  macOS:   brew install ffmpeg\n"
            "  Linux:   sudo apt install ffmpeg"
        )

MPH_PER_MPS = 2.2369362920544
KMH_PER_MPS = 3.6
OVERLAY_FPS = 24

ProgressFn = Callable[[str, float, str], None]
LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]

# MP4 can hold these audio codecs as-is.
_COPY_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac"}


class PipelineError(RuntimeError):
    pass


class Cancelled(PipelineError):
    pass


@dataclass
class VideoInfo:
    path: Path
    duration: float
    width: int                 # display dimensions (rotation applied)
    height: int
    rotation: int
    fps: float
    video_index: int
    audio_index: Optional[int]
    audio_codec: Optional[str]
    data_index: int
    data_packets: int

    @property
    def duration_hms(self) -> str:
        return fmt_hms(self.duration)


@dataclass
class SpeedStats:
    max_mph: float
    min_mph: float
    avg_moving_mph: float
    valid_seconds: int
    total_seconds: int
    distance_km: float


@dataclass
class PipelineResult:
    output: Path
    info: VideoInfo
    stats: SpeedStats
    elapsed: float
    work_dir: Path
    frames: int


def fmt_hms(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _as_float(value) -> Optional[float]:
    try:
        if value in (None, "", "N/A"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _check_cancel(cancelled: Optional[CancelFn]) -> None:
    if cancelled and cancelled():
        raise Cancelled("Cancelled")


def _fps_from(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key) or ""
        if "/" in raw:
            num, den = raw.split("/", 1)
            try:
                if float(den):
                    return float(num) / float(den)
            except ValueError:
                pass
        else:
            try:
                return float(raw)
            except ValueError:
                pass
    return 0.0


def probe_video(path: str | Path) -> VideoInfo:
    """Read stream layout and find the DJI telemetry (djmd) data track."""
    path = Path(path)
    if not path.is_file():
        raise PipelineError(f"Input video not found:\n{path}")
    check_tools()

    cmd = [
        FFPROBE, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True,
                              creationflags=_CREATE_FLAGS)
    except FileNotFoundError as exc:
        raise PipelineError("ffprobe is not installed or not on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or "").strip().splitlines()[-3:]
        raise PipelineError("ffprobe could not read this file.\n" + "\n".join(tail)) from exc

    meta = json.loads(proc.stdout or "{}")
    streams = meta.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"]
    if not videos:
        raise PipelineError("No video stream found in this file.")
    video = videos[0]

    duration = _as_float(video.get("duration"))
    if not duration or duration <= 0:
        duration = _as_float((meta.get("format") or {}).get("duration"))
    if not duration or duration <= 0:
        raise PipelineError("Could not determine the video duration.")

    rotation = 0
    for side_data in video.get("side_data_list") or []:
        if "rotation" in side_data:
            value = _as_float(side_data.get("rotation"))
            if value is not None:
                rotation = int(round(value))
                break
    if rotation == 0:
        value = _as_float((video.get("tags") or {}).get("rotate"))
        if value is not None:
            rotation = int(round(value))
    rotation %= 360

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if rotation % 180 == 90:
        width, height = height, width

    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    def packets(stream: dict) -> int:
        for key in ("nb_read_packets", "nb_frames"):
            try:
                value = int(stream.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value:
                return value
        return 0

    def tag(stream: dict) -> str:
        return str(stream.get("codec_tag_string") or "").lower()

    # The MP4 timecode track is also a data stream; the djmd track is not.
    data_streams = [
        s for s in streams
        if s.get("codec_type") == "data" and tag(s) != "tmcd"
    ]
    if not data_streams:
        raise PipelineError(
            "No DJI telemetry track (djmd) found in this video.\n"
            "This tool needs footage from a DJI camera that records GPS telemetry."
        )
    preferred = [
        s for s in data_streams
        if any(name in tag(s) for name in ("djmd", "dvtm", "mebx"))
    ]
    data = max(preferred or data_streams, key=packets)

    return VideoInfo(
        path=path,
        duration=duration,
        width=width,
        height=height,
        rotation=rotation,
        fps=_fps_from(video),
        video_index=int(video.get("index", 0)),
        audio_index=int(audio["index"]) if audio else None,
        audio_codec=audio.get("codec_name") if audio else None,
        data_index=int(data["index"]),
        data_packets=packets(data),
    )


def extract_packet_index(
    info: VideoInfo,
    out_csv: str | Path,
    progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelFn] = None,
    log: Optional[LogFn] = None,
) -> int:
    """Write pts,dts,size,pos rows for every telemetry packet."""
    if progress:
        progress("Indexing telemetry packets", 0.02, "reading packet table")

    cmd = [
        FFPROBE, "-v", "error",
        "-select_streams", str(info.data_index),
        "-show_entries", "packet=pts_time,dts_time,size,pos",
        "-of", "csv=p=0", str(info.path),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, creationflags=_CREATE_FLAGS)
    lines: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if not line.strip():
                continue
            lines.append(line if line.endswith("\n") else line + "\n")
            if len(lines) % 500 == 0:
                _check_cancel(cancelled)
                if progress and info.data_packets:
                    progress(
                        "Indexing telemetry packets",
                        0.02 + 0.04 * min(1.0, len(lines) / info.data_packets),
                        f"{len(lines):,} packets",
                    )
        stderr = (proc.stderr.read() if proc.stderr else "").strip()
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    if code != 0:
        raise PipelineError("Could not index the telemetry packets.\n" + stderr)

    out_csv = Path(out_csv)
    out_csv.write_text("".join(lines), encoding="utf-8")
    if not lines:
        raise PipelineError("The telemetry track is empty.")
    if log:
        log(f"Indexed {len(lines):,} telemetry packets")
    return len(lines)


def parse_telemetry(
    info: VideoInfo,
    packets_csv: str | Path,
    out_csv: str | Path,
    progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelFn] = None,
    log: Optional[LogFn] = None,
) -> dict:
    """Parse each packet as a standalone GenericMessage. Returns counters."""
    from pyosmogps.dji_pb2 import GenericMessage

    packets: list[tuple[float, int, int]] = []
    with open(packets_csv, newline="", encoding="utf-8") as fh:
        for line in fh:
            parts = line.strip().split(",")
            if len(parts) < 4:
                continue
            t = _as_float(parts[0])
            if t is None:
                t = _as_float(parts[1])
            try:
                size = int(float(parts[2]))
                pos = int(float(parts[3]))
            except (TypeError, ValueError):
                continue
            if t is None or size <= 0 or pos < 0:
                continue
            packets.append((t, size, pos))

    if not packets:
        raise PipelineError(
            "The telemetry track has no readable packets. "
            "This may not be a DJI Osmo/Action file."
        )

    packets.sort(key=lambda row: row[2])       # read the mdat sequentially
    t0 = min(t for t, _, _ in packets)         # align telemetry to the video timeline
    total = len(packets)
    bad = 0
    gps_rows = 0

    with open(info.path, "rb") as src, open(out_csv, "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(
            ["t", "dt", "lat", "lon", "alt_m", "der_x", "der_y", "der_z",
             "dev_name", "coord_units"]
        )
        current = 0
        for index, (t, size, pos) in enumerate(packets):
            _check_cancel(cancelled)
            if pos != current:
                src.seek(pos)
            buf = src.read(size)
            current = pos + size

            message = GenericMessage()
            try:
                message.ParseFromString(buf)
            except Exception:
                bad += 1
                continue

            for gps in message.gps_info:
                remote = gps.remote_gps_info
                coords = remote.coordinates
                deriv = remote.derivatives
                try:
                    stamp = coords.datetime.datetime
                except Exception:
                    stamp = ""
                writer.writerow([
                    t - t0,
                    stamp,
                    coords.info.latitude,
                    coords.info.longitude,
                    coords.gps_altitude_mm / 1000.0,
                    deriv.x,
                    deriv.y,
                    deriv.z,
                    remote.device_info.device_name,
                    coords.info.coordinate_units,
                ])
                gps_rows += 1

            if progress and ((index + 1) % 256 == 0 or index + 1 == total):
                progress(
                    "Parsing telemetry",
                    0.06 + 0.10 * (index + 1) / total,
                    f"{index + 1:,} / {total:,} packets",
                )

    if gps_rows == 0:
        raise PipelineError(
            "The telemetry packets parsed, but contained no GPS samples. "
            "The camera may not have had a GPS fix."
        )
    if log:
        log(f"Parsed {total:,} packets -> {gps_rows:,} GPS rows ({bad:,} bad)")
    return {"packets": total, "rows": gps_rows, "bad": bad}


def compute_speed(
    telemetry_csv: str | Path,
    out_csv: str | Path,
    duration: float,
    progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelFn] = None,
    log: Optional[LogFn] = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, SpeedStats]:
    """Build the 1 Hz speed series and running min/max."""
    if progress:
        progress("Computing speed", 0.16, "1 Hz series")

    with open(telemetry_csv, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise PipelineError("No telemetry rows to convert into speed.")

    t = np.array([float(r["t"]) for r in rows])
    lat = np.array([float(r["lat"]) for r in rows])
    lon = np.array([float(r["lon"]) for r in rows])
    dx = np.array([float(r["der_x"]) for r in rows])
    dy = np.array([float(r["der_y"]) for r in rows])
    valid = (np.abs(lat) > 0.001) & (np.abs(lon) > 0.001)

    by_second: dict[int, list[int]] = {}
    for i in range(len(t)):
        by_second.setdefault(round(float(t[i])), []).append(i)

    # Cover both the video and the last telemetry sample (they can differ by ~1 s).
    last_second = int(round(float(np.max(t))))
    nsec = max(1, max(int(round(duration)), last_second) + 1)
    speed = np.full(nsec, np.nan)
    for second in range(nsec):
        indexes = by_second.get(second)
        if not indexes:
            continue
        i = indexes[len(indexes) // 2]
        if valid[i]:
            speed[second] = float(np.hypot(dx[i], dy[i]))
        _check_cancel(cancelled)

    # Fill isolated single-second dropouts; real gaps stay blank.
    filled = speed.copy()
    for second in range(1, nsec - 1):
        if (np.isnan(filled[second])
                and not np.isnan(speed[second - 1])
                and not np.isnan(speed[second + 1])):
            filled[second] = (speed[second - 1] + speed[second + 1]) / 2.0

    running_min = np.full(nsec, np.nan)
    running_max = np.full(nsec, np.nan)
    lo = hi = None
    for second in range(nsec):
        value = filled[second]
        if not np.isnan(value):
            lo = value if lo is None else min(lo, value)
            hi = value if hi is None else max(hi, value)
        running_min[second] = lo if lo is not None else np.nan
        running_max[second] = hi if hi is not None else np.nan

    with open(out_csv, "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(["sec", "mps", "mph", "kmh"])
        for second, value in enumerate(filled):
            if np.isnan(value):
                writer.writerow([second, "", "", ""])
            else:
                writer.writerow([
                    second,
                    f"{value:.4f}",
                    f"{value * MPH_PER_MPS:.4f}",
                    f"{value * KMH_PER_MPS:.4f}",
                ])

    known = ~np.isnan(filled)
    if not known.any():
        raise PipelineError(
            "This clip contains telemetry, but no GPS fix: every latitude/longitude "
            "sample is empty.\nCheck that the camera had satellite lock while recording."
        )
    moving = filled[known] >= 1.0
    avg_moving = float(filled[known][moving].mean()) if moving.any() else 0.0
    # 1 Hz bins: summing m/s over seconds gives metres.
    distance = float(np.nansum(np.where(known, filled, 0.0)))

    stats = SpeedStats(
        max_mph=float(np.nanmax(filled)) * MPH_PER_MPS,
        min_mph=float(np.nanmin(filled)) * MPH_PER_MPS,
        avg_moving_mph=avg_moving * MPH_PER_MPS,
        valid_seconds=int(known.sum()),
        total_seconds=nsec,
        distance_km=distance / 1000.0,
    )
    if log:
        log(
            f"Speed: max {stats.max_mph:.1f} mph, min {stats.min_mph:.1f} mph, "
            f"avg moving {stats.avg_moving_mph:.1f} mph, {stats.distance_km:.1f} km, "
            f"GPS in {stats.valid_seconds}/{stats.total_seconds} s"
        )
    return filled, running_min, running_max, stats


def render_frames(
    filled: np.ndarray,
    running_min: np.ndarray,
    running_max: np.ndarray,
    frames_dir: str | Path,
    unit: str = "mph",
    hud_style: str = "classic",
    hud_scale: float = 1.0,
    hud_opacity: float = 1.0,
    hud_position: str = "bottom-right",
    hud_center: Optional[tuple[float, float]] = None,
    progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelFn] = None,
) -> int:
    frames_dir = Path(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)

    # Remove a previous (possibly longer) run's frames.
    for old in frames_dir.glob("f_*.png"):
        old.unlink()

    total = len(filled)
    for i, value in enumerate(filled):
        _check_cancel(cancelled)
        image = hud.render(
            float(value) if not np.isnan(value) else 0.0,
            float(running_min[i]) if not np.isnan(running_min[i]) else 0.0,
            float(running_max[i]) if not np.isnan(running_max[i]) else 0.0,
            i,
            nofix=bool(np.isnan(value)),
            unit=unit,
            style=hud_style,
            scale=hud_scale,
            opacity=hud_opacity,
            position=hud_position,
            center=hud_center,
        )
        image.save(frames_dir / f"f_{i:05d}.png")
        if progress and ((i + 1) % 8 == 0 or i + 1 == total):
            progress(
                "Rendering HUD frames",
                0.18 + 0.08 * (i + 1) / total,
                f"{i + 1:,} / {total:,} frames",
            )
    return total


def burn(
    info: VideoInfo,
    frames_dir: str | Path,
    output: str | Path,
    progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelFn] = None,
    log: Optional[LogFn] = None,
    preset: str = "veryfast",
    crf: int = 20,
) -> Path:
    """Overlay the 1 fps PNG frames on the source and encode the result."""
    frames_dir = Path(frames_dir)
    output = Path(output)
    part = output.with_name(output.name + ".part.mp4")
    part.unlink(missing_ok=True)

    filter_graph = (
        f"[1:v]scale={info.width}:-2,fps={OVERLAY_FPS}[o];"
        f"[0:v][o]overlay=W-w:H-h:shortest=1,format=yuv420p[v]"
    )
    cmd = [
        FFMPEG, "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
        "-i", str(info.path),
        "-framerate", "1", "-i", str(frames_dir / "f_%05d.png"),
        "-filter_complex", filter_graph,
        "-map", "[v]",
    ]
    if info.audio_index is not None:
        cmd += ["-map", "0:a:0"]
        if info.audio_codec in _COPY_AUDIO:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-profile:v", "high", "-movflags", "+faststart",
        "-metadata", "comment=speed HUD: current/min/max from DJI GPS telemetry",
        str(part),
    ]

    log_path = output.with_name(output.name + ".ffmpeg.log")
    total = info.duration
    started = time.time()
    with open(log_path, "w", encoding="utf-8") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True,
                                creationflags=_CREATE_FLAGS)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if cancelled and cancelled():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    part.unlink(missing_ok=True)
                    raise Cancelled("Cancelled")
                line = line.strip()
                if line.startswith("out_time_us=") and total > 0:
                    try:
                        seconds = int(line.split("=", 1)[1]) / 1_000_000.0
                    except ValueError:
                        continue
                    seconds = max(0.0, min(seconds, total))
                    if progress:
                        progress(
                            "Burning video",
                            0.26 + 0.74 * seconds / total,
                            f"{fmt_hms(seconds)} / {fmt_hms(total)}",
                        )
                elif line == "progress=end" and progress:
                    progress("Burning video", 1.0, f"{fmt_hms(total)} / {fmt_hms(total)}")
            code = proc.wait()
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    if code != 0:
        tail = ""
        try:
            tail = "\n".join(log_path.read_text(encoding="utf-8").strip().splitlines()[-6:])
        except OSError:
            pass
        part.unlink(missing_ok=True)
        raise PipelineError("ffmpeg failed while burning the video.\n" + tail)

    part.replace(output)
    log_path.unlink(missing_ok=True)
    if log:
        size_mb = output.stat().st_size / (1024 * 1024)
        log(f"Encoded {output.name} ({size_mb:,.0f} MB) in {fmt_hms(time.time() - started)}")
    return output


def run_pipeline(
    input_path: str | Path,
    output_path: str | Path,
    work_dir: str | Path | None = None,
    unit: str = "mph",
    hud_style: str = "classic",
    hud_scale: float = 1.0,
    hud_opacity: float = 1.0,
    hud_position: str = "bottom-right",
    hud_center: Optional[tuple[float, float]] = None,
    encode_preset: str = "veryfast",
    encode_crf: int = 20,
    keep_intermediates: bool = True,
    progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelFn] = None,
    log: Optional[LogFn] = None,
) -> PipelineResult:
    started = time.time()
    check_tools()
    input_path = Path(input_path)
    output_path = Path(output_path)
    if input_path.resolve() == output_path.resolve():
        raise PipelineError("The output file must be different from the input file.")
    if work_dir is None:
        work_dir = output_path.with_name(output_path.stem + "_work")
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(stage: str, fraction: float, detail: str = "") -> None:
        if progress:
            progress(stage, fraction, detail)

    emit("Reading video metadata", 0.0, input_path.name)
    info = probe_video(input_path)
    if log:
        telemetry = (f"{info.data_packets:,} packets" if info.data_packets
                     else "track found")
        log(
            f"Input: {info.path.name}  {info.width}x{info.height}  "
            f"{info.fps:.3f} fps  {info.duration_hms}  telemetry: {telemetry}"
        )

    packets_csv = work_dir / "djmd_pkts.csv"
    telemetry_csv = work_dir / "telemetry.csv"
    speed_csv = work_dir / "speed_1hz.csv"
    frames_dir = work_dir / "frames"

    _check_cancel(cancelled)
    extract_packet_index(info, packets_csv, progress, cancelled, log)
    _check_cancel(cancelled)
    parse_telemetry(info, packets_csv, telemetry_csv, progress, cancelled, log)
    _check_cancel(cancelled)
    filled, running_min, running_max, stats = compute_speed(
        telemetry_csv, speed_csv, info.duration, progress, cancelled, log
    )
    _check_cancel(cancelled)
    frames = render_frames(
        filled, running_min, running_max, frames_dir,
        unit=unit, hud_style=hud_style, hud_scale=hud_scale,
        hud_opacity=hud_opacity, hud_position=hud_position,
        hud_center=hud_center, progress=progress, cancelled=cancelled,
    )
    _check_cancel(cancelled)
    burn(info, frames_dir, output_path, progress, cancelled, log,
         preset=encode_preset, crf=encode_crf)

    if not keep_intermediates:
        shutil.rmtree(work_dir, ignore_errors=True)

    return PipelineResult(
        output=output_path,
        info=info,
        stats=stats,
        elapsed=time.time() - started,
        work_dir=work_dir,
        frames=frames,
    )
