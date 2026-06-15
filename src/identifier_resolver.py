import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

from src.comptox_use import (
    DEFAULT_API_BASE as COMPTOX_DEFAULT_API_BASE,
    normalize_input_columns as normalize_comptox_input_columns,
    resolve_dtxsid,
)
from src.echa_use import (
    DEFAULT_ECHA_BASE,
    normalize_input_columns as normalize_echa_input_columns,
    resolve_substance,
)


REQUIRED_IDENTIFIER_COLUMNS = ["compound", "smiles", "cas", "ec", "dtxsid", "echa_id"]
DEFAULT_PUBCHEM_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/"
CAS_RE = re.compile(r"\b\d{2,7}-\d{2}-\d\b")
EC_RE = re.compile(r"\b\d{3}-\d{3}-\d\b")
DTXSID_RE = re.compile(r"\bDTXSID\d+\b", re.I)

RESOLVED_COLUMNS = [
    "compound",
    "smiles",
    "pubchem_cid",
    "cas",
    "ec",
    "dtxsid",
    "echa_id",
    "resolved_name",
    "pubchem_match_status",
    "epa_match_status",
    "echa_match_status",
    "completion_status",
    "notes",
]

WARNING_COLUMNS = [
    "compound",
    "smiles",
    "cas",
    "ec",
    "dtxsid",
    "echa_id",
    "stage",
    "message",
]


def make_template_file():
    template_df = pd.DataFrame(
        {
            "compound": ["Diethyl phthalate", "Bisphenol A", "Benzophenone"],
            "smiles": [
                "CCOC(=O)c1ccccc1C(=O)OCC",
                "CC(C)(c1ccc(O)cc1)c1ccc(O)cc1",
                "O=C(c1ccccc1)c1ccccc1",
            ],
            "cas": ["", "", ""],
            "ec": ["", "", ""],
            "dtxsid": ["", "", ""],
            "echa_id": ["", "", ""],
        }
    )
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        template_df.to_excel(writer, sheet_name="Identifier_Input", index=False)
    buffer.seek(0)
    return buffer


def normalize_input_columns(df):
    comptox_df = normalize_comptox_input_columns(df)
    echa_df = normalize_echa_input_columns(df)
    normalized = pd.DataFrame(index=df.index)
    normalized["compound"] = _first_existing_series(comptox_df, echa_df, "compound")
    normalized["smiles"] = _first_existing_series(comptox_df, echa_df, "smiles")
    normalized["cas"] = _first_existing_series(comptox_df, echa_df, "cas")
    normalized["ec"] = echa_df["ec"] if "ec" in echa_df.columns else pd.NA
    normalized["dtxsid"] = comptox_df["dtxsid"] if "dtxsid" in comptox_df.columns else pd.NA
    normalized["echa_id"] = echa_df["echa_id"] if "echa_id" in echa_df.columns else pd.NA
    return normalized[REQUIRED_IDENTIFIER_COLUMNS]


def validate_input(df):
    available = [col for col in REQUIRED_IDENTIFIER_COLUMNS if col in df.columns]
    if not available:
        return False, "表格至少需要包含 compound、smiles、cas、ec、dtxsid 或 echa_id 中的一列。"

    usable_rows = df[REQUIRED_IDENTIFIER_COLUMNS].notna().any(axis=1).sum()
    if usable_rows == 0:
        return False, "没有可用于补全的化合物标识。"

    return True, f"标识符补全输入检查通过，共 {usable_rows} 个可处理化合物。"


