"""The dataset's structure standardisation, copied verbatim (function body) from
environment/dataset/common.py `standardize`, which built every SMILES and InChIKey14 in the
dataset. Same RDKit version (2026.9.1) as that build. Keep the two in sync."""
from __future__ import annotations

from rdkit import Chem, RDLogger
from rdkit.Chem import Descriptors
from rdkit.Chem.MolStandardize import rdMolStandardize

RDLogger.DisableLog("rdApp.*")

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


def ik14(smi: str) -> str | None:
    r = standardize(smi)
    return r[1] if r else None
