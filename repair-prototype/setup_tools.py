"""Download the Windows build linked by ffmpeg.org into this project only."""
from pathlib import Path
import hashlib
import shutil
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

def download(url, dest, headers=(), direct=False):
    # Windows curl respects the system's common proxy/TLS configuration.
    git_curl = Path("C:/Program Files/Git/mingw64/bin/curl.exe")
    curl = str(git_curl) if git_curl.is_file() else (shutil.which("curl.exe") or shutil.which("curl"))
    if curl:
        proxy = urllib.request.getproxies().get("https")
        proxy_args = ["--noproxy", "*"] if direct else (["--proxy", proxy] if proxy else [])
        header_args = [value for header in headers for value in ("-H", header)]
        subprocess.run([curl, *proxy_args, *header_args, "--fail", "--location", "--retry", "3", "--silent", "--show-error",
                        "--connect-timeout", "30", "--max-time", "600",
                        "--output", str(dest), url], check=True)
    else:
        request = urllib.request.Request(url, headers={h.split(":", 1)[0]: h.split(":", 1)[1].strip() for h in headers})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if direct else urllib.request.build_opener()
        with opener.open(request, timeout=60) as response, dest.open("wb") as output:
            shutil.copyfileobj(response, output)

def main():
    tools = ROOT / ".tools"
    tools.mkdir(exist_ok=True)
    archive = tools / "ffmpeg.zip"
    print("Downloading FFmpeg, FFprobe and FFplay...", flush=True)
    download(URL, archive)
    checksum = tools / "ffmpeg.zip.sha256"
    download(URL + ".sha256", checksum)
    expected = checksum.read_text().split()[0].lower()
    with archive.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != expected:
        raise RuntimeError("SHA256 mismatch; downloaded tools were not installed")
    with zipfile.ZipFile(archive) as bundle:
        for name in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"):
            member = next(n for n in bundle.namelist() if n.endswith("/bin/" + name))
            with bundle.open(member) as source, (tools / name).open("wb") as dest:
                shutil.copyfileobj(source, dest)
        for member in bundle.namelist():
            if member.endswith("LICENSE") or member.endswith("LICENSE.txt"):
                (tools / "FFMPEG-LICENSE.txt").write_bytes(bundle.read(member))
    archive.unlink()
    print("Tools installed in " + str(tools), flush=True)

if __name__ == "__main__":
    main()