def run_identifier_completion_batch(
    input_df,
    comptox_api_base=COMPTOX_DEFAULT_API_BASE,
    comptox_api_key=None,
    echa_base=DEFAULT_ECHA_BASE,
    use_epa=True,
    use_echa=True,
    use_pubchem=True,
    pubchem_base=DEFAULT_PUBCHEM_BASE,
    timeout=60,
    delay_seconds=0.2,
    progress_callback=None,
):
    clean_df = normalize_input_columns(input_df)
    completed_rows = []
    warning_rows = []
    total = len(clean_df)

    for pos, (_, row) in enumerate(clean_df.iterrows(), start=1):
        working = _row_dict(row)
        notes = []
        pubchem_status = ""
        epa_status = ""
        echa_status = ""

        if use_pubchem and working["smiles"]:
            try:
                pubchem_resolution = resolve_pubchem_by_smiles(
                    working["smiles"],
                    base_url=pubchem_base,
                    timeout=timeout,
                )
                pubchem_status = _clean_cell(pubchem_resolution.get("status"))
                _update_if_empty(working, "pubchem_cid", pubchem_resolution.get("pubchem_cid"))
                _update_if_empty(working, "cas", pubchem_resolution.get("cas"))
                _update_if_empty(working, "ec", pubchem_resolution.get("ec"))
                _update_if_empty(working, "dtxsid", pubchem_resolution.get("dtxsid"))
                _update_if_empty(working, "resolved_name", pubchem_resolution.get("preferred_name"))
                pubchem_message = _clean_cell(pubchem_resolution.get("message"))
                notes.append(pubchem_message)
                if not _clean_cell(pubchem_resolution.get("pubchem_cid")) and pubchem_message:
                    warning_rows.append(_warning_row(row, "pubchem_resolution", pubchem_message))
            except Exception as exc:
                pubchem_status = "PubChem 补全失败"
                warning_rows.append(_warning_row(row, "pubchem_resolution", str(exc)))

        if use_epa:
            try:
                epa_resolution = resolve_dtxsid(
                    pd.Series(
                        {
                            "compound": working["compound"],
                            "cas": working["cas"],
                            "smiles": working["smiles"],
                            "dtxsid": working["dtxsid"],
                        }
                    ),
                    api_base=comptox_api_base,
                    api_key=comptox_api_key,
                    timeout=timeout,
                )
                epa_status = _clean_cell(epa_resolution.get("status"))
                _update_if_empty(working, "dtxsid", epa_resolution.get("dtxsid"))
                _update_if_empty(working, "cas", epa_resolution.get("matched_cas"))
                _update_if_empty(working, "resolved_name", epa_resolution.get("matched_name"))
                epa_message = _clean_cell(epa_resolution.get("message"))
                notes.append(epa_message)
                if not _clean_cell(epa_resolution.get("dtxsid")) and epa_message:
                    warning_rows.append(_warning_row(row, "epa_resolution", epa_message))
            except Exception as exc:
                epa_status = "EPA 补全失败"
                warning_rows.append(_warning_row(row, "epa_resolution", str(exc)))

        if use_echa:
            try:
                echa_query = _best_echa_query(working)
                echa_resolution = resolve_substance(
                    pd.Series(echa_query),
                    base_url=echa_base,
                    timeout=timeout,
                )
                echa_status = _clean_cell(echa_resolution.get("status"))
                _update_if_empty(working, "echa_id", echa_resolution.get("echa_id"))
                _update_if_empty(working, "ec", echa_resolution.get("matched_ec"))
                _update_if_empty(working, "cas", echa_resolution.get("matched_cas"))
                _update_if_empty(working, "resolved_name", echa_resolution.get("matched_name"))
                echa_message = _clean_cell(echa_resolution.get("message"))
                notes.append(echa_message)
                if not _clean_cell(echa_resolution.get("echa_id")) and echa_message:
                    warning_rows.append(_warning_row(row, "echa_resolution", echa_message))
            except Exception as exc:
                echa_status = "ECHA 补全失败"
                warning_rows.append(_warning_row(row, "echa_resolution", str(exc)))

        completed_rows.append(
            {
                "compound": working["compound"] or working["resolved_name"],
                "smiles": working["smiles"],
                "pubchem_cid": working["pubchem_cid"],
                "cas": working["cas"],
                "ec": working["ec"],
                "dtxsid": working["dtxsid"],
                "echa_id": working["echa_id"],
                "resolved_name": working["resolved_name"],
                "pubchem_match_status": pubchem_status,
                "epa_match_status": epa_status,
                "echa_match_status": echa_status,
                "completion_status": _completion_status(working),
                "notes": "；".join(note for note in notes if note),
            }
        )

        if progress_callback:
            progress_callback(pos, total, _display_compound(row))
        if delay_seconds and pos < total:
            time.sleep(delay_seconds)

    return (
        _ensure_columns(pd.DataFrame(completed_rows), RESOLVED_COLUMNS),
        _ensure_columns(pd.DataFrame(warning_rows), WARNING_COLUMNS),
    )


