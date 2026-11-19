"""AC power flow (Newton-Raphson, polar form) for one island.

Standard formulation: PV buses hold voltage while their units have reactive room, PQ buses are loads,
one reference bus takes up the mismatch. Units that hit a reactive limit become PQ at that limit.
Returns None when the solution does not converge, which the simulator treats as voltage collapse.

The Jacobian is assembled straight from the admittance matrix's nonzeros (COO), which keeps a solve on the
whole 538-bus grid to a few milliseconds.
"""
import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import spsolve


def ybus(n, f, t, r, x, b, shunt_b):
    """Bus admittance matrix in COO parts (rows, cols, vals), duplicates summed, diagonal always present."""
    y = 1.0 / (r + 1j * x)
    bc = 1j * b / 2.0
    rows = np.concatenate([f, t, f, t, np.arange(n)])
    cols = np.concatenate([f, t, t, f, np.arange(n)])
    diag = (1j * shunt_b) if shunt_b is not None else np.zeros(n)
    vals = np.concatenate([y + bc, y + bc, -y, -y, diag])
    Y = sp.coo_matrix((vals, (rows, cols)), shape=(n, n)).tocsr()
    Y.sum_duplicates()
    coo = Y.tocoo()
    return Y, coo.row, coo.col, coo.data


def newton(Y, rows, cols, data, S, V0, ref, pv, pq, tol=1e-7, maxit=25, _direction=False):
    """Plain Newton-Raphson; None if it does not converge. With _direction, returns the first Newton step instead."""
    n = len(V0)
    V = V0.astype(complex).copy()
    Va, Vm = np.angle(V), np.abs(V)
    pvpq = np.concatenate([pv, pq])
    npvpq, npq = len(pvpq), len(pq)
    pos_a = np.full(n, -1); pos_a[pvpq] = np.arange(npvpq)
    pos_m = np.full(n, -1); pos_m[pq] = np.arange(npq)
    is_diag = rows == cols
    ra, ca = pos_a[rows], pos_a[cols]
    rm, cm = pos_m[rows], pos_m[cols]
    m11 = (ra >= 0) & (ca >= 0)
    m12 = (ra >= 0) & (cm >= 0)
    m21 = (rm >= 0) & (ca >= 0)
    m22 = (rm >= 0) & (cm >= 0)
    Jr = np.concatenate([ra[m11], ra[m12], npvpq + rm[m21], npvpq + rm[m22]])
    Jc = np.concatenate([ca[m11], npvpq + cm[m12], ca[m21], npvpq + cm[m22]])
    size = npvpq + npq
    for _ in range(maxit):
        Ib = Y @ V
        mis = V * np.conj(Ib) - S
        F = np.concatenate([mis[pvpq].real, mis[pq].imag])
        if F.size == 0 or np.max(np.abs(F)) < tol:
            return V
        Vn = V / np.abs(V)
        dVm = V[rows] * np.conj(data * Vn[cols])
        dVa = -1j * V[rows] * np.conj(data * V[cols])
        dVm[is_diag] += np.conj(Ib[rows[is_diag]]) * Vn[rows[is_diag]]
        dVa[is_diag] += 1j * V[rows[is_diag]] * np.conj(Ib[rows[is_diag]])
        vals = np.concatenate([dVa[m11].real, dVm[m12].real, dVa[m21].imag, dVm[m22].imag])
        J = sp.csc_matrix((vals, (Jr, Jc)), shape=(size, size))
        try:
            dx = spsolve(J, -F)
        except Exception:
            return None
        if not np.all(np.isfinite(dx)):
            return None
        if _direction:
            return dx
        Va[pvpq] += dx[:npvpq]
        Vm[pq] += dx[npvpq:]
        if np.any(Vm < 0.3) or np.any(Vm > 2.0):
            return None
        V = Vm * np.exp(1j * Va)
    return None


