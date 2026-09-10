import gzip
import importlib.util
import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres
SCRIPT = Path(__file__).resolve().parents[2] / "deploy/mihomo_volume_maintenance.py"


def _module():
    spec = importlib.util.spec_from_file_location("mihomo_volume_maintenance", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _volume(module, root, name="a" * 64):
    module.VOLUME_ROOT = root
    directory = root / name / "_data"
    directory.mkdir(parents=True)
    for filename in module.EXPECTED_FILES:
        (directory / filename).write_bytes(b"" if filename == "config.yaml" else filename.encode())
    return {"Name": name, "Mountpoint": str(directory), "Driver": "local", "Labels": None, "Options": None}


@pytest.mark.parametrize("change", ["extra", "symlink", "config", "named"])
def test_volume_rejects_ambiguous_or_noncache_data(tmp_path, change):
    module = _module()
    volume = _volume(module, tmp_path)
    root = Path(volume["Mountpoint"])
    if change == "extra":
        (root / "business-data").write_text("preserve")
    elif change == "symlink":
        (root / "test").unlink()
        (root / "test").symlink_to(root / "cache.db")
    elif change == "config":
        (root / "config.yaml").write_text("preserve")
    else:
        volume["Name"] = "business-volume"
    with pytest.raises(ValueError):
        module.volume_record(volume)


def test_backup_deduplicates_files_and_restores_exact_bytes(tmp_path):
    module = _module()
    volume = _volume(module, tmp_path / "volumes")
    record = module.volume_record(volume)
    backups = tmp_path / "backups"
    backups.mkdir()
    first = module.backup_record(record, backups, frozenset())
    second = module.backup_record(record, backups, first)
    assert first == second and len(list(backups.iterdir())) == len(first)
    for item in record["files"]:
        with gzip.open(backups / (item["sha256"] + ".gz"), "rb") as stream:
            assert stream.read() == (Path(volume["Mountpoint"]) / item["name"]).read_bytes()


def _manifest(module, record, runtime=None):
    payload = {"version": 1, "release": "release", "runtime": runtime or [], "targets": [record], "rejected": []}
    return {**payload, "fingerprint": module.fingerprint(payload)}


def test_apply_rejects_reference_before_backup_or_remove(tmp_path, monkeypatch):
    module = _module()
    record = module.volume_record(_volume(module, tmp_path / "volumes"))
    manifest = _manifest(module, record, [{"volumes": [record["volume"]["Name"]]}])
    monkeypatch.setattr(module, "verify_current", lambda manifest: None)
    with pytest.raises(ValueError, match="target_is_referenced"):
        module.apply_manifest(manifest, output=tmp_path, expected_fingerprint=manifest["fingerprint"])
    assert not (tmp_path / "blobs").exists()


@pytest.mark.parametrize("suffix", ["", "/" + "a" * 64, "/" + "a" * 64 + "/_data/cache.db"])
def test_bind_mounts_of_volume_parent_or_file_also_protect_volume(tmp_path, suffix):
    module = _module()
    module.VOLUME_ROOT = tmp_path
    assert module.volume_is_referenced("a" * 64, [{"volumes": [], "binds": [str(tmp_path) + suffix]}])


def test_apply_rejects_changed_content_without_remove(tmp_path, monkeypatch):
    module = _module()
    volume = _volume(module, tmp_path / "volumes")
    manifest = _manifest(module, module.volume_record(volume))
    (Path(volume["Mountpoint"]) / "cache.db").write_text("changed")
    monkeypatch.setattr(module, "verify_current", lambda manifest: None)
    calls = []
    def command(args):
        calls.append(args)
        return json.dumps([volume])
    monkeypatch.setattr(module, "command", command)
    with pytest.raises(RuntimeError, match="content_drift"):
        module.apply_manifest(manifest, output=tmp_path, expected_fingerprint=manifest["fingerprint"])
    assert all("rm" not in args for args in calls)


def test_apply_removes_only_frozen_volume_after_backup_and_reads_back(tmp_path, monkeypatch):
    module = _module()
    volume = _volume(module, tmp_path / "volumes")
    manifest = _manifest(module, module.volume_record(volume))
    monkeypatch.setattr(module, "verify_current", lambda manifest: None)
    calls = []
    def command(args):
        calls.append(args)
        if args[:3] == ["docker", "volume", "inspect"]:
            return json.dumps([volume])
        if args[:3] == ["docker", "volume", "rm"]:
            assert list((tmp_path / "blobs").iterdir())
            return volume["Name"]
        return ""
    monkeypatch.setattr(module, "command", command)
    module.apply_manifest(manifest, output=tmp_path, expected_fingerprint=manifest["fingerprint"])
    assert [args for args in calls if "rm" in args] == [["docker", "volume", "rm", volume["Name"]]]
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "persisted_verified"


@pytest.mark.parametrize("labels", [None, {}, {"com.docker.volume.anonymous": ""}])
def test_docker_anonymous_label_is_recognized(tmp_path, labels):
    module = _module()
    volume = {**_volume(module, tmp_path), "Labels": labels}
    assert module.volume_record(volume)["volume"] == volume


@pytest.mark.parametrize("labels", [{"owner": "business"}, {"com.docker.volume.anonymous": "true"},
                                     {"com.docker.volume.anonymous": "", "owner": "business"}])
def test_other_volume_labels_remain_protected(tmp_path, labels):
    module = _module()
    volume = {**_volume(module, tmp_path), "Labels": labels}
    with pytest.raises(ValueError, match="identity_invalid"):
        module.volume_record(volume)
