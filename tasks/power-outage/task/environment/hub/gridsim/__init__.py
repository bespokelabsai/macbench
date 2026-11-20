"""The Black Start simulator.

One floating-point path on every x86-64 machine. numpy and OpenBLAS pick their kernels for the CPU they run on
(AVX-512 on one host, AVX2 on another), and kernels round differently. The grid amplifies a last-bit difference:
the same controllers on the same scenario drifted apart after 3 simulated hours and ended 0.53 on one grading
host and 0.14 on another. So the simulator and the controllers (runner.py passes the same two variables) leave
numpy's AVX-512 kernels off and run OpenBLAS's Haswell (AVX2) kernels, which every grading host has. They are set
here, before numpy is imported, so every entry point (the grader's runner, the practice office's drills, bound.py)
gets them; the processes it starts inherit them.
"""
import os

NUMERIC_ENV = {
    "NPY_DISABLE_CPU_FEATURES": "AVX512F AVX512CD AVX512_KNL AVX512_KNM AVX512_SKX AVX512_CLX AVX512_CNL "
                                "AVX512_ICL AVX512_SPR",
    "OPENBLAS_CORETYPE": "Haswell",
}
os.environ.update(NUMERIC_ENV)