def build_result_workbook(input_df, completed_df=None, warnings_df=None):
    if completed_df is None:
        completed_df = pd.DataFrame(columns=RESOLVED_COLUMNS)
    if warnings_df is None:
        warnings_df = pd.DataFrame(columns=WARNING_COLUMNS)

    guide_df = pd.DataFrame(
        [
            {"字段": "compound", "说明": "输入或补全得到的化合物名称。"},
            {"字段": "smiles", "说明": "输入 SMILES；本模块不会改写结构式。"},
            {"字段": "pubchem_cid", "说明": "PubChem Compound ID，主要用于纯 SMILES 的中间匹配。"},
            {"字段": "cas", "说明": "CAS Registry Number，优先来自输入，其次来自 EPA 或 ECHA 匹配。"},
            {"字段": "ec", "说明": "ECHA/欧盟 EC 号，主要来自 ECHA 匹配。"},
            {"字段": "dtxsid", "说明": "EPA CompTox 使用的 DSSTox Substance ID。"},
            {"字段": "echa_id", "说明": "ECHA CHEM 使用的 ECHA ID / RML ID。"},
            {"字段": "completion_status", "说明": "已补全、部分补全或未补全。"},
        ]
    )

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        normalize_input_columns(input_df).to_excel(writer, sheet_name="Input", index=False)
        completed_df.to_excel(writer, sheet_name="Completed_Identifiers", index=False)
        warnings_df.to_excel(writer, sheet_name="Resolver_Warnings", index=False)
        guide_df.to_excel(writer, sheet_name="Field_Guide", index=False)
    buffer.seek(0)
    return buffer


def build_empty_completed_template(input_df):
    clean_df = normalize_input_columns(input_df)
    rows = []
    for _, row in clean_df.iterrows():
        working = _row_dict(row)
        rows.append(
            {
                "compound": working["compound"],
                "smiles": working["smiles"],
                "pubchem_cid": working["pubchem_cid"],
                "cas": working["cas"],
                "ec": working["ec"],
                "dtxsid": working["dtxsid"],
                "echa_id": working["echa_id"],
                "resolved_name": "",
                "pubchem_match_status": "待补全",
                "epa_match_status": "待补全",
                "echa_match_status": "待补全",
                "completion_status": "待补全",
                "notes": "",
            }
        )
    return _ensure_columns(pd.DataFrame(rows), RESOLVED_COLUMNS)


def _first_existing_series(primary_df, secondary_df, column):
    if column in primary_df.columns:
        return primary_df[column]
    if column in secondary_df.columns:
        return secondary_df[column]
    return pd.Series([pd.NA] * len(primary_df), index=primary_df.index)


def _row_dict(row):
    return {
        "compound": _clean_cell(row.get("compound")),
        "smiles": _clean_cell(row.get("smiles")),
        "pubchem_cid": "",
        "cas": _clean_cell(row.get("cas")),
        "ec": _clean_cell(row.get("ec")),
        "dtxsid": _clean_cell(row.get("dtxsid")),
        "echa_id": _clean_cell(row.get("echa_id")),
        "resolved_name": "",
    }


def _best_echa_query(working):
    query = {"compound": "", "cas": "", "ec": "", "smiles": "", "echa_id": ""}
    if working["echa_id"]:
        query["echa_id"] = working["echa_id"]
    elif working["ec"]:
        query["ec"] = working["ec"]
    elif working["cas"]:
        query["cas"] = working["cas"]
    elif working["resolved_name"] or working["compound"]:
        query["compound"] = working["resolved_name"] or working["compound"]
    else:
        query["smiles"] = working["smiles"]
    return query


