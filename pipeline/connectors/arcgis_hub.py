"""Generic ArcGIS Hub / Esri REST connector.

Tempe has a verified, live per-permit FeatureServer (see build_tempe_connector
below) — confirmed by fetching real, current records (permits issued within
the last few days) with a rich field set including contractor name/license.
Chandler's public ArcGIS org only exposes aggregate KPI dashboards (e.g.
"Permits Issued" as a monthly count, not individual records) — no per-permit
endpoint has been found there, so it stays 'pending' (see
pipeline/config/jurisdictions.yaml).

Instantiating this connector without a service_url/field_map raises
ConnectorNotConfiguredError rather than silently returning nothing or,
worse, fabricated data — that's what protects any future unverified
jurisdiction from being wired in by accident.
"""

import time
from datetime import datetime, timezone
from typing import Iterable, Optional

import requests

from pipeline.config.settings import HTTP_REQUEST_DELAY_SECONDS, HTTP_USER_AGENT
from pipeline.connectors.base import BaseConnector, ConnectorNotConfiguredError

ESRI_PAGE_SIZE = 1000


class EsriFeature(dict):
    """A feature's attributes (the dict itself) with its geometry alongside.

    The geometry is an attribute, not a key, so the stored raw payload
    (``raw_source_json``) and its RAW-capture hash stay exactly the attribute
    record the source published; only mapped fields such as latitude and
    longitude read it.
    """

    def __init__(self, attributes: dict, geometry: Optional[dict] = None):
        super().__init__(attributes or {})
        self.geometry = geometry or None


def _geometry_coord(raw, axis: str) -> Optional[float]:
    geometry = getattr(raw, "geometry", None) or {}
    value = geometry.get(axis)
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


