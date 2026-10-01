import stat

import pytest

from clabfleet import cli, deployer
from clabfleet.cluster import ClusterConfig, HostInfo
from clabfleet.deployer import DeploymentError, LabDeployer, missing_images, node_images
from clabfleet.runner import CommandResult, LocalRunner
from clabfleet.topology import topology_from_dict


def _fake_docker(tmp_path, monkeypatch, present, server_ok=True):
    """Put a `docker` stand-in first on PATH that knows a fixed set of images."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "docker"
    images = " ".join(f'"{i}"' for i in present)
    script.write_text(f"""#!/bin/sh
case "$1" in
  version) exit {0 if server_ok else 1} ;;
  image) for i in {images}; do [ "$3" = "$i" ] && exit 0; done; exit 1 ;;
esac
exit 2
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")


def test_missing_images_runs_against_docker(tmp_path, monkeypatch):
    _fake_docker(tmp_path, monkeypatch, ["alpine:3", "ghcr.io/x/y:1 with space"])
    runner = LocalRunner()
    assert missing_images(runner, ["alpine:3", "ceos:4.35", "ghcr.io/x/y:1 with space"],
                          sudo=False) == ["ceos:4.35"]


def test_missing_images_without_docker_access(tmp_path, monkeypatch):
    _fake_docker(tmp_path, monkeypatch, [], server_ok=False)
    assert missing_images(LocalRunner(), ["alpine:3"], sudo=False) is None


def _topo():
    return topology_from_dict({
        "name": "t",
        "topology": {
            "kinds": {"arista_ceos": {"image": "ceos:4.35"}},
            "nodes": {
                "s1": {"kind": "arista_ceos"},
                "s2": {"kind": "arista_ceos"},
                "h1": {"kind": "linux", "image": "alpine:3"},
                "h2": {"kind": "linux", "image": "alpine:edge", "image-pull-policy": "Always"},
                "br": {"kind": "bridge"},
            },
        },
    })


def test_node_images_groups_nodes_and_skips_pull_always():
    assert node_images(_topo(), ["s1", "s2", "h1", "h2", "br"]) == {
        "ceos:4.35": ["s1", "s2"], "alpine:3": ["h1"],
    }


class FakeDockerRunner:
    """Answers the image-check script and `docker pull` like a host would."""

    def __init__(self, present, pullable=(), docker_needs_sudo=False):
        self.present, self.pullable = set(present), set(pullable)
        self.docker_needs_sudo = docker_needs_sudo
        self.pulled, self.sudo_used = [], set()

    def run(self, args, cwd=None, check=True, sudo=None, on_output=None):
        self.sudo_used.add(bool(sudo))
        if self.docker_needs_sudo and not sudo:
            return CommandResult(0, "NODOCKER\n", "")
        if args[:2] == ["docker", "pull"]:
            self.pulled.append(args[2])
            if on_output:
                on_output(f"pulling {args[2]}")
            if args[2] in self.pullable:
                self.present.add(args[2])
                return CommandResult(0, "", "")
            return CommandResult(1, "", "denied")
        assert args[:2] == ["sh", "-c"]
        missing = [i for i in args[4:] if i not in self.present]
        return CommandResult(0, "".join(f"MISSING {i}\n" for i in missing), "")

    def close(self):
        pass


def _deployer(monkeypatch, runners, multi=True, on_output=None):
    hosts = [HostInfo(name, f"10.0.0.{i}", sudo=True) for i, name in enumerate(runners, 1)]
    monkeypatch.setattr(deployer, "create_runner", lambda host: runners[host.name])
    return LabDeployer(ClusterConfig(hosts=hosts), on_output=on_output)


def _host_topos(mapping):
    return {host: {"topology": {"nodes": {n: {} for n in nodes}}}
            for host, nodes in mapping.items()}


def test_check_images_lists_every_missing_image_per_host(monkeypatch):
    runners = {"a": FakeDockerRunner({"ceos:4.35"}), "b": FakeDockerRunner(set())}
    dep = _deployer(monkeypatch, runners)
    with pytest.raises(DeploymentError) as err:
        dep._check_images(_topo(), _host_topos({"a": ["s1", "h1"], "b": ["s2", "h1"]}), pull=False)
    msg = str(err.value)
    assert "  a: alpine:3 (h1)" in msg
    assert "  b: ceos:4.35 (s2)" in msg
    assert "  b: alpine:3 (h1)" in msg
    assert "--pull" in msg and "--skip-image-check" in msg


def test_check_images_pulls_and_falls_back_to_sudo(monkeypatch):
    lines = []
    runners = {"a": FakeDockerRunner(set(), pullable={"alpine:3", "ceos:4.35"},
                                     docker_needs_sudo=True)}
    dep = _deployer(monkeypatch, runners, on_output=lines.append)
    dep._check_images(_topo(), _host_topos({"a": ["s1", "h1", "h2", "br"]}), pull=True)
    assert sorted(runners["a"].pulled) == ["alpine:3", "ceos:4.35"]
    assert True in runners["a"].sudo_used
    assert lines == ["pulling ceos:4.35", "pulling alpine:3"]  # single host: no prefix


def test_check_images_reports_images_that_fail_to_pull(monkeypatch):
    runners = {"a": FakeDockerRunner(set(), pullable={"alpine:3"})}
    dep = _deployer(monkeypatch, runners)
    with pytest.raises(DeploymentError) as err:
        dep._check_images(_topo(), _host_topos({"a": ["s1", "h1"]}), pull=True)
    assert "a: ceos:4.35 (s1)" in str(err.value)
    assert "alpine:3" not in str(err.value)
    assert "--pull" not in str(err.value)


def test_check_images_skips_hosts_without_docker_access(monkeypatch, caplog):
    runner = FakeDockerRunner(set(), docker_needs_sudo=True)
    monkeypatch.setattr(deployer, "create_runner", lambda host: runner)
    dep = LabDeployer(ClusterConfig(hosts=[HostInfo("a", "10.0.0.1", sudo=False)]))
    dep._check_images(_topo(), _host_topos({"a": ["s1"]}), pull=False)
    assert "Cannot reach Docker on a" in caplog.text


def test_cli_passes_image_flags(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(LabDeployer, "deploy",
                        lambda self, path, **kw: seen.update(kw) or {"hosts": {}})
    topo = tmp_path / "t.clab.yml"
    topo.write_text("name: t\ntopology:\n  nodes:\n    a: {kind: linux, image: alpine}\n")
    assert cli.main(["deploy", str(topo), "--pull"]) == 0
    assert seen["pull_images"] is True and seen["check_images"] is True
    assert cli.main(["deploy", str(topo), "--skip-image-check"]) == 0
    assert seen["check_images"] is False and seen["pull_images"] is False
