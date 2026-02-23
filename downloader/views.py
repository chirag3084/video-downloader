# downloader/views.py
from django.shortcuts import render
from django.http import StreamingHttpResponse
import requests
from urllib.parse import urlparse
import os

# Optional: restrict domains if you want
ALLOWED_DOMAINS = set()  # leave empty to allow all direct URLs


def is_allowed_domain(url):
    if not ALLOWED_DOMAINS:
        return True
    try:
        netloc = urlparse(url).netloc.lower()
        return any(netloc.endswith(d) for d in ALLOWED_DOMAINS)
    except Exception:
        return False


def stream_video_from_url(request):
    if request.method == "GET":
        return render(request, "downloader/form.html", {})

    video_url = request.POST.get("video_url", "").strip()

    if not video_url:
        return render(request, "downloader/form.html",
                      {"error": "A video URL is required."}, status=400)

    if not is_allowed_domain(video_url):
        return render(request, "downloader/form.html",
                      {"error": "Domain not allowed. Use trusted direct URLs only."}, status=400)

    try:
        r = requests.get(video_url, stream=True, timeout=15)
        r.raise_for_status()
    except requests.exceptions.Timeout:
        return render(request, "downloader/form.html",
                      {"error": "Request timed out. The server took too long to respond."}, status=400)
    except requests.exceptions.ConnectionError:
        return render(request, "downloader/form.html",
                      {"error": "Could not connect to the URL. Please check the link and try again."}, status=400)
    except requests.exceptions.HTTPError as e:
        return render(request, "downloader/form.html",
                      {"error": f"The server returned an error: {e.response.status_code}."}, status=400)
    except Exception as e:
        return render(request, "downloader/form.html",
                      {"error": f"Failed to fetch video: {e}"}, status=400)

    # Validate content type — reject non-video responses
    content_type = r.headers.get("Content-Type", "application/octet-stream")
    if not content_type.startswith(("video/", "application/octet-stream")):
        r.close()
        return render(request, "downloader/form.html",
                      {"error": f"URL does not point to a video file (Content-Type: {content_type})."}, status=400)

    # Determine and sanitize filename from URL path
    parsed = urlparse(video_url)
    raw_name = os.path.basename(parsed.path) or "video.mp4"
    filename = "".join(c for c in raw_name if c.isalnum() or c in "._-") or "video.mp4"

    def stream_generator(resp):
        try:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    response = StreamingHttpResponse(stream_generator(r), content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    # Forward Content-Length so browsers can show download progress
    content_length = r.headers.get("Content-Length")
    if content_length:
        response["Content-Length"] = content_length

    return response
