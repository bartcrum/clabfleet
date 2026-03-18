"""Topology teardown — cleanly destroy EVE-NG labs.

Supports full teardown (delete lab) or selective teardown (stop nodes,
wipe configs, remove specific resources).
"""

import logging
from typing import Optional

from .api_client import EveNgClient, EveNgApiError

logger = logging.getLogger(__name__)


class TeardownError(Exception):
    """Raised when a teardown operation fails."""


def teardown_lab(
    client: EveNgClient,
    lab_path: str,
    stop_first: bool = True,
    wipe_nodes: bool = True,
    delete_lab: bool = True,
) -> dict:
    """Fully tear down a lab.

    Args:
        client: Authenticated EveNgClient.
        lab_path: Path to the lab.
        stop_first: Stop all nodes before teardown.
        wipe_nodes: Wipe node NVRAM/disks before deletion.
        delete_lab: Delete the lab file after stopping/wiping.

    Returns:
        Summary of actions taken.
    """
    summary = {"lab": lab_path, "actions": []}

    # Stop all nodes
    if stop_first:
        logger.info("Stopping all nodes in '%s'", lab_path)
        try:
            client.stop_all_nodes(lab_path)
            summary["actions"].append("stopped_all_nodes")
        except EveNgApiError as exc:
            logger.warning("Failed to stop nodes: %s", exc)

    # Wipe all nodes
    if wipe_nodes:
        logger.info("Wiping all nodes in '%s'", lab_path)
        try:
            client.wipe_all_nodes(lab_path)
            summary["actions"].append("wiped_all_nodes")
        except EveNgApiError as exc:
            logger.warning("Failed to wipe nodes: %s", exc)

    # Delete the lab
    if delete_lab:
        logger.info("Deleting lab '%s'", lab_path)
        try:
            client.delete_lab(lab_path)
            summary["actions"].append("deleted_lab")
        except EveNgApiError as exc:
            raise TeardownError(f"Failed to delete lab: {exc}") from exc

    logger.info("Teardown complete for '%s': %s", lab_path, summary["actions"])
    return summary


def stop_lab(client: EveNgClient, lab_path: str) -> None:
    """Stop all nodes in a lab without deleting anything."""
    logger.info("Stopping all nodes in '%s'", lab_path)
    client.stop_all_nodes(lab_path)


def wipe_lab(client: EveNgClient, lab_path: str) -> None:
    """Wipe all node configs/NVRAM in a lab (nodes must be stopped)."""
    logger.info("Wiping all nodes in '%s'", lab_path)
    client.wipe_all_nodes(lab_path)


def teardown_node(
    client: EveNgClient,
    lab_path: str,
    node_id: int,
    stop: bool = True,
    wipe: bool = True,
    delete: bool = False,
) -> None:
    """Tear down a single node."""
    name = f"node {node_id}"
    try:
        info = client.get_node(lab_path, node_id)
        name = info.get("name", name)
    except EveNgApiError:
        pass

    if stop:
        logger.info("Stopping %s", name)
        try:
            client.stop_node(lab_path, node_id)
        except EveNgApiError as exc:
            logger.warning("Failed to stop %s: %s", name, exc)

    if wipe:
        logger.info("Wiping %s", name)
        try:
            client.wipe_node(lab_path, node_id)
        except EveNgApiError as exc:
            logger.warning("Failed to wipe %s: %s", name, exc)

    if delete:
        logger.info("Deleting %s", name)
        client.delete_node(lab_path, node_id)