def resolve_pubchem_by_smiles(smiles, base_url=DEFAULT_PUBCHEM_BASE, timeout=60):
    smiles = _clean_cell(smiles)
    if not smiles:
        return {"status": "未提供 SMILES", "message": ""}

    cid_data = _pubchem_get_json(
        "compound/smiles/cids/JSON",
        params={"smiles": smiles},
        base_url=base_url,
        timeout=timeout,
    )
    cids = (
        cid_data.get("IdentifierList", {}).get("CID", [])
        if isinstance(cid_data, dict)
        else []
    )
    if not cids:
        return {"status": "PubChem 未匹配", "message": "PubChem 未返回 CID。"}

    cid = str(cids[0])
    properties = {}
    try:
        prop_data = _pubchem_get_json(
            f"compound/cid/{urllib.parse.quote(cid, safe='')}/property/IUPACName,CanonicalSMILES/JSON",
            base_url=base_url,
            timeout=timeout,
        )
        records = prop_data.get("PropertyTable", {}).get("Properties", [])
        if records:
            properties = records[0]
    except Exception:
        properties = {}

    synonyms = []
    try:
        synonym_data = _pubchem_get_json(
            f"compound/cid/{urllib.parse.quote(cid, safe='')}/synonyms/JSON",
            base_url=base_url,
            timeout=timeout,
        )
        records = synonym_data.get("InformationList", {}).get("Information", [])
        if records:
            synonyms = records[0].get("Synonym", []) or []
    except Exception:
        synonyms = []

    preferred_name = _first_text(synonyms) or _clean_cell(properties.get("IUPACName"))
    cas = _first_regex_match(synonyms, CAS_RE)
    ec = _first_regex_match(synonyms, EC_RE)
    dtxsid = _first_regex_match(synonyms, DTXSID_RE)

    return {
        "pubchem_cid": cid,
        "preferred_name": preferred_name,
        "cas": cas,
        "ec": ec,
        "dtxsid": dtxsid.upper() if dtxsid else "",
        "status": "通过 PubChem SMILES 匹配",
        "message": "",
    }


def _pubchem_get_json(path, params=None, base_url=DEFAULT_PUBCHEM_BASE, timeout=60):
    base = base_url if base_url.endswith("/") else base_url + "/"
    url = urllib.parse.urljoin(base, path)
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "ToxApp identifier resolver",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连接失败: {exc.reason}") from exc
    return json.loads(raw.decode("utf-8", errors="replace"))


def _first_regex_match(values, pattern):
    for value in values or []:
        match = pattern.search(str(value))
        if match:
            return match.group(0)
    return ""


def _first_text(values):
    for value in values or []:
        text = _clean_cell(value)
        if text:
            return text
    return ""


def _update_if_empty(target, key, value):
    text = _clean_cell(value)
    if text and not _clean_cell(target.get(key)):
        target[key] = text


def _completion_status(working):
    ids = ["cas", "ec", "dtxsid", "echa_id"]
    found = sum(1 for key in ids if _clean_cell(working.get(key)))
    if found >= 3:
        return "已补全"
    if found >= 1:
        return "部分补全"
    return "未补全"


def _warning_row(row, stage, message):
    return {
        "compound": _clean_cell(row.get("compound")),
        "smiles": _clean_cell(row.get("smiles")),
        "cas": _clean_cell(row.get("cas")),
        "ec": _clean_cell(row.get("ec")),
        "dtxsid": _clean_cell(row.get("dtxsid")),
        "echa_id": _clean_cell(row.get("echa_id")),
        "stage": stage,
        "message": message,
    }


def _display_compound(row):
    for key in ("compound", "cas", "ec", "dtxsid", "echa_id", "smiles"):
        value = _clean_cell(row.get(key))
        if value:
            return value
    return "未命名化合物"


def _ensure_columns(df, columns):
    for col in columns:
        if col not in df.columns:
            df[col] = pd.NA
    return df[columns]


def _clean_cell(value):
    if _is_missing(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "<na>"} else text


def _is_missing(value):
    if value is None:
        return True
    if isinstance(value, (list, dict, tuple, set)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False
