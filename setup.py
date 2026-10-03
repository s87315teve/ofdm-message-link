from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension(
            "ofdm_link.phy._turbo_native",
            ["src/ofdm_link/phy/turbo_native.cpp"],
            language="c++",
            extra_compile_args=["-O3", "-std=c++17"],
        )
    ]
)
