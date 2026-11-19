"""Named Tudat outputs (dependent variables).

Tudat returns every dependent variable of an arc as one flat array per time. Code that counts columns
breaks as soon as anyone adds an output: in October 2026 a three-wide vector added made the 6-DOF
code read the thrust two columns early on every flight in air (fixed). Here each output has a
name and a width, and readers ask by name.

    cols = ColumnSet()
    cols.add("altitude", dv.altitude(body, central))
    cols.add("aero_acc", dv.single_acceleration(aero, body, central), width=3)
    ...propagate with output_variables=cols.settings()...
    out = ArcOutput(cols, dependent_variable_history)
    out.last("aero_acc")          # numpy array of 3
    out.series("altitude")        # (times, values)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Column:
    name: str
    start: int
    width: int

    @property
    def stop(self):
        return self.start + self.width


class ColumnSet:
    """An ordered set of named outputs. Names are unique; the order is the order they were added."""

    def __init__(self):
        self._cols: dict[str, Column] = {}
        self._settings: list = []
        self._width = 0

    def add(self, name, setting, width=1, owner=None):
        """Request one output. ``owner`` is the model asking, for error messages. Asking twice for the same
        name returns the first column, so two models can share an output."""
        if name in self._cols:
            return self._cols[name]
        if width < 1:
            raise ValueError(f"column {name}: width must be at least 1")
        col = Column(name, self._width, int(width))
        self._cols[name] = col
        self._settings.append(setting)
        self._width += col.width
        return col

    def settings(self):
        return list(self._settings)

    @property
    def width(self):
        return self._width

    def __contains__(self, name):
        return name in self._cols

    def __getitem__(self, name):
        try:
            return self._cols[name]
        except KeyError:
            raise KeyError(f"no column named {name!r}; known: {sorted(self._cols)}") from None

    def names(self):
        return list(self._cols)


class ArcOutput:
    """The dependent-variable history of one arc, read by name."""

    def __init__(self, cols: ColumnSet, history: dict):
        self.cols = cols
        self.times = sorted(history)
        self._rows = history
        if self.times:
            n = len(np.atleast_1d(history[self.times[0]]))
            if n != cols.width:
                raise ValueError(f"Tudat returned {n} output columns but {cols.width} were requested "
                                 f"({cols.names()}): a width is wrong")

    def at(self, name, t):
        c = self.cols[name]
        row = np.asarray(self._rows[t], dtype=float)
        v = row[c.start:c.stop]
        return float(v[0]) if c.width == 1 else v

    def last(self, name):
        return self.at(name, self.times[-1])

    def series(self, name):
        return self.times, [self.at(name, t) for t in self.times]

    def __contains__(self, name):
        return name in self.cols
