"""The core of marsflight: the flight loop and the interfaces that models plug into.

This package holds no physics. Physics lives in ``models/<area>/``, one folder per area.
A model is found by the registry, so adding one edits no shared file.

Until the move lands, ``sim.py`` still runs every flight and nothing here is called by it.
New adapters are written against these interfaces and tested on their own.
"""
from .columns import Column, ColumnSet, ArcOutput
from .model import Arc, Event, FlightContext, MassItem, Model, Provider
from .registry import Registry, registry

__all__ = ["Column", "ColumnSet", "ArcOutput", "Arc", "Event", "FlightContext", "MassItem", "Model",
           "Provider", "Registry", "registry"]
