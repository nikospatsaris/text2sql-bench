"""Fetch World Bank data and load it into the benchmark database."""

import logging
import time

from .client import RetiredIndicator

log = logging.getLogger("bench.load")

START_YEAR = 2000
END_YEAR = 2023

# Chosen to span several topics so questions can join across them, and to vary
# in coverage: education spending is missing for far more country-years than
# population is, which is what makes "no data" questions meaningful.
INDICATORS = [
    ("NY.GDP.PCAP.CD", "economy"),
    ("NY.GDP.MKTP.CD", "economy"),
    ("SP.POP.TOTL", "population"),
    ("SP.URB.TOTL.IN.ZS", "population"),
    ("SP.DYN.LE00.IN", "health"),
    ("SH.XPD.CHEX.GD.ZS", "health"),
    ("SL.UEM.TOTL.ZS", "labour"),
    ("SE.XPD.TOTL.GD.ZS", "education"),
    ("IT.NET.USER.ZS", "infrastructure"),
    ("AG.LND.FRST.ZS", "environment"),
    ("EN.GHG.CO2.PC.CE.AR5", "environment"),
]


def _clean(value):
    """The API sends empty strings where it means nothing."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_economies(client, conn):
    rows = client.economies()
    now = time.time()

    regions, incomes = {}, {}
    for row in rows:
        region, income = row.get("region") or {}, row.get("incomeLevel") or {}
        # Aggregates carry the literal region id 'NA'.
        if _clean(region.get("id")) and region["id"] != "NA":
            regions[region["id"]] = _clean(region.get("value")) or region["id"]
        if _clean(income.get("id")) and income["id"] != "NA":
            incomes[income["id"]] = _clean(income.get("value")) or income["id"]

    conn.executemany("INSERT OR REPLACE INTO regions (region_id, name) VALUES (?,?)",
                     sorted(regions.items()))
    conn.executemany("INSERT OR REPLACE INTO income_levels (income_id, name) VALUES (?,?)",
                     sorted(incomes.items()))

    countries = 0
    for row in rows:
        region_id = _clean((row.get("region") or {}).get("id"))
        income_id = _clean((row.get("incomeLevel") or {}).get("id"))
        is_country = 1 if region_id and region_id != "NA" else 0
        countries += is_country
        conn.execute(
            """INSERT OR REPLACE INTO economies
               (iso3, iso2, name, region_id, income_id, capital_city,
                latitude, longitude, is_country)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (row["id"], _clean(row.get("iso2Code")), _clean(row.get("name")) or row["id"],
             region_id if is_country else None,
             income_id if is_country else None,
             _clean(row.get("capitalCity")),
             _number(row.get("latitude")), _number(row.get("longitude")), is_country),
        )
    conn.commit()
    log.info("loaded %d economies (%d countries, %d aggregates), %d regions, %d income levels",
             len(rows), countries, len(rows) - countries, len(regions), len(incomes))
    return {"economies": len(rows), "countries": countries}


def load_observations(client, conn, indicators=None, start=START_YEAR, end=END_YEAR):
    indicators = indicators or INDICATORS
    known = {r[0] for r in conn.execute("SELECT iso3 FROM economies").fetchall()}
    # The observation endpoint puts the ISO-2 code in country.id and the ISO-3 in
    # countryiso3code -- the reverse of what the country endpoint suggests. Keep a
    # fallback map so a blank countryiso3code can still be resolved.
    by_iso2 = {r[0]: r[1] for r in conn.execute(
        "SELECT iso2, iso3 FROM economies WHERE iso2 IS NOT NULL").fetchall()}
    now = time.time()
    total_stored = 0

    for indicator_id, topic in indicators:
        try:
            _meta, rows = client.observations(indicator_id, start, end)
        except RetiredIndicator as exc:
            # Worth logging loudly rather than skipping quietly: an indicator that
            # vanished changes what the benchmark can ask.
            log.error("skipping %s: %s", indicator_id, exc)
            conn.execute(
                "INSERT INTO load_log (loaded_at, indicator_id, rows_returned, rows_stored, note)"
                " VALUES (?,?,0,0,?)", (now, indicator_id, "retired"))
            conn.commit()
            continue

        name = None
        stored = skipped_unknown = 0
        for row in rows:
            if name is None:
                name = _clean((row.get("indicator") or {}).get("value"))
            value = _number(row.get("value"))
            if value is None:
                continue                      # no measurement: no row (see schema.sql)
            iso3 = _clean(row.get("countryiso3code"))
            if iso3 is None:
                iso3 = by_iso2.get(_clean((row.get("country") or {}).get("id")))
            if iso3 not in known:
                skipped_unknown += 1
                continue
            year = int(row["date"])
            conn.execute(
                """INSERT OR REPLACE INTO observations (iso3, indicator_id, year, value)
                   VALUES (?,?,?,?)""", (iso3, indicator_id, year, value))
            stored += 1

        conn.execute("INSERT OR REPLACE INTO indicators (indicator_id, name, topic) VALUES (?,?,?)",
                     (indicator_id, name or indicator_id, topic))
        conn.execute(
            "INSERT INTO load_log (loaded_at, indicator_id, rows_returned, rows_stored, note)"
            " VALUES (?,?,?,?,?)",
            (now, indicator_id, len(rows), stored,
             "skipped %d rows for unknown economies" % skipped_unknown if skipped_unknown else None))
        conn.commit()
        # Storing nothing from a full response is a bug in this loader, not a
        # fact about the world. Saying so here is the difference between a
        # five-minute fix and a benchmark quietly built on an empty table.
        if rows and not stored:
            raise RuntimeError(
                "%s: the API returned %d rows and none were stored -- every row was "
                "dropped as an unknown economy, which means the identifier field "
                "changed shape" % (indicator_id, len(rows)))

        total_stored += stored
        log.info("%-22s %5d of %5d rows stored (%.0f%% of country-years have data)",
                 indicator_id, stored, len(rows), 100.0 * stored / len(rows) if rows else 0)

    return {"observations": total_stored}


def load_all(client, conn):
    result = load_economies(client, conn)
    result.update(load_observations(client, conn))
    return result
