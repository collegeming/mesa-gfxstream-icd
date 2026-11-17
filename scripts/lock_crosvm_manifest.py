#!/usr/bin/env python3
"""Resolve the reviewed minimal manifest; no repo sync, builds, or device access.

Run with -I. This creates a NEW fixed baseline, not a reconstruction of run43938110284.
Unsupported manifest composition is rejected rather than silently discarded.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

MANIFEST_COMMIT = "6ee0c357c461f09245a0378c55459993f067876e"
SOURCE_PINS = {
    "external/crosvm": "849e05f12d5c8d4c36af8e15763986d242cd43b5",
    "external/virglrenderer": "899a4e179a5793c5aa86bbf0799b0683c6746422",
}
ALLOWED_FETCHES = {
    "https://android.googlesource.com/",
    "https://github.com/Droid-VM/",
    "https://github.com/Crosvm-Android/",
}
SHA = re.compile(r"[0-9a-f]{40}")
SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")
ATTRS = {
    "remote": {"name", "fetch", "review", "revision"},
    "default": {"remote", "revision", "sync-j"},
    "project": {"name", "path", "remote", "revision", "groups"},
    "linkfile": {"src", "dest"},
    "copyfile": {"src", "dest"},
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def ls_remote(url, revision):
    """Resolve exactly one branch/tag; never fall back to HEAD or a nearby ref."""
    require(not revision.startswith("-"), "invalid revision")
    if revision.startswith("refs/"):
        refs = [revision, revision + "^{}"]
    else:
        refs = ["refs/heads/" + revision, "refs/tags/" + revision,
                "refs/tags/" + revision + "^{}"]
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", url, *refs], check=True,
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    found = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        require(len(parts) == 2 and SHA.fullmatch(parts[0]) and parts[1] in refs,
                "unexpected ls-remote response")
        require(parts[1] not in found, "duplicate remote ref")
        found[parts[1]] = parts[0]
    branch = found.get("refs/heads/" + revision)
    tag = found.get("refs/tags/" + revision + "^{}", found.get("refs/tags/" + revision))
    if revision.startswith("refs/"):
        selected = found.get(revision + "^{}", found.get(revision))
    else:
        require(not (branch and tag), "ambiguous branch/tag: " + revision)
        selected = branch or tag
    require(selected is not None, "unresolved revision: " + revision)
    return selected


def lock_manifest(raw, resolve=ls_remote):
    require(b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper(),
            "DTD/entities are unsupported")
    root = ET.fromstring(raw)
    require(root.tag == "manifest" and not root.attrib, "unexpected manifest root")
    remotes, defaults, projects = {}, [], []
    for node in root:
        require(node.tag in {"remote", "default", "project"},
                "unsupported manifest element: " + str(node.tag))
        require(set(node.attrib) <= ATTRS[node.tag], "unsupported attributes: " + node.tag)
        if node.tag == "project":
            for child in node:
                require(child.tag in {"linkfile", "copyfile"} and not list(child),
                        "unsupported project child: " + str(child.tag))
                require(set(child.attrib) == ATTRS[child.tag], "invalid file mapping")
            projects.append(node)
        else:
            require(not list(node), "unexpected nested manifest element")
            if node.tag == "remote":
                name = node.get("name")
                require(name and name not in remotes, "missing/duplicate remote")
                require(node.get("fetch") in ALLOWED_FETCHES, "unapproved remote fetch")
                remotes[name] = node
            else:
                defaults.append(node)
    require(len(defaults) == 1 and projects, "need one default and nonempty projects")
    default = defaults[0]
    paths = [p.get("path", p.get("name")) for p in projects]
    require(all(path and SAFE_NAME.fullmatch(path) and
                all(part not in {".", ".."} for part in path.split("/")) for path in paths),
            "invalid project path")
    require(all(p.get("name") and SAFE_NAME.fullmatch(p.get("name")) and
                all(part not in {".", ".."} for part in p.get("name").split("/")) for p in projects),
            "invalid project name")
    require(len(paths) == len(set(paths)), "duplicate project path")
    require("external/llvm-libc" not in paths, "llvm-libc already present: review seed")
    supplement = ET.SubElement(root, "project", {
        "path": "external/llvm-libc", "name": "platform/external/llvm-libc",
        "remote": "aosp", "revision": "refs/heads/android16-qpr2-release",
    })
    projects.append(supplement)
    records = []
    for project in projects:
        name, path = project.get("name", ""), project.get("path", project.get("name", ""))
        require(SAFE_NAME.fullmatch(name) and SAFE_NAME.fullmatch(path), "invalid project name/path")
        require(all(part not in {".", ".."} for part in path.split("/")), "unsafe path")
        remote_name = project.get("remote", default.get("remote"))
        require(remote_name in remotes, "unknown remote")
        remote = remotes[remote_name]
        revision = project.get("revision", remote.get("revision", default.get("revision")))
        require(revision, "missing revision")
        url = remote.get("fetch") + name
        if path in SOURCE_PINS:
            require(remote_name == "droidvm" and name == path.split("/")[-1],
                    "source pin remote/name changed")
            commit = SOURCE_PINS[path]
            method = "historical artifact source pin; fetchability checked at future sync"
        elif SHA.fullmatch(revision):
            commit, method = revision, "input exact SHA"
        else:
            commit, method = resolve(url, revision), "ls-remote exact ref"
        require(SHA.fullmatch(commit), "resolver did not return full SHA")
        project.set("revision", commit)
        project.set("remote", remote_name)
        records.append({"path": path, "name": name, "url": url,
                        "requested_revision": revision, "commit": commit, "method": method})
        print(path + " " + commit, flush=True)
    require(set(SOURCE_PINS) <= {r["path"] for r in records}, "missing historical source pin")
    # No moving default/remote revisions remain; every project has its own full SHA.
    default.attrib.pop("revision", None)
    for remote in remotes.values():
        remote.attrib.pop("revision", None)
    ET.indent(root, space="  ")
    locked = ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"
    return locked, records


def selfcheck():
    seed = b'''<manifest><remote name="aosp" fetch="https://android.googlesource.com/"/>
<remote name="droidvm" fetch="https://github.com/Droid-VM/"/>
<default remote="aosp" revision="main"/>
<project path="external/crosvm" name="crosvm" remote="droidvm" revision="droidvm"/>
<project path="external/virglrenderer" name="virglrenderer" remote="droidvm" revision="droidvm"/>
<project path="build/make" name="platform/build"><linkfile src="a" dest="b"/></project>
</manifest>'''
    result, records = lock_manifest(seed, lambda url, ref: "a" * 40)
    assert len(records) == 4 and b'<linkfile src="a" dest="b"' in result
    assert all(SHA.fullmatch(p.get("revision", "")) for p in ET.fromstring(result).findall("project"))
    assert records[-1]["path"] == "external/llvm-libc"
    for bad in (b"include", b"submanifest", b"extend-project", b"remove-project"):
        try:
            lock_manifest(seed.replace(b"</manifest>", b"<" + bad + b"/></manifest>"))
        except ValueError:
            pass
        else:
            raise AssertionError("unsupported composition accepted")
    def fail(url, ref):
        raise RuntimeError("ref unavailable")
    try:
        lock_manifest(seed, fail)
    except RuntimeError:
        pass
    else:
        raise AssertionError("resolution failure swallowed")
    from unittest.mock import patch
    url = "https://android.googlesource.com/platform/build"
    with patch.object(subprocess, "run") as run:
        run.return_value.stdout = "a" * 40 + "\trefs/heads/main\n"
        assert ls_remote(url, "main") == "a" * 40
        run.return_value.stdout += "b" * 40 + "\trefs/tags/main\n"
        try:
            ls_remote(url, "main")
        except ValueError:
            pass
        else:
            raise AssertionError("ambiguous branch/tag accepted")
        run.return_value.stdout = ("b" * 40 + "\trefs/tags/v1\n" +
                                   "c" * 40 + "\trefs/tags/v1^{}\n")
        assert ls_remote(url, "v1") == "c" * 40
        run.side_effect = subprocess.CalledProcessError(2, "git")
        try:
            ls_remote(url, "missing")
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("failed ref accepted")
    print("manifest lock selfcheck PASS (no network/build/device)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selfcheck", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.selfcheck:
        selfcheck()
        return
    require(args.manifest and args.output, "--manifest and --output required")
    require(not args.output.exists(), "output must be a new directory")
    raw = args.manifest.read_bytes()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    require(blob == "66d3fc3b2b07cdbed8156af818caa72d5bbe77c4",
            "input differs from reviewed manifest commit/blob")
    # Complete every resolution before publishing any apparently usable lock file.
    locked, projects = lock_manifest(raw)
    args.output.mkdir(parents=True)
    (args.output / "resolved-manifest.xml").write_bytes(locked)
    (args.output / "input-manifest.xml").write_bytes(raw)
    info = {
        "baseline_status": "NEW_FIXED_BASELINE_NOT_DEPLOYED_REPRODUCTION",
        "manifest_repository": "https://github.com/Droid-VM/crosvm-minimal-manifest",
        "manifest_commit": MANIFEST_COMMIT,
        "historical_ci_commit": "3c20ef7b333755e488f92946249ae92660385f35",
        "input_project_count": len(projects) - 1,
        "resolved_project_count": len(projects),
        "supplement": "external/llvm-libc",
        "input_manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "resolved_manifest_sha256": hashlib.sha256(locked).hexdigest(),
        "build_status": "NOT_BUILT", "device_status": "NOT_TESTED",
        "projects": projects,
    }
    (args.output / "lock-info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    lines = []
    for file in sorted(args.output.iterdir()):
        lines.append(hashlib.sha256(file.read_bytes()).hexdigest() + "  " + file.name)
    (args.output / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in info.items() if k != "projects"}, indent=2))


if __name__ == "__main__":
    main()