class ArcGISHubConnector(BaseConnector):
    def __init__(
        self,
        jurisdiction_slug: str,
        service_url: Optional[str],
        field_map: Optional[dict] = None,
        date_field: Optional[str] = None,
        city_name: Optional[str] = None,
        supports_pagination: bool = True,
        point_geometry_wgs84: bool = False,
    ):
        super().__init__(jurisdiction_slug)
        self.service_url = service_url.rstrip("/") if service_url else None
        self.field_map = field_map or {}
        self.date_field = date_field
        self.city_name = city_name
        # Some older MapServers (e.g. Chandler DSActiveProjects) reject
        # resultOffset with "Pagination is not supported." — those must be
        # fetched in a single request within maxRecordCount.
        self.supports_pagination = supports_pagination
        # Point layers can return each feature's location; ask for it in
        # WGS84 (outSR=4326) so no client-side reprojection is needed.
        self.point_geometry_wgs84 = point_geometry_wgs84
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": HTTP_USER_AGENT})

    def fetch_raw(self, since: Optional[datetime] = None) -> Iterable[dict]:
        if not self.service_url or not self.field_map:
            raise ConnectorNotConfiguredError(
                f"ArcGISHubConnector for '{self.jurisdiction_slug}' has no verified "
                "service_url/field_map yet. Locate and verify a real FeatureServer "
                "endpoint before running this connector — never fabricate records."
            )

        offset = 0
        where = "1=1"
        if since and self.date_field:
            since_str = since.strftime("%Y-%m-%d %H:%M:%S")
            where = f"{self.date_field} >= TIMESTAMP '{since_str}'"

        while True:
            params = {
                "where": where,
                "outFields": "*",
                "f": "json",
            }
            if self.supports_pagination:
                params["resultOffset"] = offset
                params["resultRecordCount"] = ESRI_PAGE_SIZE
            if self.point_geometry_wgs84:
                params["returnGeometry"] = "true"
                params["outSR"] = "4326"
            response = self.session.get(f"{self.service_url}/query", params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()

            # Esri REST often returns HTTP 200 with an embedded error object
            # instead of a 4xx/5xx status — raise_for_status() won't catch
            # that. Surface it loudly rather than silently yielding zero
            # records, which would look identical to "no permits found."
            if "error" in payload:
                raise RuntimeError(
                    f"ArcGIS query error for '{self.jurisdiction_slug}': {payload['error']}"
                )

            features = payload.get("features", [])

            if not features:
                break

            for feature in features:
                if self.point_geometry_wgs84:
                    yield EsriFeature(feature.get("attributes", {}), feature.get("geometry"))
                else:
                    yield feature.get("attributes", {})

            if not self.supports_pagination or len(features) < ESRI_PAGE_SIZE:
                break

            offset += ESRI_PAGE_SIZE
            time.sleep(HTTP_REQUEST_DELAY_SECONDS)

    def map_record(self, raw: dict) -> dict:
        mapped = self._empty_permit_dict()
        for target_field, source in self.field_map.items():
            if callable(source):
                mapped[target_field] = source(raw)
            else:
                mapped[target_field] = raw.get(source)
        if self.city_name and not mapped.get("city"):
            mapped["city"] = self.city_name
        return mapped


def _join_address(raw: dict) -> Optional[str]:
    line1 = raw.get("OriginalAddress1")
    line2 = raw.get("OriginalAddress2")
    if not line1:
        return None
    return f"{line1} {line2}".strip() if line2 else line1


# ------------------------------------------------------------------
# Tempe, AZ — verified live dataset
# (services.arcgis.com/lQySeXwbBg53XWDi/.../building_permits/FeatureServer/0)
# ------------------------------------------------------------------
TEMPE_FIELD_MAP = {
    "permit_number": "PermitNum",
    "permit_type": "PermitClass",
    "permit_subtype": "Type",
    "status": "StatusCurrent",
    "description": "Description",
    "project_description": "ProjectName",
    "filed_date": "AppliedDate",
    "issued_date": "IssuedDate",
    "expiration_date": "ExpiresDate",
    "finaled_date": "CompletedDate",
    "job_address": _join_address,
    "city": "OriginalCity",
    "state": "OriginalState",
    "zip": "OriginalZip",
    "latitude": lambda raw: raw.get("Latitude"),
    "longitude": lambda raw: raw.get("Longitude"),
    "valuation": lambda raw: raw.get("EstProjectCost"),
    "square_footage": lambda raw: raw.get("TotalSqFt"),
    "general_contractor_name": lambda raw: (
        raw.get("ContractorCompanyName")
        or (
            str(raw.get("ProjectName")).strip()
            if _looks_like_company(raw.get("ProjectName"))
            else None
        )
    ),
    "contractor_license_number": "ContractorLicNum",
}


def build_tempe_connector() -> ArcGISHubConnector:
    return ArcGISHubConnector(
        jurisdiction_slug="tempe_az",
        service_url="https://services.arcgis.com/lQySeXwbBg53XWDi/arcgis/rest/services/building_permits/FeatureServer/0",
        field_map=TEMPE_FIELD_MAP,
        date_field="IssuedDateDtm",
        city_name="Tempe",
    )


def _epoch_ms_to_date(epoch_ms: Optional[int]) -> Optional[str]:
    if not epoch_ms:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# ------------------------------------------------------------------
# Gilbert, AZ — verified live dataset (EnergovPermitData)
# (maps.gilbertaz.gov/.../Growth_Development_Tables_1/MapServer/3)
# Note: this source has NO contractor-identity field of any kind — every
# Gilbert permit will have general_contractor_name/plumbing_contractor_name
# = NULL. That's an honest gap in the source, not something to guess at.
# ------------------------------------------------------------------
GILBERT_FIELD_MAP = {
    "permit_number": "PermitNumber",
    "permit_type": "PermitType",
    "permit_subtype": "WorkClass",
    "status": "PermitStatus",
    "project_description": "ProjectName",
    "filed_date": lambda raw: _epoch_ms_to_date(raw.get("ApplyDate")),
    "issued_date": lambda raw: _epoch_ms_to_date(raw.get("IssuedDate")),
    "finaled_date": lambda raw: _epoch_ms_to_date(raw.get("FinalDate")),
    "job_address": "AddressFull",
    "city": "AddressCity",
    "state": "AddressState",
    "zip": "AddressZip",
    "parcel_number": "ParcelNumber",
    "latitude": lambda raw: _valid_az_latitude(raw.get("Latitude")),
    "longitude": lambda raw: _valid_az_longitude(raw.get("Longitude")),
    "valuation": lambda raw: raw.get("PermitValuation"),
}


def _valid_az_latitude(value) -> Optional[float]:
    # Some Gilbert records store Latitude/Longitude in a projected coordinate
    # system (state plane feet) instead of WGS84 degrees — clearly outside
    # Arizona's real latitude range. Null those out rather than storing
    # obviously-wrong coordinates; never fabricate a "corrected" value.
    if value is None or not (31.0 <= value <= 37.5):
        return None
    return value


def _valid_az_longitude(value) -> Optional[float]:
    if value is None or not (-115.0 <= value <= -108.5):
        return None
    return value


def build_gilbert_connector() -> ArcGISHubConnector:
    return ArcGISHubConnector(
        jurisdiction_slug="gilbert_az",
        service_url="https://maps.gilbertaz.gov/arcgis/rest/services/OD/Growth_Development_Tables_1/MapServer/3",
        field_map=GILBERT_FIELD_MAP,
        date_field="IssuedDate",
        city_name="Gilbert",
    )


# ------------------------------------------------------------------
# Scottsdale, AZ — verified live dataset ("Planning and Development
# Building Permits", maps.scottsdaleaz.gov/.../OpenData_Tabular/MapServer/12)
#
# This CORRECTS the earlier assessment in jurisdictions.yaml that Scottsdale
# was Accela-only with no public API. That assessment was about the wrong
# system (Accela Citizen Access / Scottsdale SPUR, the interactive search
# UI) — this separate open-data feature table has genuine individual permit
# records with owner and builder/contractor names, found via the same
# ArcGIS-org-search technique used for Tempe/Gilbert. Always re-verify
# rather than trusting a prior "pending" note as permanent.
# ------------------------------------------------------------------
SCOTTSDALE_FIELD_MAP = {
    "permit_number": lambda raw: str(raw.get("PermitNumber")) if raw.get("PermitNumber") is not None else None,
    "permit_type": "PermitType",
    "status": "PermitStatus",
    "issued_date": lambda raw: _epoch_ms_to_date(raw.get("IssueDate")),
    "job_address": "Address",
    "parcel_number": "APN",
    "valuation": lambda raw: raw.get("Valuation"),
    "square_footage": lambda raw: raw.get("AirConditionedSQFT"),
    "owner_name": "Owner",
    "general_contractor_name": lambda raw: raw.get("Builder") or raw.get("ResponsibleParty"),
    "latitude": lambda raw: _valid_az_latitude(raw.get("Latitude")),
    "longitude": lambda raw: _valid_az_longitude(raw.get("Longitude")),
    "permit_url": "CityOfScottsdaleMap",
}


def build_scottsdale_connector() -> ArcGISHubConnector:
    return ArcGISHubConnector(
        jurisdiction_slug="scottsdale_az",
        service_url="https://maps.scottsdaleaz.gov/arcgis/rest/services/OpenData_Tabular/MapServer/12",
        field_map=SCOTTSDALE_FIELD_MAP,
        date_field="IssueDate",
        city_name="Scottsdale",
    )


def _parse_currency_string(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------
# Chandler, AZ — verified live dataset, but narrower scope than
# Tempe/Gilbert/Scottsdale. This is Chandler's own ArcGIS Server
# (gis.chandleraz.gov), not an ArcGIS Online org — found by walking the
# server's /appsanonymous/rest/services folder tree directly (the org's
# public content search only surfaces aggregate KPI dashboards, per
# jurisdictions.yaml's prior "pending" note; the underlying per-permit
# layers were never in that search index).
#
# Layer covers only BLD (building) permits in the "Under Construction"
# project stage of Chandler's DSActiveProjects feed (an Accela-fed
# GIS layer of active development projects, not a general permit table) —
# confirmed real records with issued dates through ~2025-10-17, ~170
# records. Adjacent layers in the same MapServer track CIV (civil site
# plans), "Approved Projects", and "Completed Projects" if broader
# coverage is wanted later.
# GAP: no filed/applied date field exists in this layer (same gap as
# Mesa) — only BLD_F_ISSUED_DT (issued) and BLD_L_CO_DT (certificate of
# occupancy). GAP: no contractor-identity field. GAP: scoped to new
# construction/additions/remodels requiring a structural permit — routine
# trade-only permits (plumbing/electrical/mechanical) are NOT in this feed.
CHANDLER_FIELD_MAP = {
    "permit_number": "BLD_F_B1_ALT_ID",
    "permit_type": "BLD_F_PERM_TYPE",
    "status": lambda raw: "Under Construction",
    "project_description": "BLD_L_PROJ_NM",
    "description": "BLD_L_DTL_DESC",
    "issued_date": lambda raw: _epoch_ms_to_date(raw.get("BLD_F_ISSUED_DT")),
    "job_address": "BLD_F_FULL_ADDRESS",
    "valuation": lambda raw: _parse_currency_string(raw.get("BLD_L_JOB_VALUE")),
    "square_footage": lambda raw: _parse_currency_string(raw.get("BLD_L_SQ_FT")),
}


def build_chandler_connector() -> ArcGISHubConnector:
    return ArcGISHubConnector(
        jurisdiction_slug="chandler_az",
        service_url="https://gis.chandleraz.gov/appsanonymous/rest/services/DevelopmentServices/DSActiveProjects/MapServer/18",
        field_map=CHANDLER_FIELD_MAP,
        date_field=None,  # BLD_F_ISSUED_DT is a Date field but querying via `since` isn't verified here; full refresh only for now.
        city_name="Chandler",
        supports_pagination=False,  # MapServer rejects resultOffset ("Pagination is not supported.")
    )


# ------------------------------------------------------------------
# Peoria, AZ — verified live dataset (Peoria's own ArcGIS Server,
# gis.peoriaaz.gov, Accela folder). This is the richest source found in
# this pass: real filed AND issued dates (B1_FILE_DD / IssDate) with
# genuine variance, applicant/contractor name, project status, and a
# general building-permit scope (not narrowed to one permit type the way
# Chandler's feed is). Confirmed current — issue dates through the day
# of verification.
# NOTE: the source has its own `calcDays` field, but sample records show
# it does NOT equal IssDate - B1_FILE_DD (e.g. filed 2026-01-14, issued
# 2026-07-04, calcDays=0) — its actual meaning wasn't determined, so it's
# deliberately NOT mapped here. Compute approval_days downstream from
# filed_date/issued_date instead of trusting it.
# GAP: no valuation/job-value field exists on this layer at all.
PEORIA_FIELD_MAP = {
    "permit_number": "Permit_Number",
    "permit_type": "Permit_Type",
    "permit_subtype": "B1_PER_SUB_TYPE",
    "status": "Project_Status",
    "description": "Project_Description",
    "project_description": "Project_Name",
    "filed_date": lambda raw: _epoch_ms_to_date(raw.get("B1_FILE_DD")),
    "issued_date": lambda raw: _epoch_ms_to_date(raw.get("IssDate")),
    "expiration_date": lambda raw: _epoch_ms_to_date(raw.get("ExpDate")),
    "finaled_date": lambda raw: _epoch_ms_to_date(raw.get("CmpDate")),
    "job_address": "Project_Address",
    "parcel_number": "APN",
    "general_contractor_name": lambda raw: raw.get("Applicant_Contact_Organization") or raw.get("Applicant_Contact_Name"),
}


def build_peoria_connector() -> ArcGISHubConnector:
    return ArcGISHubConnector(
        jurisdiction_slug="peoria_az",
        service_url="https://gis.peoriaaz.gov/arcgis/rest/services/Accela/Peoria_Building_Permit_All/FeatureServer/3",
        field_map=PEORIA_FIELD_MAP,
        date_field="IssDate",
        city_name="Peoria",
    )


# ------------------------------------------------------------------
# Goodyear, AZ — verified live dataset (Accela Construction_Permits)
# maps.goodyearaz.gov/.../Accela/Construction_Permits/MapServer/0
#
# Nightly Accela sync with individual permit records, contractor name +
# license on nearly every row, valuation, sqft, address, owner.
# ~245 records total; ~145 issued since 2024-01-01 (verified 2026-07-14).
# GAP: smaller/narrower than Mesa/Tempe trade-permit feeds — Accela
# construction/TI-focused scope. PermitDate is a date string; incremental
# TIMESTAMP filters are unreliable on this layer, so date_field is None
# and each run does a full refresh (cheap at this volume).
# ------------------------------------------------------------------
GOODYEAR_FIELD_MAP = {
    "permit_number": "PermitId",
    "permit_type": "JobDescription",
    "status": "StatusDescription",
    "description": "JobDescription",
    "project_description": "ProjectName",
    "issued_date": "PermitDate",
    "expiration_date": "PermitExpirationDate",
    "finaled_date": "CompletionDate",
    "job_address": "FullAddress",
    "city": "JobCity",
    "zip": "JobZipCode",
    "parcel_number": "ParcelId",
    "valuation": lambda raw: raw.get("PermitValue"),
    "square_footage": lambda raw: raw.get("JobSquareFeet"),
    "owner_name": "OwnerName",
    "general_contractor_name": "GenConName",
    "contractor_license_number": "GenConLicenseNumber",
}


def build_goodyear_connector() -> ArcGISHubConnector:
    return ArcGISHubConnector(
        jurisdiction_slug="goodyear_az",
        service_url="https://maps.goodyearaz.gov/server/rest/services/Accela/Construction_Permits/MapServer/0",
        field_map=GOODYEAR_FIELD_MAP,
        date_field=None,  # full refresh; TIMESTAMP where-clause fails on string PermitDate
        city_name="Goodyear",
    )


# ------------------------------------------------------------------
# Phoenix, AZ — verified live dataset (Planning_Permit MapServer)
# maps.phoenix.gov/pub/.../Public/Planning_Permit/MapServer/1
#
# City of Phoenix public Planning/Development "Permits" layer with
# ~71k individual records; issue dates through verification day
# (2026-07-13). PROFESS_NAME carries company/professional name on most
# rows (~35k+ LLC/INC; also OWNER / TO BE BID placeholders).
# GAP: no valuation/job-value or square-footage fields on this layer.
# ------------------------------------------------------------------
def _looks_like_company(value: Optional[str]) -> bool:
    if not value or not str(value).strip():
        return False
    upper = str(value).strip().upper()
    hints = (
        "LLC", "L.L.C", "INC", "CORP", "CO.", "COMPANY", "HOMES", "BUILDERS",
        "BUILDER", "CONSTRUCTION", "CONTRACTING", "CONTRACTORS", "PLUMBING",
        "ELECTRIC", "MECHANICAL", "DEVELOPMENT", "PROPERTIES", "PROPERTY",
        "HOLDINGS",
    )
    return any(h in upper for h in hints)


def _phoenix_description(raw: dict) -> Optional[str]:
    parts = [
        raw.get("PERMIT_NAME"),
        raw.get("SCOPE_DESC"),
        raw.get("MOD_DESC"),
    ]
    joined = " — ".join(p.strip() for p in parts if p and str(p).strip())
    return joined or None


def _phoenix_contractor(raw: dict) -> Optional[str]:
    profess = raw.get("PROFESS_NAME")
    if profess and str(profess).strip():
        return str(profess).strip()
    # When company fields are blank, some rows still carry a firm name in
    # PERMIT_NAME (e.g. retail TI). Use only company-like titles — never
    # invent from generic job text.
    permit_name = raw.get("PERMIT_NAME")
    if _looks_like_company(permit_name):
        return str(permit_name).strip()
    return None


PHOENIX_FIELD_MAP = {
    "permit_number": "PER_NUM",
    "permit_type": "PER_TYPE",
    "permit_subtype": "PER_TYPE_DESC",
    "status": "PERMIT_STAT",
    "description": _phoenix_description,
    "project_description": "PERMIT_NAME",
    "filed_date": lambda raw: _epoch_ms_to_date(raw.get("PER_ENT_DATE")),
    "issued_date": lambda raw: _epoch_ms_to_date(raw.get("PER_ISSUE_DATE")),
    "expiration_date": lambda raw: _epoch_ms_to_date(raw.get("PER_EXPIRE_DATE")),
    "finaled_date": lambda raw: _epoch_ms_to_date(raw.get("PER_COMPL_DATE")),
    "job_address": "STREET_FULL_NAME",
    "general_contractor_name": _phoenix_contractor,
    # Point geometry requested in WGS84 (x = longitude, y = latitude).
    # Out-of-Arizona values are nulled, never corrected.
    "latitude": lambda raw: _valid_az_latitude(_geometry_coord(raw, "y")),
    "longitude": lambda raw: _valid_az_longitude(_geometry_coord(raw, "x")),
}

PHOENIX_SERVICE_URL = "https://maps.phoenix.gov/pub/rest/services/Public/Planning_Permit/MapServer/1"


def build_phoenix_connector() -> ArcGISHubConnector:
    return ArcGISHubConnector(
        jurisdiction_slug="phoenix_az",
        service_url=PHOENIX_SERVICE_URL,
        field_map=PHOENIX_FIELD_MAP,
        date_field="PER_ISSUE_DATE",
        city_name="Phoenix",
        point_geometry_wgs84=True,
    )


# ------------------------------------------------------------------
# Buckeye, AZ — verified live dataset (EnerGov hosted FeatureServer)
# maps.buckeyeaz.gov/.../Hosted/EnergovPermitswReviewHistory2/FeatureServer/0
#
# Public EnergovPermits layer with ~50k individual permits; issue dates
# through verification (2026-07-10). Includes valuation, sqft, apply/issue/
# finalize dates, and trade types (Building/Plumbing/Electrical/Fire).
# ~16k issued since 2024-01-01; ~4k Plumbing - Residential. TIMESTAMP
# filters on issuedate work for incremental ingest.
# GAP: no contractor-identity field on this layer (same class of gap as
# Gilbert). NOTE: many recent rows have blank situs/address fields — still
# ingestable; address is mapped when present, otherwise null.
# ------------------------------------------------------------------
def _buckeye_address(raw: dict) -> Optional[str]:
    for key in ("situsaddress", "addressline1"):
        value = raw.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return None


def _buckeye_city(raw: dict) -> Optional[str]:
    value = raw.get("city")
    if value and str(value).strip():
        return str(value).strip()
    return None


BUCKEYE_FIELD_MAP = {
    "permit_number": "permitnumber",
    "permit_type": "permittype",
    "permit_subtype": "workclass",
    "status": "permitstatus",
    "description": "permitdesc",
    "project_description": "projectname",
    "filed_date": lambda raw: _epoch_ms_to_date(raw.get("applydate")),
    "issued_date": lambda raw: _epoch_ms_to_date(raw.get("issuedate")),
    "expiration_date": lambda raw: _epoch_ms_to_date(raw.get("expiredate")),
    "finaled_date": lambda raw: _epoch_ms_to_date(raw.get("finalizedate")),
    "job_address": _buckeye_address,
    "city": _buckeye_city,
    "state": "state",
    "zip": "postalcode",
    "parcel_number": "parcelnumber",
    "valuation": lambda raw: raw.get("value"),
    "square_footage": lambda raw: raw.get("squarefeet"),
}


def build_buckeye_connector() -> ArcGISHubConnector:
    return ArcGISHubConnector(
        jurisdiction_slug="buckeye_az",
        service_url="https://maps.buckeyeaz.gov/server/rest/services/Hosted/EnergovPermitswReviewHistory2/FeatureServer/0",
        field_map=BUCKEYE_FIELD_MAP,
        date_field="issuedate",
        city_name="Buckeye",
    )
