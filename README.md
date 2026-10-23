# Bandwidth Test

A small self-hosted speed test. You run the server and open the page in a browser. The page measures download and upload speed to that server, latency and an optional bandwidth limit. The server uses only the Python standard library, so there is nothing to install.

## Quick start

```sh
python3 server.py --port 8080
```

Then open `http://<server-ip>:8080/` in a browser.

## Features

- Download and upload tests, run one at a time or back to back
- Latency measurement
- A bandwidth limit you set on the page, from 1 to 10,000 Mbps on a log-scale slider, or no limit
- A live throughput graph with average and peak speeds
- Results saved on the server and shown to every browser, including the client and server IPs
- One test at a time: while a test is running, other browsers are told the server is busy, so tests don't share the link and skew each other's results
- A live bandwidth readout in the server console for each test, which can also be written to a log file

## Options

| Flag | Default | Description |
| --- | --- | --- |
| `--host` | `0.0.0.0` | Address to listen on |
| `--port` | `8080` | Port to listen on |
| `--max-mbps` | `0` | Hard cap per test that clients cannot exceed (`0` means no cap) |
| `--interval` | `1.0` | Seconds between live bandwidth lines in the console |
| `--log FILE` | none | Also append the live bandwidth lines to `FILE` |
| `--results FILE` | `results.json` | Where test results are saved, by default next to the script |
| `--keep` | `200` | Number of recent results to keep |

Example:

```sh
python3 server.py --port 8080 --max-mbps 500 --log bandwidth.log
```

## HTTP endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | The web UI (`index.html`) |
| `GET` | `/download?duration=S&mbps=M` | Streams random bytes for `S` seconds, paced to `M` Mbps (`0` = no limit) |
| `POST` | `/upload` | Reads and discards the request body |
| `GET` | `/ping` | Returns a tiny response, used to measure latency |
| `POST` | `/begin?test=ID` | Claims the server for one test. Returns `503` if another test is running |
| `POST` | `/end?test=ID` | Releases the server when the test is done |
| `GET` | `/config` | Returns the server version and limits as JSON |
| `GET` | `/results` | Returns recent results as JSON, newest first |
| `POST` | `/results` | Stores one result. The server adds the time and the source and destination IPs |

Download payloads are random bytes, so compression cannot inflate the measured speed. A single test runs for at most 60 seconds.

Only one test runs at a time. The page sends a test ID with every request. While one test is running, `/begin`, `/download` and `/upload` requests with any other ID get `503`. A test releases the server at `/end`. If its browser goes away without calling `/end`, the server is released 3 seconds after that test's last request.

## Requirements

- Python 3.7 or later
- A modern browser

## License

MIT. See [LICENSE](LICENSE).
