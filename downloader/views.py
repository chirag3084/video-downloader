# downloader/views.py
from django.shortcuts import render
from django.http import StreamingHttpResponse, FileResponse
import requests
from urllib.parse import urlparse
import os
import re
import tempfile
import subprocess
import threading

# Platforms that need yt-dlp (not direct file URLs)
YT_DLP_DOMAINS = [
    "youtube.com", "youtu.be",
    "instagram.com", "twitter.com", "x.com",
    "tiktok.com", "facebook.com", "fb.watch",
    "vimeo.com", "dailymotion.com", "twitch.tv",
    "reddit.com", "drive.google.com",
]


def needs_yt_dlp(url):
    try:
        netloc = urlparse(url).netloc.lower().lstrip("www.")
        return any(netloc == d or netloc.endswith("." + d) for d in YT_DLP_DOMAINS)
    except Exception:
        return False


def sanitize_filename(name):
    name = re.sub(r'[^\w\s.\-]', '', name)
    name = re.sub(r'\s+', '_', name).strip("._- ")
    return name[:200] or "video"


def download_with_yt_dlp(url):
    """
    Downloads video to a temp file using yt-dlp.
    Returns (filepath, filename, error_message).
    """
    tmp_dir = tempfile.mkdtemp()
    output_template = os.path.join(tmp_dir, "%(title)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--max-filesize", "500m",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", output_template,
        url,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return None, None, "Download timed out (5 min limit). Try a shorter video."
    except FileNotFoundError:
        return None, None, "yt-dlp is not installed on the server. Run: pip install yt-dlp"

    if result.returncode != 0:
        # Extract a clean error message from yt-dlp stderr
        stderr = result.stderr or ""
        for line in reversed(stderr.splitlines()):
            line = line.strip()
            if line and "ERROR" in line:
                msg = re.sub(r'^.*ERROR\s*:\s*', '', line)
                return None, None, f"yt-dlp error: {msg}"
        return None, None, "yt-dlp failed to download the video. The URL may be private or unsupported."

    # Find the downloaded file
    files = [f for f in os.listdir(tmp_dir) if os.path.isfile(os.path.join(tmp_dir, f))]
    if not files:
        return None, None, "yt-dlp ran but no output file was found."

    filepath = os.path.join(tmp_dir, files[0])
    filename = sanitize_filename(os.path.splitext(files[0])[0]) + ".mp4"
    return filepath, filename, None


def delete_file_after_response(filepath):
    """Deletes a temp file and its parent directory after streaming."""
    try:
        os.remove(filepath)
        os.rmdir(os.path.dirname(filepath))
    except Exception:
        pass


def stream_video_from_url(request):
    if request.method == "GET":
        return render(request, "downloader/form.html", {})

    video_url = request.POST.get("video_url", "").strip()
    quality = request.POST.get("quality", "best")

    if not video_url:
        return render(request, "downloader/form.html",
                      {"error": "A video URL is required."}, status=400)

    # --- Route: yt-dlp for YouTube / social platforms / Google Drive ---
    if needs_yt_dlp(video_url):
        filepath, filename, error = download_with_yt_dlp(video_url)
        if error:
            return render(request, "downloader/form.html",
                          {"error": error, "video_url": video_url}, status=400)

        # Stream the temp file to the browser, then delete it
        file_handle = open(filepath, "rb")

        def file_iterator(fh, path):
            try:
                while True:
                    chunk = fh.read(8192)
                    if not chunk:
                        break
                    yield chunk
            finally:
                fh.close()
                threading.Thread(target=delete_file_after_response, args=(path,), daemon=True).start()

        response = StreamingHttpResponse(file_iterator(file_handle, filepath), content_type="video/mp4")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Content-Length"] = os.path.getsize(filepath)
        return response

    # --- Route: direct file URL ---
    try:
        r = requests.get(video_url, stream=True, timeout=15)
        r.raise_for_status()
    except requests.exceptions.Timeout:
        return render(request, "downloader/form.html",
                      {"error": "Request timed out.", "video_url": video_url}, status=400)
    except requests.exceptions.ConnectionError:
        return render(request, "downloader/form.html",
                      {"error": "Could not connect. Check the URL and try again.", "video_url": video_url}, status=400)
    except requests.exceptions.HTTPError as e:
        return render(request, "downloader/form.html",
                      {"error": f"Server returned error: {e.response.status_code}.", "video_url": video_url}, status=400)
    except Exception as e:
        return render(request, "downloader/form.html",
                      {"error": f"Failed to fetch video: {e}", "video_url": video_url}, status=400)

    content_type = r.headers.get("Content-Type", "application/octet-stream")
    if not content_type.startswith(("video/", "application/octet-stream")):
        r.close()
        return render(request, "downloader/form.html",
                      {"error": f"URL does not point to a video (Content-Type: {content_type}).",
                       "video_url": video_url}, status=400)

    parsed = urlparse(video_url)
    raw_name = os.path.basename(parsed.path) or "video"
    filename = sanitize_filename(os.path.splitext(raw_name)[0]) + ".mp4"

    def stream_generator(resp):
        try:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    response = StreamingHttpResponse(stream_generator(r), content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    if r.headers.get("Content-Length"):
        response["Content-Length"] = r.headers["Content-Length"]
    return response
