"""Install official Windows AI and index-recovery releases in the project."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

from setup_tools import ROOT, download

AI_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip"
AI_ASSET_API = "https://api.github.com/repos/xinntao/Real-ESRGAN/releases/assets/63494469"
AI_SHA256 = "abc02804e17982a3be33675e4d471e91ea374e65b70167abc09e31acb412802d"
# Pinned from the official HTTPS asset; upstream did not supply a digest.
INDEX_API = "https://api.github.com/repos/anthwlock/untrunc/releases/latest"


def install_zip(url, destination, expected=None, asset_api=None):
    tools = ROOT / ".tools"
    cache = tools / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / (destination.name + "-asset.zip")
    if asset_api:
        try:
            download(asset_api + "?download=1", archive, headers=("Accept: application/octet-stream",), direct=True)
        except Exception:
            download(url, archive)
    else:
        download(url, archive)
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if expected and digest != expected:
        raise RuntimeError("Checksum mismatch for " + destination.name)
    with tempfile.TemporaryDirectory(prefix="install-", dir=tools) as staging:
        stage = Path(staging)
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                resolved = (stage / entry.filename).resolve()
                if not resolved.is_relative_to(stage.resolve()):
                    raise RuntimeError("Unsafe archive path")
            bundle.extractall(stage)
        destination.mkdir(exist_ok=True)
        shutil.copytree(stage, destination, dirs_exist_ok=True)
    archive.unlink()
    return digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ai", action="store_true")
    parser.add_argument("--index", action="store_true")
    args = parser.parse_args()
    tools = ROOT / ".tools"
    tools.mkdir(exist_ok=True)
    both = not args.ai and not args.index
    if args.ai or both:
        print("Installing Real-ESRGAN NCNN Vulkan and models...", flush=True)
        checksum = install_zip(AI_URL, tools / "realesrgan", AI_SHA256 or None, AI_ASSET_API)
        record = {"source": AI_URL, "version": "20220424 / v0.2.5.0", "sha256": checksum,
                          "upstream_digest_available": False,
                          "installed_at": datetime.now().astimezone().isoformat()}
        (tools / "realesrgan" / "install.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    if args.index or both:
        print("Installing Untrunc from the author's release...", flush=True)
        cache = tools / "downloads"
        cache.mkdir(exist_ok=True)
        metadata = cache / "untrunc-release.json"
        download(INDEX_API, metadata, direct=True)
        release = json.loads(metadata.read_text(encoding="utf-8"))
        asset = next(a for a in release["assets"] if a["name"] == "untrunc_x64.zip")
        url = asset["browser_download_url"]
        if not url.startswith("https://github.com/anthwlock/untrunc/releases/download/"):
            raise RuntimeError("Unexpected release URL")
        expected = (asset.get("digest") or "").removeprefix("sha256:")
        if len(expected) != 64:
            raise RuntimeError("Upstream release is missing its SHA256 digest")
        checksum = install_zip(url, tools / "untrunc", expected, asset["url"])
        record = {"source": url, "commit": release.get("target_commitish"),
                             "release_body": release.get("body"), "sha256": checksum,
                             "upstream_digest_available": True,
                             "installed_at": datetime.now().astimezone().isoformat()}
        (tools / "untrunc" / "install.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print("Extension engines installed. Restart the desktop app.", flush=True)


if __name__ == "__main__":
    main()
