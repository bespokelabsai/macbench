"""Shared helpers for the dataset build (paths, standardisation, fingerprints)."""
from __future__ import annotations

import os
from pathlib import Path

from rdkit import Chem, RDLogger
from rdkit.Chem import Descriptors, rdFingerprintGenerator
from rdkit.Chem.MolStandardize import rdMolStandardize

RDLogger.DisableLog("rdApp.*")

# Everything the build reads or writes lives under one directory: build/ next to the task folder (outside it, so
# Harbor never sees it), or $MS2_BUILD_DIR. drun.sh mounts it into the runner container and sets MS2_BUILD_DIR there.
BUILD = Path(os.environ.get("MS2_BUILD_DIR") or Path(__file__).resolve().parents[3] / "build")
RAW = str(BUILD / "massspecgym" / "data")  # download.sh: MassSpecGym files at their HF repo paths
OUT = str(BUILD)                            # the dataset: agent/, grader/, dev/ (prepare.sh reads it)
WORK = str(BUILD / "work")                  # intermediates, not shipped
SEED = 20261103

_lfc = None
_unc = None


def _tools():
    global _lfc, _unc
    if _lfc is None:
        _lfc = rdMolStandardize.LargestFragmentChooser(preferOrganic=True)
        _unc = rdMolStandardize.Uncharger(canonicalOrder=True)
    return _lfc, _unc


def standardize(smi: str):
    """Largest fragment -> neutralise -> drop isotopes -> remove stereo -> canonical SMILES.
    Returns (smiles, ik14, exact_mass, n_heavy, n_chiral) or None if RDKit cannot handle it."""
    lfc, unc = _tools()
    try:
        m = Chem.MolFromSmiles(smi)
        if m is None:
            return None
        m = lfc.choose(m)
        m = unc.uncharge(m)
        for a in m.GetAtoms():
            a.SetIsotope(0)
        Chem.RemoveStereochemistry(m)
        s = Chem.MolToSmiles(m)
        m2 = Chem.MolFromSmiles(s)  # round-trip so the stored string is a fixed point
        if m2 is None:
            return None
        s2 = Chem.MolToSmiles(m2)
        ik = Chem.MolToInchiKey(m2)
        if not ik:
            return None
        nch = len(Chem.FindMolChiralCenters(m2, includeUnassigned=True, useLegacyImplementation=False))
        return s2, ik[:14], Descriptors.ExactMolWt(m2), m2.GetNumHeavyAtoms(), nch
    except Exception:
        return None


_mfp = None


def morgan_bits(smi: str, nbits: int = 2048):
    global _mfp
    if _mfp is None or _mfp[0] != nbits:
        _mfp = (nbits, rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=nbits))
    m = Chem.MolFromSmiles(smi)
    if m is None:
        return None
    return _mfp[1].GetFingerprint(m)
