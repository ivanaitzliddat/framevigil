# FrameVigil

FrameVigil is a locally hosted visual alert monitor for reading text from cameras or USB video capture cards. Sources can be plugged into the computer running the Python service on macOS, Windows, or Linux. The service discovers those devices with FFmpeg, captures frames, and uses Tesseract for local OCR. A browser can also contribute a video source from the device opening the page. The dashboard shows previews, alert rules, and activity over a local or Tailscale connection.

Recent preview frames stay in memory for up to 30 seconds. When an alert fires, its frame stays in memory for up to one hour, subject to a 40 MB limit, so the alert can be opened to show OCR word locations. Matched OCR lines appear in the in-memory alert history; unrelated OCR text is discarded. Frames and alert history are not written to disk. Source names, device selections, scan intervals, and rules are saved in a private local config file so they survive a restart.

FrameVigil is an independent implementation with its own source code and documentation.

## Run locally

1. Install [FFmpeg](https://ffmpeg.org/download.html) and [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) on the server computer, and ensure both commands are on `PATH`. On macOS, `brew install ffmpeg tesseract` is one option.
2. Use Python 3.10 or newer. Create a virtual environment and install `requirements.txt` (`python -m venv .venv`, then `python -m pip install -r requirements.txt` inside it).
3. Run `python server.py` from this directory and open `http://127.0.0.1:8765/`.
4. Add a camera or capture card under **Server video sources**. For a hardware-free check, select **Demo status board (synthetic)** and add an `ERROR` rule. Set simple alert terms or detailed conditions for any source. The service retries if a configured device is unplugged.
5. Optionally connect a browser camera under **This device's camera**. Its capture runs only while that browser page is open.

## Stop or restart

**Stop all sources** in the dashboard stops server-side monitoring but keeps FrameVigil available. Closing a browser tab stops that tab's camera and sound; server-side sources keep running. **Shut down FrameVigil** asks for confirmation and then stops the Python service and all its capture workers. Other open dashboards will show that the server is offline. Restart the service with the same `python server.py` command and options to use it again. Active source settings are retained and resume on restart unless they were stopped first. Tailscale Serve may continue routing to port 8765 while the Python service is off; it works again when the service restarts.

The dashboard identifies configured, unconfigured, and disconnected server devices. When a new device appears, it is highlighted for setup; a configured device that disappears is marked disconnected even if monitoring was stopped. The server retries active sources when they reconnect.

Click an alert to inspect its frame and red rectangles around matched OCR words. The alert list can be searched, filtered by source and date, and sorted. The same match may alert again after 15 seconds until it is acknowledged. Acknowledgement suppresses that source, rule or condition, and normalized OCR sentence across all viewers for a configurable period between 1 minute and 7 days. The duration and active acknowledgements are saved in the server's private settings file. A new matching sentence or a match from another device still alerts. Sound can also be muted temporarily on a single browser, independently of acknowledgement. Detailed conditions support multiple keywords on one OCR line, optional background color (including color-only detection when OCR text is present), custom spoken text, and enabling or disabling individual conditions. Browser speech offers voice, rate, volume, sound pattern, and a test control. Sound requires the browser page to remain open.

By default, the server binds only to the loopback interface. It supports up to eight configured host video sources and one browser video source per page. Host sources, their rules, and their running state persist in `~/.config/framevigil/sources.json`; acknowledgement settings persist in `~/.config/framevigil/settings.json`. Active sources resume after a server restart. Recent alerts and frames reset when the server exits.

On macOS, discovering a camera does not confirm permission to capture from it. If a camera remains in **Waiting** with a timeout, approve Camera access for the app running the Python service in **System Settings → Privacy & Security → Camera** while signed into the Mac's graphical desktop. A process started through SSH may be unable to display that permission prompt. The synthetic demo feed remains usable without camera permission.

## Private remote access

Use a private HTTPS reverse proxy such as [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve) so browsers on other devices can use camera capture. Start the app with `--allowed-origin https://YOUR-TAILSCALE-HOST --access-token-file ~/.config/framevigil/access-token`, then run `tailscale serve --bg 8765`. The server generates a private access code in the specified file on first start. Enter that code in the browser. Tailscale Serve limits the HTTPS site to your tailnet; it does not publish it to the internet.

The server also supports `--host 0.0.0.0` with an access token, for network setups that need a direct listener. Add the exact browser origin with `--allowed-origin`. Raw HTTP from another device cannot use browser camera APIs, so HTTPS is required for live capture.

## Security and privacy boundaries

- Browser capture requires the user's permission. Audio is never requested.
- Browser frames are sent to the configured server; host frames are captured there. Preview frames stay in memory briefly, and alert frames stay in memory for up to one hour so detections can be inspected. No frames or OCR text are written to disk.
- The server rejects requests from other web origins and caps each JPEG at 2 MB.
- Acknowledgement keys are hashes of source, rule or condition, and OCR sentence. Their expiry times are saved; the OCR sentence and screenshot are not saved in the settings file.
- When an access token file is configured, API calls require the bearer code. The browser keeps it in tab session storage.
- The default listener is restricted to `127.0.0.1`. For remote browser capture, use private HTTPS transport such as Tailscale Serve.

## Verify

Run `python -m unittest discover -s tests -v` and `node --check web/app.js`.
