from pathlib import Path

from setuptools import Extension, setup
from setuptools.command.build_py import build_py

# The link-adaptation threshold table lives in configs/ (the one source the
# repository edits); a built package has no configs/, so ship a copy inside
# ofdm_message_link/la/, where la.thresholds looks when configs/ is absent.
THRESHOLDS = Path("configs") / "la" / "thresholds_pluto_v5.yaml"


class BuildPyWithThresholds(build_py):
    def run(self):
        super().run()
        target = Path(self.build_lib) / "ofdm_message_link" / "la" / THRESHOLDS.name
        self.mkpath(str(target.parent))
        self.copy_file(str(THRESHOLDS), str(target))


setup(
    cmdclass={"build_py": BuildPyWithThresholds},
    ext_modules=[
        Extension(
            "ofdm_link.phy._turbo_native",
            ["src/ofdm_link/phy/turbo_native.cpp"],
            language="c++",
            extra_compile_args=["-O3", "-std=c++17"],
        )
    ],
)
