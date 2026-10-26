"""
Web tools: search, read pages, weather, YouTube, open URLs. No API keys -
DuckDuckGo's HTML endpoint for search, Open-Meteo for weather.
"""

from __future__ import annotations

import html
import re
import urllib.parse
import webbrowser

import requests

from .. import config
from . import tool

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}


def _clean(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


@tool(
    "web_search",
    "Search the web and return the top results (title, url, snippet). Use for news, "
    "facts that may have changed, prices, scores, anything current.",
    {"query": {"type": "string"}, "max_results": {"type": "integer"}},
    ["query"],
    category="web",
)
def web_search(query: str, max_results: int = 6) -> str:
    resp = requests.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=_UA, timeout=15)
    resp.raise_for_status()
    results = []
    for chunk in resp.text.split('class="result__a"')[1:]:
        head = re.search(r'href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.DOTALL)
        if not head:
            continue
        href, title = head.group(1), head.group(2)
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', chunk, re.DOTALL)
        snippet = snip.group(1) if snip else ""
        if "duckduckgo.com/l/" in href:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(href)).query)
            href = q.get("uddg", [href])[0]
        if "duckduckgo.com/y.js" in href:  # ads
            continue
        results.append(f"- {_clean(title)}\n  {href}\n  {_clean(snippet or '')}")
        if len(results) >= max(1, min(max_results, 10)):
            break
    return "\n".join(results) or f"No results for '{query}'."


@tool(
    "read_webpage",
    "Fetch a web page and return its readable text (for summarising articles, docs...).",
    {"url": {"type": "string"}, "max_chars": {"type": "integer"}},
    ["url"],
    category="web",
)
def read_webpage(url: str, max_chars: int = 8000) -> str:
    resp = requests.get(url, headers=_UA, timeout=20)
    resp.raise_for_status()
    text = resp.text
    text = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>", " ", text)
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", resp.text)
    text = _clean(re.sub(r"(?i)<(br|p|div|li|h\d)[^>]*>", "\n", text))
    text = re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text))
    cap = max(1000, min(max_chars, 20000))
    head = f"TITLE: {_clean(title.group(1))}\n\n" if title else ""
    return head + text[:cap]


@tool(
    "open_url",
    "Open a URL in the default browser. For a plain search, pass a search URL.",
    {"url": {"type": "string"}},
    ["url"],
    category="web",
    mutates=True,
)
def open_url(url: str) -> str:
    if not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
        url = "https://" + url
    webbrowser.open(url)
    return f"Opened {url}"


@tool(
    "play_youtube",
    "Find a video on YouTube and start playing it in the browser.",
    {"query": {"type": "string"}},
    ["query"],
    category="media",
    mutates=True,
)
def play_youtube(query: str) -> str:
    resp = requests.get("https://www.youtube.com/results", params={"search_query": query}, headers=_UA, timeout=15)
    ids = re.findall(r'"videoId":"([\w-]{11})"', resp.text)
    if not ids:
        webbrowser.open(f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}")
        return "Couldn't pick a video automatically, opened the search results instead."
    titles = re.findall(r'"title":\{"runs":\[\{"text":"(.*?)"\}', resp.text)
    webbrowser.open(f"https://www.youtube.com/watch?v={ids[0]}")
    return f"Playing: {titles[0] if titles else ids[0]}"


_WMO = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "rain showers",
    81: "rain showers",
    82: "violent rain showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}


def _fetch_weather(city: str, timeout: int = 10) -> tuple[dict, dict] | None:
    geo = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1}, timeout=timeout
    ).json()
    if not geo.get("results"):
        return None
    place = geo["results"][0]
    data = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "timezone": "auto",
            "forecast_days": 3,
        },
        timeout=timeout,
    ).json()
    return place, data


def weather_brief(city: str, timeout: int = 4) -> str | None:
    """'28 degrees and clear sky' - for the spoken greeting."""
    got = _fetch_weather(city, timeout)
    if not got:
        return None
    cur = got[1]["current"]
    return f"{cur['temperature_2m']:.0f} degrees and {_WMO.get(cur['weather_code'], 'fair')}"


def weather_summary(city: str, timeout: int = 10) -> str:
    got = _fetch_weather(city, timeout)
    if not got:
        return f"Couldn't find a place called {city}."
    place, data = got
    cur = data["current"]
    d = data["daily"]
    lines = [
        f"{place['name']}, {place.get('country', '')}: {cur['temperature_2m']:.0f}°C "
        f"(feels {cur['apparent_temperature']:.0f}°C), {_WMO.get(cur['weather_code'], 'unknown')}, "
        f"humidity {cur['relative_humidity_2m']}%, wind {cur['wind_speed_10m']:.0f} km/h",
    ]
    for i, day in enumerate(d["time"]):
        lines.append(
            f"{day}: {d['temperature_2m_min'][i]:.0f}-{d['temperature_2m_max'][i]:.0f}°C, "
            f"{_WMO.get(d['weather_code'][i], '?')}, rain chance {d['precipitation_probability_max'][i]}%"
        )
    return "\n".join(lines)


@tool(
    "weather",
    "Current weather and 3-day forecast for a city (defaults to the user's home city).",
    {"city": {"type": "string"}},
    category="web",
)
def weather(city: str = "") -> str:
    city = city or config.HOME_CITY
    if not city:
        return "No city given and JARVIS_CITY isn't set. Ask the user which city."
    return weather_summary(city)
