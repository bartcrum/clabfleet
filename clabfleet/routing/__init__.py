"""Logical routing views (OSPF, BGP, EVPN) of a topology, from its configs."""

from .graph import routing_view

__all__ = ["routing_view"]
