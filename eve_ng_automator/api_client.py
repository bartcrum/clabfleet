"""EVE-NG REST API client.

Handles authentication, session management, and all CRUD operations against
the EVE-NG server API (Community and Professional editions).

Reference: EVE-NG API runs on https://<host>/api/
"""

import logging
import time
from typing import Any, Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class EveNgApiError(Exception):
    """Raised when an EVE-NG API call fails."""

    def __init__(self, message: str, status_code: int = 0, response: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class EveNgClient:
    """Low-level client for the EVE-NG REST API.

    Usage::

        client = EveNgClient("192.168.1.100", username="admin", password="eve")
        client.login()
        labs = client.list_labs("/")
        client.logout()

    Or as a context manager::

        with EveNgClient("192.168.1.100") as client:
            labs = client.list_labs("/")
    """

    def __init__(
        self,
        host: str,
        username: str = "admin",
        password: str = "eve",
        port: int = 443,
        ssl: bool = True,
        verify_ssl: bool = False,
    ):
        self.base_url = f"{'https' if ssl else 'http'}://{host}:{port}/api"
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self.session.headers.update({"Accept": "application/json"})
        self._logged_in = False

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.logout()
        return False

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def login(self) -> dict:
        """Authenticate and establish a session cookie."""
        resp = self._post("/auth/login", json={
            "username": self.username,
            "password": self.password,
            "html5": "-1",
        })
        self._logged_in = True
        logger.info("Logged in to EVE-NG at %s as %s", self.base_url, self.username)
        return resp

    def logout(self) -> dict:
        """Destroy the current session."""
        if not self._logged_in:
            return {}
        resp = self._get("/auth/logout")
        self._logged_in = False
        logger.info("Logged out from EVE-NG")
        return resp

    def status(self) -> dict:
        """Get server status (version, CPU, memory, etc.)."""
        return self._get("/status")

    # ------------------------------------------------------------------
    # Lab management
    # ------------------------------------------------------------------

    def list_labs(self, folder: str = "/") -> list:
        """List labs inside a folder."""
        folder = folder.rstrip("/") or "/"
        return self._get(f"/folders{folder}")

    def get_lab(self, lab_path: str) -> dict:
        """Get lab details."""
        return self._get(f"/labs/{_norm_lab(lab_path)}")

    def create_lab(
        self,
        name: str,
        path: str = "/",
        version: str = "1",
        description: str = "",
        author: str = "",
    ) -> dict:
        """Create a new lab (.unl file)."""
        return self._post(f"/labs", json={
            "path": path,
            "name": name,
            "version": version,
            "description": description,
            "author": author,
        })

    def delete_lab(self, lab_path: str) -> dict:
        """Delete a lab."""
        return self._delete(f"/labs/{_norm_lab(lab_path)}")

    def lock_lab(self, lab_path: str) -> dict:
        """Lock a lab (acquire exclusive access)."""
        return self._put(f"/labs/{_norm_lab(lab_path)}/Lock", json={})

    def unlock_lab(self, lab_path: str) -> dict:
        """Unlock a lab."""
        return self._put(f"/labs/{_norm_lab(lab_path)}/Unlock", json={})

    # ------------------------------------------------------------------
    # Node management
    # ------------------------------------------------------------------

    def list_nodes(self, lab_path: str) -> dict:
        """List all nodes in a lab."""
        return self._get(f"/labs/{_norm_lab(lab_path)}/nodes")

    def get_node(self, lab_path: str, node_id: int) -> dict:
        """Get a single node's details."""
        return self._get(f"/labs/{_norm_lab(lab_path)}/nodes/{node_id}")

    def create_node(self, lab_path: str, node_data: dict) -> dict:
        """Create a node in a lab.

        ``node_data`` keys (common):
            - template (str):  e.g. "vios", "csr1000v", "veos", "nxosv9k"
            - name (str):      display name
            - image (str):     image filename from /api/list/templates/<template>
            - ethernet (int):  number of ethernet interfaces
            - serial (int):    number of serial interfaces (optional)
            - ram (int):       RAM in MB (optional, uses template default)
            - cpu (int):       vCPU count (optional)
            - config (str):    "Exported" | "None" — startup config source
            - left (int):      X position on canvas (optional)
            - top (int):       Y position on canvas (optional)
            - icon (str):      icon name (optional)
            - console (str):   "telnet" | "vnc" (optional)
            - startup_config (str): base64/text of startup config (optional)
        """
        return self._post(f"/labs/{_norm_lab(lab_path)}/nodes", json=node_data)

    def update_node(self, lab_path: str, node_id: int, node_data: dict) -> dict:
        """Update a node's properties."""
        return self._put(
            f"/labs/{_norm_lab(lab_path)}/nodes/{node_id}", json=node_data
        )

    def delete_node(self, lab_path: str, node_id: int) -> dict:
        """Delete a node from a lab."""
        return self._delete(f"/labs/{_norm_lab(lab_path)}/nodes/{node_id}")

    def start_node(self, lab_path: str, node_id: int) -> dict:
        """Start (power on) a node."""
        return self._get(f"/labs/{_norm_lab(lab_path)}/nodes/{node_id}/start")

    def stop_node(self, lab_path: str, node_id: int) -> dict:
        """Stop (power off) a node."""
        return self._get(f"/labs/{_norm_lab(lab_path)}/nodes/{node_id}/stop")

    def wipe_node(self, lab_path: str, node_id: int) -> dict:
        """Wipe a node (reset NVRAM/disk)."""
        return self._get(f"/labs/{_norm_lab(lab_path)}/nodes/{node_id}/wipe")

    def export_node_config(self, lab_path: str, node_id: int) -> dict:
        """Export startup config from a running node."""
        return self._put(
            f"/labs/{_norm_lab(lab_path)}/nodes/{node_id}/export", json={}
        )

    def get_node_config(self, lab_path: str, node_id: int) -> dict:
        """Retrieve the saved startup config for a node."""
        return self._get(f"/labs/{_norm_lab(lab_path)}/nodes/{node_id}/config")

    def set_node_config(self, lab_path: str, node_id: int, config: str) -> dict:
        """Upload a startup config for a node."""
        return self._put(
            f"/labs/{_norm_lab(lab_path)}/nodes/{node_id}/config",
            json={"id": node_id, "data": config},
        )

    def get_node_interfaces(self, lab_path: str, node_id: int) -> dict:
        """Get interfaces for a node."""
        return self._get(f"/labs/{_norm_lab(lab_path)}/nodes/{node_id}/interfaces")

    def start_all_nodes(self, lab_path: str) -> dict:
        """Start all nodes in a lab."""
        return self._get(f"/labs/{_norm_lab(lab_path)}/nodes/start")

    def stop_all_nodes(self, lab_path: str) -> dict:
        """Stop all nodes in a lab."""
        return self._get(f"/labs/{_norm_lab(lab_path)}/nodes/stop")

    def wipe_all_nodes(self, lab_path: str) -> dict:
        """Wipe all nodes in a lab."""
        return self._get(f"/labs/{_norm_lab(lab_path)}/nodes/wipe")

    # ------------------------------------------------------------------
    # Network (bridge / cloud) management
    # ------------------------------------------------------------------

    def list_networks(self, lab_path: str) -> dict:
        """List all networks in a lab."""
        return self._get(f"/labs/{_norm_lab(lab_path)}/networks")

    def get_network(self, lab_path: str, net_id: int) -> dict:
        """Get network details."""
        return self._get(f"/labs/{_norm_lab(lab_path)}/networks/{net_id}")

    def create_network(self, lab_path: str, net_data: dict) -> dict:
        """Create a network in a lab.

        ``net_data`` keys:
            - name (str):       display name
            - type (str):       "bridge" | "ovs" | "pnet0"..."pnet9"
            - left (int):       X position (optional)
            - top (int):        Y position (optional)
            - visibility (int): 1=visible, 0=hidden (optional)
        """
        return self._post(f"/labs/{_norm_lab(lab_path)}/networks", json=net_data)

    def update_network(self, lab_path: str, net_id: int, net_data: dict) -> dict:
        """Update a network's properties."""
        return self._put(
            f"/labs/{_norm_lab(lab_path)}/networks/{net_id}", json=net_data
        )

    def delete_network(self, lab_path: str, net_id: int) -> dict:
        """Delete a network from a lab."""
        return self._delete(f"/labs/{_norm_lab(lab_path)}/networks/{net_id}")

    # ------------------------------------------------------------------
    # Link / interface-to-network connections
    # ------------------------------------------------------------------

    def connect_interface(
        self,
        lab_path: str,
        node_id: int,
        interface_id: int,
        network_id: int,
    ) -> dict:
        """Connect a node interface to a network."""
        return self._put(
            f"/labs/{_norm_lab(lab_path)}/nodes/{node_id}/interfaces",
            json={str(interface_id): str(network_id)},
        )

    def disconnect_interface(
        self,
        lab_path: str,
        node_id: int,
        interface_id: int,
    ) -> dict:
        """Disconnect a node interface from its network."""
        return self._put(
            f"/labs/{_norm_lab(lab_path)}/nodes/{node_id}/interfaces",
            json={str(interface_id): ""},
        )

    # ------------------------------------------------------------------
    # Topology (visual links) — read-only
    # ------------------------------------------------------------------

    def get_topology(self, lab_path: str) -> dict:
        """Get full topology (nodes, networks, links) for a lab."""
        return self._get(f"/labs/{_norm_lab(lab_path)}/topology")

    # ------------------------------------------------------------------
    # Templates / images
    # ------------------------------------------------------------------

    def list_templates(self) -> dict:
        """List all available node templates."""
        return self._get("/list/templates/")

    def get_template(self, template: str) -> dict:
        """Get details for a specific template (includes available images)."""
        return self._get(f"/list/templates/{template}")

    def list_network_types(self) -> dict:
        """List available network types."""
        return self._get("/list/networks")

    # ------------------------------------------------------------------
    # Folders
    # ------------------------------------------------------------------

    def create_folder(self, path: str, name: str) -> dict:
        """Create a folder for organizing labs."""
        return self._post("/folders", json={"path": path, "name": name})

    def delete_folder(self, path: str) -> dict:
        """Delete a folder."""
        return self._delete(f"/folders{path}")

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        logger.debug("%s %s %s", method.upper(), url, kwargs.get("json", ""))
        try:
            resp = self.session.request(method, url, **kwargs)
        except requests.ConnectionError as exc:
            raise EveNgApiError(f"Connection failed: {exc}") from exc

        if resp.status_code == 401 and self._logged_in:
            raise EveNgApiError("Session expired — re-login required", 401)

        try:
            body = resp.json()
        except ValueError:
            body = {"data": resp.text}

        if resp.status_code >= 400:
            msg = body.get("message", resp.text)
            raise EveNgApiError(
                f"API error {resp.status_code}: {msg}",
                status_code=resp.status_code,
                response=body,
            )

        return body.get("data", body)

    def _get(self, path: str, **kwargs) -> dict:
        return self._request("GET", path, **kwargs)

    def _post(self, path: str, **kwargs) -> dict:
        return self._request("POST", path, **kwargs)

    def _put(self, path: str, **kwargs) -> dict:
        return self._request("PUT", path, **kwargs)

    def _delete(self, path: str, **kwargs) -> dict:
        return self._request("DELETE", path, **kwargs)


def _norm_lab(lab_path: str) -> str:
    """Normalise a lab path — strip leading slash, ensure .unl extension."""
    lab_path = lab_path.lstrip("/")
    if not lab_path.endswith(".unl"):
        lab_path += ".unl"
    return lab_path
