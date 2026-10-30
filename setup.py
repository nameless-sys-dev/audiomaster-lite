import os
from pathlib import Path

from setuptools import Distribution, setup
from setuptools.command.build_py import build_py
from wheel.bdist_wheel import bdist_wheel


class BinaryDistribution(Distribution):
    def has_ext_modules(self):
        return True


class PlatformWheel(bdist_wheel):
    def get_tag(self):
        _, _, platform_tag = super().get_tag()
        # The executable is platform-specific, but has no Python ABI dependency.
        return "py3", "none", platform_tag


class CheckedBuildPy(build_py):
    def run(self):
        name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        binary = Path("src/audiomaster/bin") / name

        if not binary.is_file():
            raise RuntimeError(
                "Bundled FFmpeg is missing. "
                "Run scripts/build_ffmpeg_linux.sh before building/installing."
            )

        super().run()


setup(
    distclass=BinaryDistribution,
    cmdclass={
        "bdist_wheel": PlatformWheel,
        "build_py": CheckedBuildPy,
    },
)