import math
import struct
import sys
import tempfile
import wave
from pathlib import Path

from audiomaster import FORMATS, Masterer


def make_test_wav(path: Path):
    sample_rate = 48000
    frames = bytearray()

    for n in range(sample_rate * 6):
        t = n / sample_rate
        value = (
            0.17 * math.sin(2 * math.pi * 220 * t)
            + 0.06 * math.sin(2 * math.pi * 880 * t)
        )
        sample = round(value * 32767)
        frames.extend(struct.pack("<hh", sample, sample))

    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(frames)


def main():
    masterer = Masterer()

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "input.wav"
        make_test_wav(source)

        for name, spec in FORMATS.items():
            encoded = root / f"{name}{spec.suffix}"

            report = masterer.master(
                source,
                encoded,
                genre="pop",
                target_lufs=-16,
                output_format=name,
            )

            assert encoded.stat().st_size > 0
            assert abs(report.after.integrated_lufs + 16) <= 0.5
            assert report.after.true_peak_dbtp <= -1.0

            # Verify that each exported codec is also decodable.
            masterer.master(
                encoded,
                root / f"{name}-decoded.wav",
                genre="jazz",
                target_lufs=-18,
                output_format="wav",
            )

            print(f"PASS: WAV -> {name} -> WAV")

        if len(sys.argv) > 1:
            masterer.master(
                sys.argv[1],
                root / "mp3-import.wav",
                genre="rock",
                target_lufs=-16,
                output_format="wav",
            )
            print("PASS: supplied MP3 -> WAV")


if __name__ == "__main__":
    main()