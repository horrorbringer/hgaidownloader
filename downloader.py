import argparse
import os
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import yt_dlp
from safe_paths import sanitize_filename

# Regex pattern to match common streaming manifests and direct video streams
MEDIA_URL_PATTERN = re.compile(
    r"\.(m3u8|mp4|mpd|flv|ts)(\?.*)?$", re.IGNORECASE
)


def download_with_ytdlp(target_url: str, output_dir: Path, referer: str = None,
                        cookies_from_browser: str = None) -> bool:
    """
    Downloads a direct media stream or webpage video using yt-dlp.
    Handles HLS (.m3u8), DASH (.mpd), and direct MP4 files with concurrent fragment fetching.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "outtmpl": str(output_dir / "%(title).100s [%(id)s].%(ext)s"),
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 8,
        "retries": 10,
        "fragment_retries": 10,
        "nocheckcertificate": False,
        "quiet": False,
        "no_warnings": False,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
        },
    }

    if referer:
        ydl_opts["http_headers"]["Referer"] = referer
        ydl_opts["http_headers"]["Origin"] = "/".join(referer.split("/")[:3])

    if cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_from_browser,)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([target_url])
        return True
    except Exception as exc:
        print(f"[!] yt-dlp direct pass failed: {exc}", file=sys.stderr)
        return False


def sniff_media_streams(page_url: str, timeout_ms: int = 25000, headless: bool = True) -> tuple[list[str], str]:
    """
    Launches a headless Chromium instance, navigates to the target page,
    simulates player interaction, and intercepts network requests for .m3u8/.mp4 URLs.
    """
    captured_streams: list[str] = []
    page_title = "downloaded_video"

    def handle_request(request):
        url = request.url
        # Ignore common ad/tracking segments while capturing video manifests
        if MEDIA_URL_PATTERN.search(url) and not url.endswith(".ts"):
            if url not in captured_streams:
                print(f"[+] Sniffed stream candidate: {url}")
                captured_streams.append(url)

    def handle_response(response):
        url = response.url
        content_type = response.headers.get("content-type", "").lower()
        if any(mime in content_type for mime in
               ["application/vnd.apple.mpegurl", "application/x-mpegurl", "video/mp4"]):
            if url not in captured_streams:
                print(f"[+] Sniffed stream via MIME ({content_type}): {url}")
                captured_streams.append(url)

    print(f"[*] Launching browser to inspect network traffic on: {page_url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
            ignore_https_errors=False,
        )
        page = context.new_page()
        page.on("request", handle_request)
        page.on("response", handle_response)

        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page_title = page.title() or page_title

            # Wait briefly for initial player scripts to initialize
            page.wait_for_timeout(4000)

            # Attempt to click common video player play buttons if stream hasn't loaded yet
            if not captured_streams:
                play_selectors = [
                    "video",
                    ".vjs-big-play-button",
                    ".dplayer-video-wrap",
                    ".art-video-player",
                    ".plyr__control--overlaid",
                    "[aria-label='Play']",
                ]
                for selector in play_selectors:
                    try:
                        loc = page.locator(selector).first
                        if loc.is_visible(timeout=1000):
                            loc.click(timeout=2000, force=True)
                            page.wait_for_timeout(3000)
                            if captured_streams:
                                break
                    except Exception:
                        continue

            # Also check direct <video> or <source> DOM elements
            dom_sources = page.evaluate(
                """() => {
                    const urls = [];
                    document.querySelectorAll('video, source').forEach(el => {
                        if (el.src && el.src.startsWith('http')) urls.push(el.src);
                    });
                    return urls;
                }"""
            )
            for src in dom_sources:
                if src not in captured_streams:
                    print(f"[+] Found DOM video source: {src}")
                    captured_streams.append(src)

        except PlaywrightTimeoutError:
            print("[!] Page navigation timed out, checking any streams captured so far...")
        finally:
            browser.close()

    return captured_streams, page_title


def main():
    parser = argparse.ArgumentParser(
        description="Download videos from huangguoai.com or similar HLS/MP4 streaming pages."
    )
    parser.add_argument("url", help="Full URL of the video page to download")
    parser.add_argument(
        "-o", "--output", default="./downloads", help="Output directory (default: ./downloads)"
    )
    parser.add_argument(
        "--browser-cookies",
        choices=["chrome", "firefox", "edge", "brave"],
        default=None,
        help="Optional browser name to load session cookies from (if login/VIP is required)",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Run browser in visible window mode (useful if the site shows a CAPTCHA)",
    )
    args = parser.parse_args()

    output_dir = Path(args.output).resolve()

    print(f"[*] Step 1: Attempting direct extraction via yt-dlp for {args.url}")
    if download_with_ytdlp(args.url, output_dir, referer=args.url, cookies_from_browser=args.browser_cookies):
        print("[+] Download completed via direct extraction.")
        return

    print("[*] Step 2: Falling back to headless browser stream interception...")
    streams, title = sniff_media_streams(args.url, headless=not args.headful)

    if not streams:
        print(
            "[-] No .m3u8 or .mp4 streams were detected. "
            "Try running with --headful in case a CAPTCHA or manual click is required.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Prioritize .m3u8 master manifests first, then .mp4
    streams.sort(key=lambda u: (0 if ".m3u8" in u.lower() else 1))
    selected_stream = streams[0]
    safe_title = sanitize_filename(title)

    print(f"[*] Downloading captured stream: {selected_stream}")
    output_dir.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "outtmpl": str(output_dir / f"{safe_title}.%(ext)s"),
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 8,
        "nocheckcertificate": False,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            "Referer": args.url,
        },
    }
    if args.browser_cookies:
        ydl_opts["cookiesfrombrowser"] = (args.browser_cookies,)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([selected_stream])

    print(f"[+] Saved to: {output_dir}")


if __name__ == "__main__":
    main()