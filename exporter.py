#!/usr/bin/env python3
"""Juniper Mist Prometheus Exporter - AP, Switch and Client metrics."""

import os
import time
import logging
import threading
import requests
from prometheus_client import start_http_server
from prometheus_client.core import GaugeMetricFamily, REGISTRY

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("mist_exporter")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MIST_API_BASE = os.environ.get("MIST_API_BASE", "https://api.mist.com/api/v1")
MIST_TOKEN = os.environ.get("MIST_TOKEN", "")
MIST_ORG_ID = os.environ.get("MIST_ORG_ID", "")
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL", "60"))
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "9100"))

# ---------------------------------------------------------------------------
# Mist API helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    return {"Authorization": f"Token {MIST_TOKEN}"}


# Reuse a single session across requests for connection pooling/keep-alive.
_session = requests.Session()
_session.headers.update(_headers())


def mist_get(path: str) -> list:
    """Fetch a paginated Mist API endpoint and return all items."""
    url = f"{MIST_API_BASE}{path}"
    items = []
    page = 1
    while True:
        log.debug("GET %s page=%d", url, page)
        resp = _session.get(url, params={"page": page, "limit": 100}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            items.extend(data)
            log.debug("  -> %d items (list response)", len(data))
            break
        results = data.get("results", data.get("items", []))
        items.extend(results)
        total = int(resp.headers.get("X-Page-Total", 1))
        log.debug("  -> page %d/%d, %d items this page", page, total, len(results))
        if page >= total:
            break
        page += 1
    return items


def mist_get_one(path: str) -> dict:
    """Fetch a single Mist API resource (non-paginated)."""
    url = f"{MIST_API_BASE}{path}"
    log.debug("GET %s", url)
    resp = _session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


class MistCollector:
    """Custom Prometheus collector for Juniper Mist metrics."""

    AP_STATUS_MAP = {"connected": 1, "disconnected": 0, "restarting": 2}
    SW_STATUS_MAP = {"connected": 1, "disconnected": 0}
    # Interface name prefixes considered physical ports (skip loopback, irb, vme, me0)
    PHYSICAL_PORT_PREFIXES = ("ge-", "xe-", "et-", "fe-")

    def __init__(self):
        # prometheus_client's HTTP server handles requests on a thread pool, so each
        # concurrent scrape gets its own switch-detail cache via thread-local storage.
        self._local = threading.local()

    def collect(self):
        t0 = time.monotonic()
        log.info("Scrape started")
        # Per-scrape cache for switch detail responses — avoids fetching the same
        # switch detail twice (once for port/poe/temp, once for wired clients).
        self._local.sw_detail_cache: dict[str, dict] = {}

        sites = self._get_sites()
        log.info("Found %d site(s)", len(sites))

        yield from self._collect_ap_metrics(sites)
        yield from self._collect_switch_metrics(sites)
        yield from self._collect_client_metrics(sites)

        self._local.sw_detail_cache = {}
        log.info("Scrape complete in %.2fs", time.monotonic() - t0)

    def _get_sites(self) -> list:
        try:
            sites = mist_get(f"/orgs/{MIST_ORG_ID}/sites")
            log.debug("Fetched %d sites", len(sites))
            return sites
        except Exception as exc:
            log.error("Failed to fetch sites: %s", exc)
            return []

    def _get_switch_detail(self, site_id: str, sw_id: str, sw_name: str) -> dict | None:
        """Return switch detail, using the per-scrape cache to avoid duplicate requests."""
        cache = self._local.sw_detail_cache
        cache_key = f"{site_id}/{sw_id}"
        if cache_key in cache:
            log.debug("Switch detail cache hit for %s", sw_name)
            return cache[cache_key]
        try:
            detail = mist_get_one(f"/sites/{site_id}/stats/devices/{sw_id}")
            cache[cache_key] = detail
            return detail
        except Exception as exc:
            log.warning("Failed to fetch detail for switch %s: %s", sw_name, exc)
            return None

    # ------------------------------------------------------------------ #
    #  Access Points                                                        #
    # ------------------------------------------------------------------ #

    def _collect_ap_metrics(self, sites: list):
        ap_status = GaugeMetricFamily(
            "mist_ap_status",
            "Access Point connection status (1=connected, 0=disconnected, 2=restarting)",
            labels=["org_id", "site_id", "site_name", "ap_id", "ap_name", "mac", "model", "ip", "version"],
        )
        ap_clients = GaugeMetricFamily(
            "mist_ap_num_clients",
            "Number of clients associated to an AP",
            labels=["org_id", "site_id", "site_name", "ap_id", "ap_name", "mac", "model", "ip", "version"],
        )
        ap_uptime = GaugeMetricFamily(
            "mist_ap_uptime_seconds",
            "AP uptime in seconds",
            labels=["org_id", "site_id", "site_name", "ap_id", "ap_name", "mac", "model", "ip", "version"],
        )

        for site in sites:
            site_id = site.get("id", "")
            site_name = site.get("name", "")
            try:
                devices = mist_get(f"/sites/{site_id}/stats/devices?type=ap")
            except Exception as exc:
                log.warning("Failed to fetch AP stats for site %s: %s", site_name, exc)
                continue

            log.info("Site '%s': %d AP(s)", site_name, len(devices))
            for ap in devices:
                labels = [
                    MIST_ORG_ID,
                    site_id,
                    site_name,
                    ap.get("id", ""),
                    ap.get("name", ""),
                    ap.get("mac", ""),
                    ap.get("model", ""),
                    ap.get("ip", ""),
                    ap.get("version", ""),
                ]
                status_str = ap.get("status", "disconnected").lower()
                status_val = self.AP_STATUS_MAP.get(status_str, 0)
                ap_status.add_metric(labels, status_val)
                ap_clients.add_metric(labels, ap.get("num_clients", 0))
                ap_uptime.add_metric(labels, ap.get("uptime", 0))

        yield ap_status
        yield ap_clients
        yield ap_uptime

    # ------------------------------------------------------------------ #
    #  Switches                                                             #
    # ------------------------------------------------------------------ #

    def _collect_switch_metrics(self, sites: list):
        sw_status = GaugeMetricFamily(
            "mist_switch_status",
            "Switch connection status (1=connected, 0=disconnected)",
            labels=["org_id", "site_id", "site_name", "switch_id", "switch_name", "mac", "model", "version"],
        )
        sw_uptime = GaugeMetricFamily(
            "mist_switch_uptime_seconds",
            "Switch uptime in seconds",
            labels=["org_id", "site_id", "site_name", "switch_id", "switch_name", "mac", "model", "version"],
        )
        sw_cpu = GaugeMetricFamily(
            "mist_switch_cpu_usage",
            "Switch CPU usage (0-100)",
            labels=["org_id", "site_id", "site_name", "switch_id", "switch_name", "mac", "model", "version"],
        )
        sw_mem = GaugeMetricFamily(
            "mist_switch_memory_usage",
            "Switch memory usage (0-100)",
            labels=["org_id", "site_id", "site_name", "switch_id", "switch_name", "mac", "model", "version"],
        )
        # Per-port metrics — use CounterMetricFamily for cumulative packet/byte counters
        port_up = GaugeMetricFamily(
            "mist_switch_port_up",
            "Switch port link status (1=up, 0=down)",
            labels=["org_id", "site_id", "site_name", "switch_id", "switch_name", "mac", "port_id"],
        )
        port_rx_pkts = GaugeMetricFamily(
            "mist_switch_port_rx_packets_total",
            "Total received packets on switch port",
            labels=["org_id", "site_id", "site_name", "switch_id", "switch_name", "mac", "port_id"],
        )
        port_tx_pkts = GaugeMetricFamily(
            "mist_switch_port_tx_packets_total",
            "Total transmitted packets on switch port",
            labels=["org_id", "site_id", "site_name", "switch_id", "switch_name", "mac", "port_id"],
        )
        port_rx_bytes = GaugeMetricFamily(
            "mist_switch_port_rx_bytes_total",
            "Total received bytes on switch port",
            labels=["org_id", "site_id", "site_name", "switch_id", "switch_name", "mac", "port_id"],
        )
        port_tx_bytes = GaugeMetricFamily(
            "mist_switch_port_tx_bytes_total",
            "Total transmitted bytes on switch port",
            labels=["org_id", "site_id", "site_name", "switch_id", "switch_name", "mac", "port_id"],
        )
        # PoE metrics (per module/FPC — Mist does not expose per-port PoE draw)
        poe_max_power = GaugeMetricFamily(
            "mist_switch_poe_max_power_watts",
            "PoE total power budget in watts",
            labels=["org_id", "site_id", "site_name", "switch_id", "switch_name", "mac", "module"],
        )
        poe_power_draw = GaugeMetricFamily(
            "mist_switch_poe_power_draw_watts",
            "PoE actual power draw in watts",
            labels=["org_id", "site_id", "site_name", "switch_id", "switch_name", "mac", "module"],
        )
        poe_power_allocated = GaugeMetricFamily(
            "mist_switch_poe_power_allocated_watts",
            "PoE power allocated to connected devices in watts",
            labels=["org_id", "site_id", "site_name", "switch_id", "switch_name", "mac", "module"],
        )
        poe_power_reserved = GaugeMetricFamily(
            "mist_switch_poe_power_reserved_watts",
            "PoE power reserved (budget minus allocated) in watts",
            labels=["org_id", "site_id", "site_name", "switch_id", "switch_name", "mac", "module"],
        )
        # Temperature metrics (per sensor, per module)
        sw_temp = GaugeMetricFamily(
            "mist_switch_temperature_celsius",
            "Switch temperature sensor reading in Celsius",
            labels=["org_id", "site_id", "site_name", "switch_id", "switch_name", "mac", "module", "sensor"],
        )
        sw_temp_ok = GaugeMetricFamily(
            "mist_switch_temperature_ok",
            "Switch temperature sensor status (1=ok, 0=not ok)",
            labels=["org_id", "site_id", "site_name", "switch_id", "switch_name", "mac", "module", "sensor"],
        )

        for site in sites:
            site_id = site.get("id", "")
            site_name = site.get("name", "")
            try:
                devices = mist_get(f"/sites/{site_id}/stats/devices?type=switch")
            except Exception as exc:
                log.warning("Failed to fetch switch stats for site %s: %s", site_name, exc)
                continue

            log.info("Site '%s': %d switch(es)", site_name, len(devices))
            for sw in devices:
                sw_id = sw.get("id", "")
                sw_name = sw.get("name", "")
                sw_mac = sw.get("mac", "")

                labels = [
                    MIST_ORG_ID,
                    site_id,
                    site_name,
                    sw_id,
                    sw_name,
                    sw_mac,
                    sw.get("model", ""),
                    sw.get("version", ""),
                ]
                status_str = sw.get("status", "disconnected").lower()
                status_val = self.SW_STATUS_MAP.get(status_str, 0)
                sw_status.add_metric(labels, status_val)
                log.debug("Switch %s (%s): status=%s", sw_name, sw_mac, status_str)

                sw_uptime.add_metric(labels, sw.get("uptime", 0))
                sw_cpu.add_metric(labels, sw.get("cpu_usage", 0))
                sw_mem.add_metric(labels, sw.get("memory_usage", 0))

                # Fetch per-switch detail to get if_stat + module PoE
                # (the list endpoint returns empty if_stat)
                if status_val == 1:
                    self._collect_port_and_poe_metrics(
                        site_id, site_name, sw_id, sw_name, sw_mac,
                        port_up, port_rx_pkts, port_tx_pkts, port_rx_bytes, port_tx_bytes,
                        poe_max_power, poe_power_draw, poe_power_allocated, poe_power_reserved,
                        sw_temp, sw_temp_ok,
                    )
                else:
                    log.debug("Skipping port/PoE/temp for offline switch %s", sw_name)

        yield sw_status
        yield sw_uptime
        yield sw_cpu
        yield sw_mem
        yield port_up
        yield port_rx_pkts
        yield port_tx_pkts
        yield port_rx_bytes
        yield port_tx_bytes
        yield poe_max_power
        yield poe_power_draw
        yield poe_power_allocated
        yield poe_power_reserved
        yield sw_temp
        yield sw_temp_ok

    def _collect_port_and_poe_metrics(
        self, site_id, site_name, sw_id, sw_name, sw_mac,
        port_up, port_rx_pkts, port_tx_pkts, port_rx_bytes, port_tx_bytes,
        poe_max_power, poe_power_draw, poe_power_allocated, poe_power_reserved,
        sw_temp, sw_temp_ok,
    ):
        detail = self._get_switch_detail(site_id, sw_id, sw_name)
        if detail is None:
            return

        # Per-port interface stats
        if_stat = detail.get("if_stat", {})
        log.debug("Switch %s: %d interface(s) in if_stat", sw_name, len(if_stat))
        for iface_key, iface in if_stat.items():
            port_id = iface.get("port_id", iface_key)
            if not any(port_id.startswith(p) for p in self.PHYSICAL_PORT_PREFIXES):
                continue
            plabels = [MIST_ORG_ID, site_id, site_name, sw_id, sw_name, sw_mac, port_id]
            port_up.add_metric(plabels, 1 if iface.get("up") else 0)
            port_rx_pkts.add_metric(plabels, iface.get("rx_pkts", 0))
            port_tx_pkts.add_metric(plabels, iface.get("tx_pkts", 0))
            port_rx_bytes.add_metric(plabels, iface.get("rx_bytes", 0))
            port_tx_bytes.add_metric(plabels, iface.get("tx_bytes", 0))

        # PoE and temperature — both live in module_stat, single loop
        for mod in detail.get("module_stat", []):
            module_label = f"fpc{mod.get('_idx', mod.get('fpc_idx', 0))}"
            mlabels = [MIST_ORG_ID, site_id, site_name, sw_id, sw_name, sw_mac, module_label]

            poe = mod.get("poe")
            if poe:
                log.debug("Switch %s %s: PoE draw=%.1fW / max=%.1fW",
                          sw_name, module_label, poe.get("power_draw", 0), poe.get("max_power", 0))
                poe_max_power.add_metric(mlabels, poe.get("max_power", 0))
                poe_power_draw.add_metric(mlabels, poe.get("power_draw", 0))
                poe_power_allocated.add_metric(mlabels, poe.get("total_power_allocated", 0))
                poe_power_reserved.add_metric(mlabels, poe.get("power_reserved", 0))

            for sensor in mod.get("temperatures", []):
                sensor_name = sensor.get("name", "unknown")
                slabels = mlabels + [sensor_name]
                log.debug("Switch %s %s sensor '%s': %.1f°C status=%s",
                          sw_name, module_label, sensor_name,
                          sensor.get("celsius", 0), sensor.get("status", ""))
                sw_temp.add_metric(slabels, sensor.get("celsius", 0))
                sw_temp_ok.add_metric(slabels, 1 if sensor.get("status", "") == "ok" else 0)

    # ------------------------------------------------------------------ #
    #  Clients (WiFi + Wired)                                              #
    # ------------------------------------------------------------------ #

    def _collect_client_metrics(self, sites: list):
        # Site-level summaries
        site_wifi_total = GaugeMetricFamily(
            "mist_site_wifi_clients_total",
            "Total number of WiFi clients currently associated per site",
            labels=["org_id", "site_id", "site_name"],
        )
        site_wired_total = GaugeMetricFamily(
            "mist_site_wired_clients_total",
            "Total number of wired clients currently seen per site",
            labels=["org_id", "site_id", "site_name"],
        )

        # Per WiFi-client metrics (one time-series per client)
        wifi_rssi = GaugeMetricFamily(
            "mist_wifi_client_rssi",
            "WiFi client RSSI in dBm",
            labels=["org_id", "site_id", "site_name", "mac", "hostname", "ap_mac", "ssid", "band"],
        )
        wifi_snr = GaugeMetricFamily(
            "mist_wifi_client_snr",
            "WiFi client SNR in dB",
            labels=["org_id", "site_id", "site_name", "mac", "hostname", "ap_mac", "ssid", "band"],
        )
        wifi_tx_bytes = GaugeMetricFamily(
            "mist_wifi_client_tx_bytes_total",
            "Total bytes transmitted by WiFi client",
            labels=["org_id", "site_id", "site_name", "mac", "hostname", "ap_mac", "ssid", "band"],
        )
        wifi_rx_bytes = GaugeMetricFamily(
            "mist_wifi_client_rx_bytes_total",
            "Total bytes received by WiFi client",
            labels=["org_id", "site_id", "site_name", "mac", "hostname", "ap_mac", "ssid", "band"],
        )
        wifi_tx_bps = GaugeMetricFamily(
            "mist_wifi_client_tx_bps",
            "WiFi client current TX throughput in bits per second",
            labels=["org_id", "site_id", "site_name", "mac", "hostname", "ap_mac", "ssid", "band"],
        )
        wifi_rx_bps = GaugeMetricFamily(
            "mist_wifi_client_rx_bps",
            "WiFi client current RX throughput in bits per second",
            labels=["org_id", "site_id", "site_name", "mac", "hostname", "ap_mac", "ssid", "band"],
        )
        wifi_uptime = GaugeMetricFamily(
            "mist_wifi_client_uptime_seconds",
            "WiFi client association uptime in seconds",
            labels=["org_id", "site_id", "site_name", "mac", "hostname", "ap_mac", "ssid", "band"],
        )
        wifi_idle = GaugeMetricFamily(
            "mist_wifi_client_idle_seconds",
            "WiFi client idle time in seconds",
            labels=["org_id", "site_id", "site_name", "mac", "hostname", "ap_mac", "ssid", "band"],
        )
        wifi_tx_rate = GaugeMetricFamily(
            "mist_wifi_client_tx_rate_mbps",
            "WiFi client TX PHY rate in Mbps",
            labels=["org_id", "site_id", "site_name", "mac", "hostname", "ap_mac", "ssid", "band"],
        )
        wifi_rx_rate = GaugeMetricFamily(
            "mist_wifi_client_rx_rate_mbps",
            "WiFi client RX PHY rate in Mbps",
            labels=["org_id", "site_id", "site_name", "mac", "hostname", "ap_mac", "ssid", "band"],
        )

        # Per wired-client metrics (one time-series per client per port)
        wired_client = GaugeMetricFamily(
            "mist_wired_client_info",
            "Wired client presence (1=seen). Labels carry identity and port info.",
            labels=["org_id", "site_id", "site_name", "switch_mac", "port_id", "mac", "source"],
        )
        wired_clients_per_port = GaugeMetricFamily(
            "mist_switch_port_wired_clients",
            "Number of wired clients seen on this switch port",
            labels=["org_id", "site_id", "site_name", "switch_mac", "port_id"],
        )

        for site in sites:
            site_id = site.get("id", "")
            site_name = site.get("name", "")
            slabels = [MIST_ORG_ID, site_id, site_name]

            # ---- WiFi clients ----
            try:
                wifi_clients = mist_get(f"/sites/{site_id}/stats/clients")
            except Exception as exc:
                log.warning("Failed to fetch WiFi clients for site %s: %s", site_name, exc)
                wifi_clients = []

            site_wifi_total.add_metric(slabels, len(wifi_clients))
            log.info("Site '%s': %d WiFi client(s)", site_name, len(wifi_clients))

            for c in wifi_clients:
                mac = c.get("mac", "")
                hostname = c.get("hostname", "")
                ap_mac = c.get("ap_mac", "")
                ssid = c.get("ssid", "")
                band = str(c.get("band", ""))
                cl = [MIST_ORG_ID, site_id, site_name, mac, hostname, ap_mac, ssid, band]
                wifi_rssi.add_metric(cl, c.get("rssi", 0))
                wifi_snr.add_metric(cl, c.get("snr", 0))
                wifi_tx_bytes.add_metric(cl, c.get("tx_bytes", 0))
                wifi_rx_bytes.add_metric(cl, c.get("rx_bytes", 0))
                wifi_tx_bps.add_metric(cl, c.get("tx_bps", 0))
                wifi_rx_bps.add_metric(cl, c.get("rx_bps", 0))
                wifi_uptime.add_metric(cl, c.get("uptime", 0))
                wifi_idle.add_metric(cl, c.get("idle_time", 0))
                wifi_tx_rate.add_metric(cl, c.get("tx_rate", 0))
                wifi_rx_rate.add_metric(cl, c.get("rx_rate", 0))

            # ---- Wired clients (from each switch detail) ----
            try:
                switches = mist_get(f"/sites/{site_id}/stats/devices?type=switch")
            except Exception as exc:
                log.warning("Failed to fetch switches for wired clients, site %s: %s", site_name, exc)
                switches = []

            total_wired = 0
            for sw in switches:
                sw_id = sw.get("id", "")
                sw_mac = sw.get("mac", "")
                sw_name = sw.get("name", sw_mac)
                if sw.get("status", "").lower() != "connected":
                    log.debug("Skipping wired clients for offline switch %s", sw_name)
                    continue

                detail = self._get_switch_detail(site_id, sw_id, sw_name)
                if detail is None:
                    continue

                # Count per port
                port_counts: dict = {}
                for wc in detail.get("clients", []):
                    client_mac = wc.get("mac", "")
                    source = wc.get("source", "")
                    for port_id in wc.get("port_ids", []):
                        wired_client.add_metric(
                            [MIST_ORG_ID, site_id, site_name, sw_mac, port_id, client_mac, source], 1
                        )
                        port_counts[port_id] = port_counts.get(port_id, 0) + 1
                        total_wired += 1

                for port_id, count in port_counts.items():
                    wired_clients_per_port.add_metric(
                        [MIST_ORG_ID, site_id, site_name, sw_mac, port_id], count
                    )
                log.debug("Switch %s: %d wired client(s) across %d port(s)",
                          sw_name, sum(port_counts.values()), len(port_counts))

            site_wired_total.add_metric(slabels, total_wired)
            log.info("Site '%s': %d wired client(s)", site_name, total_wired)

        yield site_wifi_total
        yield site_wired_total
        yield wifi_rssi
        yield wifi_snr
        yield wifi_tx_bytes
        yield wifi_rx_bytes
        yield wifi_tx_bps
        yield wifi_rx_bps
        yield wifi_uptime
        yield wifi_idle
        yield wifi_tx_rate
        yield wifi_rx_rate
        yield wired_client
        yield wired_clients_per_port


def main():
    if not MIST_TOKEN or not MIST_ORG_ID:
        raise SystemExit("MIST_TOKEN and MIST_ORG_ID environment variables are required.")

    REGISTRY.register(MistCollector())
    start_http_server(EXPORTER_PORT)
    log.info("Mist exporter listening on :%d (scrape interval %ds)", EXPORTER_PORT, SCRAPE_INTERVAL)
    while True:
        time.sleep(SCRAPE_INTERVAL)


if __name__ == "__main__":
    main()
