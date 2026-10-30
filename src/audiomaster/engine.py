from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class MasteringError(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    highpass_hz: float
    lra_lu: float
    max_ratio: float
    attack_ms: float
    release_ms: float


GENRES = {
    "pop": Profile(25, 8, 2.5, 20, 180),
    "rock": Profile(28, 9, 2.5, 25, 180),
    "hip-hop": Profile(20, 8, 2.5, 30, 220),
    "electronic": Profile(20, 7, 3.0, 15, 150),
    "jazz": Profile(18, 12, 1.6, 40, 250),
    "classical": Profile(0, 18, 1.3, 50, 350),
    "acoustic": Profile(22, 12, 1.6, 35, 250),
    "ambient": Profile(0, 14, 1.5, 50, 350),
}


@dataclass(frozen=True)
class ExportFormat:
    suffix: str
    muxer: str
    encoder_args: tuple[str, ...]
    output_filter: str
    lossy: bool = False


_PCM24 = (
    "aresample=48000:osf=s32:"
    "dither_method=triangular:output_sample_bits=24"
)

_PLANAR_PCM24 = (
    "aresample=48000:osf=s32p:"
    "dither_method=triangular:output_sample_bits=24"
)


FORMATS = {
    "wav": ExportFormat(
        ".wav",
        "wav",
        ("-c:a", "pcm_s24le", "-rf64", "auto"),
        _PCM24,
    ),
    "m4a-aac": ExportFormat(
        ".m4a",
        "ipod",
        (
            "-c:a", "aac",
            "-profile:a", "aac_low",
            "-b:a", "256k",
            "-movflags", "+faststart",
        ),
        "aresample=48000:osf=fltp",
        True,
    ),
    "m4a-alac": ExportFormat(
        ".m4a",
        "ipod",
        (
            "-c:a", "alac",
            "-sample_fmt", "s32p",
            "-bits_per_raw_sample", "24",
            "-movflags", "+faststart",
        ),
        _PLANAR_PCM24,
    ),
    "flac": ExportFormat(
        ".flac",
        "flac",
        (
            "-c:a", "flac",
            "-sample_fmt", "s32",
            "-bits_per_raw_sample", "24",
            "-compression_level", "8",
        ),
        _PCM24,
    ),
    "opus": ExportFormat(
        ".opus",
        "opus",
        (
            "-c:a", "libopus",
            "-b:a", "192k",
            "-vbr", "on",
            "-application", "audio",
            "-compression_level", "10",
        ),
        "aresample=48000:osf=flt",
        True,
    ),
}


# All measurements and mastering decisions use this standardized signal.
_BASE = (
    "aresample=48000,"
    "aformat=sample_fmts=fltp:channel_layouts=stereo"
)

_LOUDNESS_TOLERANCE = 0.5
_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Stats:
    integrated_lufs: float
    true_peak_dbtp: float
    loudness_range_lu: float
    threshold_lufs: float


@dataclass(frozen=True)
class Plan:
    highpass_hz: float
    target_lra_lu: float
    predicted_peak_excess_db: float
    pregain_db: float
    compressor_ratio: float
    compressor_threshold_dbfs: float | None
    filters: str


@dataclass(frozen=True)
class Report:
    input_path: str
    output_path: str
    output_format: str
    genre: str
    target_lufs: float
    true_peak_ceiling_dbtp: float
    before: Stats
    after: Stats
    plan: Plan
    normalization_mode: str
    render_attempts: int
    warnings: tuple[str, ...]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _source_path(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve(strict=True)

    if not path.is_file():
        raise ValueError(f"Not a regular input file: {path}")

    return path


def _loudnorm_json(stderr: str) -> dict:
    blocks = re.findall(
        r'\{\s*"input_i"\s*:.*?\}',
        stderr,
        flags=re.DOTALL,
    )

    if not blocks:
        raise MasteringError(
            "FFmpeg did not return loudnorm measurements.\n"
            + stderr[-4000:]
        )

    try:
        return json.loads(blocks[-1])
    except json.JSONDecodeError as exc:
        raise MasteringError("Invalid loudnorm JSON from FFmpeg.") from exc


def _number(data: dict, key: str) -> float:
    try:
        value = float(data[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise MasteringError(
            f"Missing or invalid loudness measurement: {key}"
        ) from exc

    if not math.isfinite(value):
        raise MasteringError(
            f"Unmeasurable loudness: {key}={data.get(key)!r}. "
            "The audio may be silent, too short/quiet, or the requested "
            "normalization may be impractical."
        )

    return value


def _stats(data: dict) -> Stats:
    # These are the INPUT measurements of the analysis filter.
    # When analyzing an exported file, they describe that exported file,
    # not the analysis filter's hypothetical normalized output.
    return Stats(
        integrated_lufs=_number(data, "input_i"),
        true_peak_dbtp=_number(data, "input_tp"),
        loudness_range_lu=_number(data, "input_lra"),
        threshold_lufs=_number(data, "input_thresh"),
    )


def _normalizer(
    target: float,
    lra: float,
    peak: float,
    measured: dict | None = None,
) -> str:
    options = [
        f"I={target:.6f}",
        f"LRA={lra:.6f}",
        f"TP={peak:.6f}",
        "print_format=json",
    ]

    if measured is None:
        options.append("linear=false")
    else:
        measurements = (
            ("measured_I", "input_i"),
            ("measured_LRA", "input_lra"),
            ("measured_TP", "input_tp"),
            ("measured_thresh", "input_thresh"),
            ("offset", "target_offset"),
        )

        for option, key in measurements:
            options.append(f"{option}={_number(measured, key):.6f}")

        # FFmpeg uses linear normalization when feasible and falls back
        # to dynamic normalization when peak/LRA constraints require it.
        options.append("linear=true")

    return "loudnorm=" + ":".join(options)


def _make_plan(
    stats: Stats,
    profile: Profile,
    target_lufs: float,
    working_peak: float,
) -> Plan:
    pressure = max(
        0.0,
        stats.true_peak_dbtp
        + target_lufs
        - stats.integrated_lufs
        - working_peak,
    )

    lra_excess = max(
        0.0,
        stats.loudness_range_lu - profile.lra_lu,
    )

    wanted_reduction = min(6.0, pressure + 0.25 * lra_excess)

    # Put the signal into a predictable operating range before compression.
    # This is floating-point processing; final loudness is set afterward.
    pregain = -6.0 - stats.true_peak_dbtp

    filters = [
        _BASE,
        f"volume={pregain:.6f}dB",
    ]

    if profile.highpass_hz > 0:
        filters.append(
            f"highpass=f={profile.highpass_hz:.3f}:p=2"
        )

    ratio = 1.0
    threshold = None

    if wanted_reduction >= 0.75:
        ratio = min(
            profile.max_ratio,
            1.0 + wanted_reduction / 3.0,
        )

        # For a static downward compressor:
        #
        # reduction = level_above_threshold * (1 - 1 / ratio)
        #
        # Limit how far below the aligned peak we place the threshold.
        above_threshold = min(
            12.0,
            wanted_reduction / (1.0 - 1.0 / ratio),
        )

        threshold = -6.0 - above_threshold
        threshold_linear = 10.0 ** (threshold / 20.0)

        filters.append(
            f"acompressor=threshold={threshold_linear:.9f}"
            f":ratio={ratio:.6f}"
            f":attack={profile.attack_ms:.3f}"
            f":release={profile.release_ms:.3f}"
            ":makeup=1"
            ":knee=2"
            ":link=maximum"
            ":detection=peak"
        )

    return Plan(
        highpass_hz=profile.highpass_hz,
        target_lra_lu=profile.lra_lu,
        predicted_peak_excess_db=pressure,
        pregain_db=pregain,
        compressor_ratio=ratio,
        compressor_threshold_dbfs=threshold,
        filters=",".join(filters),
    )


class Masterer:
    def __init__(
        self,
        ffmpeg: str | Path | None = None,
        timeout: float | None = None,
    ):
        name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

        if ffmpeg is None:
            self.ffmpeg = Path(__file__).parent / "bin" / name
        else:
            self.ffmpeg = Path(ffmpeg).expanduser().resolve()

        self.timeout = timeout

        if not self.ffmpeg.is_file():
            raise MasteringError(
                f"Bundled FFmpeg not found: {self.ffmpeg}. "
                "Run the FFmpeg build script first."
            )

        if not os.access(self.ffmpeg, os.X_OK):
            raise MasteringError(
                f"FFmpeg is not executable: {self.ffmpeg}"
            )

    def _run(self, args: list[str]) -> str:
        command = [
            str(self.ffmpeg),
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-loglevel", "info",
            "-xerror",
            "-y",
            *args,
        ]

        env = dict(os.environ)
        env["LC_ALL"] = "C"

        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise MasteringError("FFmpeg processing timed out.") from exc
        except OSError as exc:
            raise MasteringError(
                f"Unable to execute bundled FFmpeg: {exc}"
            ) from exc

        if result.returncode != 0:
            raise MasteringError(
                "FFmpeg failed:\n" + result.stderr[-6000:]
            )

        return result.stderr

    @staticmethod
    def _input_args(source: Path) -> list[str]:
        return [
            "-protocol_whitelist", "file,pipe",
            "-i", str(source),
            "-map", "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata", "-1",
            "-map_chapters", "-1",
        ]

    def _measure(
        self,
        source: Path,
        graph: str,
        target: float = -23.0,
        lra: float = 7.0,
        peak: float = -1.0,
    ) -> tuple[Stats, dict]:
        stderr = self._run([
            *self._input_args(source),
            "-af", graph + "," + _normalizer(target, lra, peak),
            # Reuse the WAV encoder for the null analysis output.
            "-c:a", "pcm_s24le",
            "-f", "null",
            "pipe:1",
        ])

        data = _loudnorm_json(stderr)
        return _stats(data), data

    def analyze(self, source: str | Path) -> Stats:
        """Measure the standardized 48 kHz stereo signal."""
        path = _source_path(source)
        return self._measure(path, _BASE)[0]

    def master(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        genre: str,
        target_lufs: float,
        output_format: str,
        true_peak_ceiling: float = -1.0,
        overwrite: bool = False,
    ) -> Report:
        genre = genre.strip().lower()
        output_format = output_format.strip().lower()
        target_lufs = float(target_lufs)
        true_peak_ceiling = float(true_peak_ceiling)

        if genre not in GENRES:
            raise ValueError(
                f"Unknown genre. Choose from: {', '.join(GENRES)}"
            )

        if output_format not in FORMATS:
            raise ValueError(
                f"Unknown output format. Choose from: {', '.join(FORMATS)}"
            )

        if not (
            math.isfinite(target_lufs)
            and -70.0 <= target_lufs <= -5.0
        ):
            raise ValueError(
                "LUFS must be finite and between -70 and -5. "
                "Extreme targets may not be achievable."
            )

        if not (
            math.isfinite(true_peak_ceiling)
            and -8.0 <= true_peak_ceiling <= 0.0
        ):
            raise ValueError(
                "True-peak ceiling must be between -8 and 0 dBTP."
            )

        source = _source_path(source)
        destination = Path(destination).expanduser().resolve()

        spec = FORMATS[output_format]
        profile = GENRES[genre]

        if destination.suffix.lower() != spec.suffix:
            raise ValueError(
                f"{output_format} output must use {spec.suffix}"
            )

        if source == destination or (
            destination.exists() and source.samefile(destination)
        ):
            raise ValueError("Refusing to overwrite the source file.")

        if destination.exists():
            if not destination.is_file():
                raise ValueError("Destination is not a regular file.")
            if not overwrite:
                raise FileExistsError(destination)

        destination.parent.mkdir(parents=True, exist_ok=True)

        before, _ = self._measure(source, _BASE)

        # Extra margin for post-filter resampling and codec overshoot.
        working_peak = true_peak_ceiling - (
            1.0 if spec.lossy else 0.2
        )
        working_peak = max(-9.0, working_peak)
        working_lufs = target_lufs

        plan = _make_plan(
            before,
            profile,
            target_lufs,
            working_peak,
        )

        warnings = []

        if before.true_peak_dbtp >= 0.0:
            warnings.append(
                "Input reaches or exceeds 0 dBTP. Inspect for existing "
                "clipping; this processor does not repair distortion."
            )

        if plan.predicted_peak_excess_db > 6.0:
            warnings.append(
                "The requested loudness requires substantial peak reduction. "
                "Audition for pumping and transient loss."
            )

        fd, temporary_name = tempfile.mkstemp(
            prefix=".audiomaster-",
            suffix=spec.suffix,
            dir=destination.parent,
        )
        os.close(fd)
        temporary = Path(temporary_name)

        try:
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                # This measurement includes the EXACT preprocessing that
                # will precede loudnorm during rendering.
                _, calibration = self._measure(
                    source,
                    plan.filters,
                    working_lufs,
                    profile.lra_lu,
                    working_peak,
                )

                graph = ",".join([
                    plan.filters,
                    _normalizer(
                        working_lufs,
                        profile.lra_lu,
                        working_peak,
                        calibration,
                    ),
                    spec.output_filter,
                ])

                stderr = self._run([
                    *self._input_args(source),
                    "-af", graph,
                    "-ar", "48000",
                    "-ac", "2",
                    *spec.encoder_args,
                    "-f", spec.muxer,
                    str(temporary),
                ])

                normalization_mode = str(
                    _loudnorm_json(stderr).get(
                        "normalization_type",
                        "unknown",
                    )
                )

                # Measure the actual encoded file, including codec effects.
                after, _ = self._measure(temporary, _BASE)

                loudness_error = (
                    target_lufs - after.integrated_lufs
                )
                peak_excess = (
                    after.true_peak_dbtp - true_peak_ceiling
                )

                if (
                    abs(loudness_error) <= _LOUDNESS_TOLERANCE
                    and peak_excess <= 0.0
                ):
                    break

                if abs(loudness_error) > _LOUDNESS_TOLERANCE:
                    working_lufs = _clamp(
                        working_lufs + loudness_error,
                        -70.0,
                        -5.0,
                    )

                if peak_excess > 0.0:
                    working_peak = max(
                        -9.0,
                        working_peak - peak_excess - 0.2,
                    )

                # Every retry reads the ORIGINAL source, not an already
                # encoded AAC/Opus file. No generational lossy transcoding.

            if (
                abs(after.integrated_lufs - target_lufs)
                > _LOUDNESS_TOLERANCE
                or after.true_peak_dbtp > true_peak_ceiling
            ):
                raise MasteringError(
                    "Could not meet the requested constraints after "
                    f"{_MAX_ATTEMPTS} renders. "
                    f"Measured {after.integrated_lufs:.2f} LUFS and "
                    f"{after.true_peak_dbtp:.2f} dBTP; requested "
                    f"{target_lufs:.2f} LUFS and <= "
                    f"{true_peak_ceiling:.2f} dBTP. "
                    "No new output was published. Change the target "
                    "or inspect the material."
                )

            if overwrite:
                os.replace(temporary, destination)
            else:
                # Temporary file and destination share a filesystem.
                # Hard-link publication is atomic and will not overwrite
                # a destination created by another process meanwhile.
                os.link(temporary, destination)
                temporary.unlink()

            return Report(
                input_path=str(source),
                output_path=str(destination),
                output_format=output_format,
                genre=genre,
                target_lufs=target_lufs,
                true_peak_ceiling_dbtp=true_peak_ceiling,
                before=before,
                after=after,
                plan=plan,
                normalization_mode=normalization_mode,
                render_attempts=attempt,
                warnings=tuple(warnings),
            )

        finally:
            temporary.unlink(missing_ok=True)