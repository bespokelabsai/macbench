"""The two kinds of plug-in: providers and models.

**Provider**: one per role, the environment. A role is a name such as ``atmosphere.mars``,
``winds.earth``, ``terrain.mars`` or ``gravity.mars``. Exactly one provider is active per role; the
registry picks it. Swapping an atmosphere means adding a provider and selecting it, not editing the loop.

**Model**: any number, the vehicle's physics and its failure checks: slosh, actuators, sensors, power,
heating, parachutes, structures, vibration, separation. Every hook is optional. The core calls them in
``order`` (lower first), and a model never calls another model: it reads what it needs from the flight
state the core passes in.

Rules:
- Outputs by name only (``core.columns``). Counting columns is not allowed.
- A force or torque is given once, in the body frame, and ``dynamics/`` applies it in 3-DOF and 6-DOF.
- Random draws come from ``ctx.rng(name)``, a stream of its own per model, so adding a model never moves
  another model's draws (one shared stream would change every world's dispersions).
- Hidden values (as-built factors, allowables) never go into ``obs`` or a team-facing message unless
  ``sanitize`` lets them through; ``dev/test_leaks.py`` checks.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class MassItem:
    """A mass a model adds to an element, with its location in the element's frame (m)."""
    element: str
    name: str
    mass: float
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    inertia: tuple | None = None   # (Ixx, Iyy, Izz) about its own centre, or None for a point mass


@dataclass
class FlightContext:
    """What a model gets once per flight, before liftoff."""
    seed: int | None
    design: dict                   # the compiled design (vehicle.json form)
    vehicle: Any                   # the built Vehicle
    world: Any                     # the drawn Dispersions
    truth: dict                    # hidden as-built truth; never shown to the team
    data_dir: str                  # marsflight/data
    bodies: Any = None             # Tudat SystemOfBodies, once built
    six_dof: bool = False

    def rng(self, name):
        """A random stream of the model's own, fixed by the flight's seed and the model's name."""
        base = self.seed if self.seed is not None else 0
        return np.random.default_rng([int(base) & 0xFFFFFFFF, zlib.crc32(name.encode())])


@dataclass
class Arc:
    """One propagated arc, as the core sets it up."""
    t0: float
    dt: float
    central: str                   # "Earth", "Mars" or "Sun"
    body: str                      # the Tudat body flown (VT..., VE..., or a 6-DOF body)
    in_atmosphere: bool
    thrusting: bool
    six_dof: bool
    groups: list = field(default_factory=list)   # lit engine groups
    command: dict = field(default_factory=dict)  # the flight software's command for this arc


@dataclass
class Event:
    """A discrete event: liftoff, separation, chute_deploy, touchdown, burnout, engine_start, ..."""
    kind: str
    t: float
    element: str | None = None
    detail: dict = field(default_factory=dict)


class Model:
    """Base class. Override only the hooks you need."""
    name = "model"
    area = "misc"
    order = 50

    def configure(self, ctx: FlightContext):
        """Once per flight. Keep ``ctx``; draw your dispersions from ``ctx.rng(self.name)``."""

    def mass_items(self, vehicle) -> list[MassItem]:
        return []

    def columns(self, arc: Arc, cols, dv):
        """Ask for the Tudat outputs you need: ``cols.add("name", dv.something(...), width=..., owner=self.name)``.
        ``dv`` is ``tudatpy.dynamics.propagation_setup.dependent_variable``."""

    def forces(self, t, state, arc: Arc):
        """Extra force (N) and torque (N m) in the body frame at time ``t``, or None. Called inside the
        integrator, so keep it cheap and free of side effects."""
        return None

    def on_arc_start(self, arc: Arc, state):
        pass

    def on_arc_end(self, arc: Arc, out, state):
        """Checks after the arc. ``out`` is a ``core.columns.ArcOutput``. Raise ``FlightFailure`` to end
        the flight."""

    def on_event(self, event: Event, state):
        pass

    def obs(self, state) -> dict:
        """Fields added to what the flight software sees. Measured values only, with their errors."""
        return {}

    def design_fields(self) -> list[str]:
        """The SysML / vehicle.json fields this model reads, for the compiler and the leak test."""
        return []


class Provider:
    """Base class for an environment provider. Subclasses implement the methods of their role."""
    role = "none"                  # e.g. "atmosphere.mars"
    name = "provider"
    default = False                # the provider used when nothing selects another

    def configure(self, ctx: FlightContext):
        pass

    def tudat_settings(self):
        """Settings to put on the Tudat body (an atmosphere, a gravity field, ...), or None if the core
        should call the methods below through a custom callback instead."""
        return None


# Methods each role's provider gives. Units are SI; angles in radians; t is seconds TDB since J2000.
ROLES = {
    "atmosphere.earth": ["density(h, lat, lon, t)", "temperature(h, lat, lon, t)", "speed_of_sound(h, lat, lon, t)"],
    "atmosphere.mars": ["density(h, lat, lon, t)", "temperature(h, lat, lon, t)", "speed_of_sound(h, lat, lon, t)"],
    "winds.earth": ["wind(h, lat, lon, t) -> (east, north, up) m/s"],
    "winds.mars": ["wind(h, lat, lon, t) -> (east, north, up) m/s"],
    "terrain.mars": ["elevation(lat, lon) -> m above the reference sphere", "slope(lat, lon) -> rad",
                     "rock_height(lat, lon) -> m"],
    "terrain.earth": ["elevation(lat, lon)"],
    "gravity.mars": ["tudat_settings() -> spherical harmonic settings"],
    "gravity.earth": ["tudat_settings() -> spherical harmonic settings"],
    "ephemeris": ["kernels() -> list of SPICE kernel paths"],
    "dust.mars": ["optical_depth(lat, lon, t)"],
    "space_weather": ["indices(t) -> dict(f107, f107a, ap)"],
}