def newton_damped(Y, rows, cols, data, S, V0, ref, pv, pq, tol=1e-7, maxit=60, v_floor=0.75):
    """Newton with a backtracking line search, for islands where the plain iteration overshoots. Used only after
    newton() gives up, so every case newton() solves keeps its result. A solution with a bus below v_floor is not
    accepted: motors stall and load cannot be held there, so the island is treated as collapsing."""
    pvpq = np.concatenate([pv, pq])
    npvpq = len(pvpq)

    def mismatch(V):
        mis = V * np.conj(Y @ V) - S
        return np.concatenate([mis[pvpq].real, mis[pq].imag])
    V = V0.astype(complex).copy()
    F = mismatch(V)
    for _ in range(maxit):
        nF = np.max(np.abs(F)) if F.size else 0.0
        if nF < tol:
            return V if np.abs(V).min() >= v_floor else None
        # one plain Newton step from V gives the direction
        step = newton(Y, rows, cols, data, S, V, ref, pv, pq, tol=0.0, maxit=1, _direction=True)
        if step is None:
            return None
        Va, Vm = np.angle(V), np.abs(V)
        alpha = 1.0
        while alpha >= 1.0 / 128:
            Va2, Vm2 = Va.copy(), Vm.copy()
            Va2[pvpq] += alpha * step[:npvpq]
            Vm2[pq] += alpha * step[npvpq:]
            if Vm2.min() > 0.3 and Vm2.max() < 2.0:
                V2 = Vm2 * np.exp(1j * Va2)
                F2 = mismatch(V2)
                if np.max(np.abs(F2)) < (1 - 1e-4 * alpha) * nF:
                    V, F = V2, F2
                    break
            alpha /= 2
        else:
            return None
    return None


def solve(n, br, shunt_b, p_inj, q_inj, gens, ref, V_init=None):
    """Solve one island.

    n: bus count; br: (f, t, r, x, b) arrays; shunt_b: per-bus shunt susceptance (pu, + capacitive);
    p_inj, q_inj: per-bus load-side injections in pu (negative for load), excluding units;
    gens: list of (bus, p_pu, vset, qmin_pu, qmax_pu) for synchronous units holding voltage;
    ref: reference bus. Returns dict(V, qbus, p_ref) or None.
    """
    f, t, r, x, b = br
    Y, rows, cols, data = ybus(n, f, t, r, x, b, shunt_b)
    pg = np.zeros(n)
    vset = np.ones(n)
    is_pv = np.zeros(n, bool)
    qlim = {}
    for bus, p, vs, qmn, qmx in gens:
        pg[bus] += p
        vset[bus] = vs
        is_pv[bus] = True
        lo, hi = qlim.get(bus, (0.0, 0.0))
        qlim[bus] = (lo + qmn, hi + qmx)
    fixed_q = np.zeros(n)
    idx = np.arange(n)
    V = None
    for _round in range(5):
        pv = np.flatnonzero(is_pv & (idx != ref))
        pq = np.flatnonzero(~is_pv & (idx != ref))
        S = (pg + p_inj) + 1j * (q_inj + fixed_q)
        held = is_pv | (idx == ref)
        if V is not None:
            V0 = np.where(held, vset, np.abs(V)) * np.exp(1j * np.angle(V))
        elif V_init is not None:
            V0 = np.where(held, vset, np.abs(V_init)) * np.exp(1j * np.angle(V_init))
        else:
            V0 = np.where(held, vset, 1.0).astype(complex)
        V = newton(Y, rows, cols, data, S, V0, ref, pv, pq)
        if V is None:
            V = newton_damped(Y, rows, cols, data, S, V0, ref, pv, pq)
        if V is None:
            return None
        Scalc = V * np.conj(Y @ V)
        qbus = Scalc.imag - q_inj
        changed = False
        for bus, (qmin, qmax) in qlim.items():
            if bus == ref or not is_pv[bus]:
                continue
            if qbus[bus] > qmax + 1e-6 or qbus[bus] < qmin - 1e-6:
                fixed_q[bus] = min(max(qbus[bus], qmin), qmax)
                is_pv[bus] = False
                changed = True
        if not changed:
            break
    return dict(V=V, qbus=qbus, p_ref=Scalc.real[ref] - p_inj[ref])


def branch_flows(V, br):
    f, t, r, x, b = br
    y = 1.0 / (r + 1j * x)
    If = (V[f] - V[t]) * y + V[f] * 1j * b / 2
    It = (V[t] - V[f]) * y + V[t] * 1j * b / 2
    Sf = V[f] * np.conj(If)
    St = V[t] * np.conj(It)
    return Sf, St
