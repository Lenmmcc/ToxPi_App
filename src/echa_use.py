import html
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

import pandas as pd


REQUIRED_IDENTIFIER_COLUMNS = ["compound", "cas", "ec", "smiles", "echa_id"]

DEFAULT_ECHA_BASE = "https://chem.echa.europa.eu/"
SUBSTANCE_SEARCH_PATH = "api-substance/v1/substance"
SUBSTANCE_DETAIL_PATH = "api-substance/v1/substance/"
DOSSIER_LIST_PATH = "api-dossier-list/v1/dossier"
REGISTRATION_NUMBERS_PATH = "api-dossier-list/v1/dossier.registration.numbers"
HTML_PAGES_PATH = "html-pages-prod/"

TOP_N_DEFAULT = 5
ECHA_ID_RE = re.compile(r"\b\d{3}\.\d{3}\.\d{3}\b")

SUMMARY_COLUMNS = [
    "compound",
    "cas",
    "ec",
    "smiles",
    "input_echa_id",
    "matched_echa_id",
    "matched_name",
    "matched_cas",
    "matched_ec",
    "match_status",
    "query_status",
]

CANDIDATE_COLUMNS = [
    "compound",
    "echa_id",
    "use_cn",
    "use_en",
    "raw_use",
    "echa_use_section",
    "use_phase",
    "evidence_count",
    "source_type",
    "source",
    "dossier_asset_id",
    "registration_number",
    "registration_status",
    "dossier_subtype",
    "registration_role",
    "last_updated_date",
    "record_url",
    "dossier_url",
]

DOSSIER_COLUMNS = [
    "compound",
    "echa_id",
    "asset_external_id",
    "registration_number",
    "registration_status",
    "registration_date",
    "last_updated_date",
    "dossier_subtype",
    "registration_role",
    "dossier_url",
    "parsed_use_count",
]

WARNING_COLUMNS = [
    "compound",
    "cas",
    "ec",
    "smiles",
    "echa_id",
    "stage",
    "message",
]


USE_TRANSLATION_RULES = [
    (("personal care", "cosmetic", "cosmetics", "toiletries", "skin care", "hair care"), "个人护理用品"),
    (("fragrance", "perfume", "scent"), "香精香料"),
    (("detergent", "cleaning", "cleaner", "disinfectant"), "清洁用品"),
    (("pharmaceutical", "medicine", "drug", "therapeutic"), "医药用品"),
    (("chemical intermediate", "intermediate", "article 18"), "化学品中间体"),
    (("plasticizer", "phthalate"), "增塑剂"),
    (("thermoplastic", "plastic", "polymer", "rubber"), "塑料/聚合物制品"),
    (("uv absorber", "ultraviolet absorber", "sunscreen", "light stabilizer"), "紫外线吸收剂"),
    (("pesticide", "insecticide", "herbicide", "fungicide", "biocide"), "农药"),
    (("paint", "coating", "varnish", "ink"), "涂料/油漆"),
    (("adhesive", "sealant", "binder"), "胶黏剂"),
    (("dye", "pigment", "colorant"), "染料/颜料"),
    (("lubricant", "lubricating", "grease"), "润滑剂"),
    (("solvent",), "溶剂"),
    (("surfactant",), "表面活性剂"),
    (("flame retardant", "fire retardant"), "阻燃剂"),
    (("antioxidant",), "抗氧化剂"),
    (("article service life", "service life", "service-life"), "制品使用寿命阶段"),
    (("consumer use", "consumer uses", "consumers"), "消费者用途"),
    (("professional use", "professional workers"), "专业用途"),
    (("industrial use", "industrial sites"), "工业用途"),
    (("formulation", "re-packing", "repacking"), "配制/再包装"),
    (("manufacturing", "manufacture"), "制造"),
]


def make_template_file():
    template_df = pd.DataFrame(
        {
            "compound": ["Diethyl phthalate", "Bisphenol A", "Benzophenone"],
            "cas": ["84-66-2", "80-05-7", "119-61-9"],
            "ec": ["201-550-6", "201-245-8", "204-337-6"],
            "smiles": [
                "CCOC(=O)c1ccccc1C(=O)OCC",
                "CC(C)(c1ccc(O)cc1)c1ccc(O)cc1",
                "O=C(c1ccccc1)c1ccccc1",
            ],
            "echa_id": ["100.001.409", "", ""],
        }
    )
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        template_df.to_excel(writer, sheet_name="ECHA_Input", index=False)
    buffer.seek(0)
    return buffer


def normalize_input_columns(df):
    normalized = df.copy()
    normalized.columns = [str(col).strip() for col in normalized.columns]

    rename_map = {}
    for col in normalized.columns:
        key = _normalize_key(col)
        if key in {
            "compound",
            "name",
            "compoundname",
            "chemical",
            "chemicalname",
            "substance",
            "substancename",
            "化合物",
            "化合物名称",
            "名称",
            "物质名称",
            "污染物",
            "污染物名称",
        }:
            rename_map[col] = "compound"
        elif key in {"cas", "casrn", "casno", "casnumber", "cas号", "cas编号", "cas号码"}:
            rename_map[col] = "cas"
        elif key in {"ec", "ecnumber", "ecno", "ec号", "einecs", "elincs"}:
            rename_map[col] = "ec"
        elif key in {"smiles", "canonicalsmiles", "isomericsmiles", "结构式"}:
            rename_map[col] = "smiles"
        elif key in {"echaid", "echa", "rmlid", "rml", "echa编号", "echa号"}:
            rename_map[col] = "echa_id"

    normalized = normalized.rename(columns=rename_map)
    for col in REQUIRED_IDENTIFIER_COLUMNS:
        if col not in normalized.columns:
            normalized[col] = pd.NA
    return normalized


def validate_input(df):
    available = [col for col in REQUIRED_IDENTIFIER_COLUMNS if col in df.columns]
    if not available:
        return False, "表格至少需要包含 compound、cas、ec、smiles 或 echa_id 中的一列。"

    usable_rows = df[REQUIRED_IDENTIFIER_COLUMNS].notna().any(axis=1).sum()
    if usable_rows == 0:
        return False, "没有可用于 ECHA 查询的化合物标识。"

    return True, f"ECHA 输入数据检查通过，共 {usable_rows} 个可查询化合物。"


def run_echa_use_batch(
    input_df,
    base_url=DEFAULT_ECHA_BASE,
    timeout=90,
    delay_seconds=0.5,
    top_n=TOP_N_DEFAULT,
    max_dossiers=1,
    progress_callback=None,
):
    clean_df = normalize_input_columns(input_df)
    summary_rows = []
    candidate_rows = []
    dossier_rows = []
    warning_rows = []
    total = len(clean_df)

    for pos, (_, row) in enumerate(clean_df.iterrows(), start=1):
        compound = _display_compound(row)
        try:
            resolution = resolve_substance(row, base_url=base_url, timeout=timeout)
            echa_id = resolution.get("echa_id")
            if not _clean_cell(echa_id):
                summary_rows.append(_summary_row(row, resolution, [], top_n, "未匹配到 ECHA 物质"))
                warning_rows.append(_warning_row(row, echa_id, "substance_resolution", resolution.get("message", "")))
            else:
                dossiers, dossier_warnings = fetch_reach_dossiers(
                    echa_id,
                    base_url=base_url,
                    timeout=timeout,
                    max_dossiers=max_dossiers,
                )
                for warning in dossier_warnings:
                    warning_rows.append(_warning_row(row, echa_id, warning["stage"], warning["message"]))

                candidates = []
                for dossier in dossiers:
                    parsed_count_before = len(candidates)
                    try:
                        html_text = fetch_dossier_html(
                            dossier["asset_external_id"],
                            base_url=base_url,
                            timeout=timeout,
                        )
                        candidates.extend(
                            extract_dossier_use_candidates(
                                html_text,
                                dossier,
                                compound=compound,
                                echa_id=echa_id,
                                base_url=base_url,
                            )
                        )
                    except Exception as exc:
                        warning_rows.append(_warning_row(row, echa_id, "dossier_html", str(exc)))
                    parsed_count = len(candidates) - parsed_count_before
                    dossier_rows.append(_dossier_row(row, echa_id, dossier, parsed_count, base_url))

                ranked = rank_use_candidates(candidates, top_n=top_n)
                status = "查询完成" if ranked else "未查到用途数据"
                summary_rows.append(_summary_row(row, resolution, ranked, top_n, status, dossier_count=len(dossiers)))

                for candidate in candidates:
                    candidate_rows.append({"compound": compound, "echa_id": echa_id, **candidate})
        except Exception as exc:
            summary_rows.append(_summary_row(row, {"echa_id": pd.NA, "status": "失败"}, [], top_n, "查询失败"))
            warning_rows.append(_warning_row(row, row.get("echa_id"), "unexpected_error", str(exc)))

        if progress_callback:
            progress_callback(pos, total, compound)
        if delay_seconds and pos < total:
            time.sleep(delay_seconds)

    return (
        _ensure_columns(pd.DataFrame(summary_rows), _summary_columns(top_n)),
        _ensure_columns(pd.DataFrame(candidate_rows), CANDIDATE_COLUMNS),
        _ensure_columns(pd.DataFrame(dossier_rows), DOSSIER_COLUMNS),
        _ensure_columns(pd.DataFrame(warning_rows), WARNING_COLUMNS),
    )


def resolve_substance(row, base_url=DEFAULT_ECHA_BASE, timeout=90):
    provided = _clean_cell(row.get("echa_id"))
    if provided:
        match = ECHA_ID_RE.search(provided)
        if match:
            echa_id = match.group(0)
            return {
                "echa_id": echa_id,
                "matched_name": _clean_cell(row.get("compound")),
                "matched_cas": _clean_cell(row.get("cas")),
                "matched_ec": _clean_cell(row.get("ec")),
                "matched_smiles": _clean_cell(row.get("smiles")),
                "status": "使用输入 ECHA ID",
                "message": "",
            }

    search_terms = [
        ("echa_id", _clean_cell(row.get("echa_id"))),
        ("ec", _clean_cell(row.get("ec"))),
        ("cas", _clean_cell(row.get("cas"))),
        ("compound", _clean_cell(row.get("compound"))),
        ("smiles", _clean_cell(row.get("smiles"))),
    ]
    failures = []
    for term_type, term in search_terms:
        if not term:
            continue
        try:
            candidates = search_substances(term, base_url=base_url, timeout=timeout)
            chosen = _choose_best_substance_match(candidates, term, term_type)
            if chosen:
                return _resolution_from_substance(chosen, f"通过 {term_type} 匹配", "")
        except Exception as exc:
            failures.append(f"{term_type}: {exc}")

    message = "；".join(failures) if failures else "没有可用查询词。"
    return {
        "echa_id": pd.NA,
        "matched_name": pd.NA,
        "matched_cas": pd.NA,
        "matched_ec": pd.NA,
        "matched_smiles": pd.NA,
        "status": "未解析",
        "message": message,
    }


def search_substances(term, base_url=DEFAULT_ECHA_BASE, timeout=90):
    data = _get_json(
        SUBSTANCE_SEARCH_PATH,
        params={"searchText": term, "pageIndex": 1, "pageSize": 20},
        base_url=base_url,
        timeout=timeout,
    )
    candidates = []
    for item in data.get("items", []) if isinstance(data, dict) else []:
        substance = item.get("substanceIndex", item) if isinstance(item, dict) else None
        if substance:
            candidates.append(substance)
    return candidates


def fetch_reach_dossiers(echa_id, base_url=DEFAULT_ECHA_BASE, timeout=90, max_dossiers=1):
    warnings = []
    page_size = max(20, int(max_dossiers or 1))
    params = {
        "rmlId": echa_id,
        "pageIndex": 1,
        "pageSize": page_size,
        "legislation": "REACH",
        "registrationStatuses": "Active",
    }
    try:
        data = _get_json(DOSSIER_LIST_PATH, params=params, base_url=base_url, timeout=timeout)
    except Exception as exc:
        warnings.append({"stage": "dossier_list:active", "message": str(exc)})
        fallback_params = {
            "rmlId": echa_id,
            "pageIndex": 1,
            "pageSize": page_size,
            "legislation": "REACH",
        }
        data = _get_json(DOSSIER_LIST_PATH, params=fallback_params, base_url=base_url, timeout=timeout)

    dossiers = []
    for item in data.get("items", []) if isinstance(data, dict) else []:
        dossier = _flatten_dossier(item)
        if dossier.get("asset_external_id"):
            dossiers.append(dossier)

    if not dossiers:
        return [], warnings

    dossiers = sorted(dossiers, key=_dossier_sort_key, reverse=True)
    return dossiers[: int(max_dossiers or 1)], warnings


def fetch_registration_numbers(echa_id, base_url=DEFAULT_ECHA_BASE, timeout=90):
    data = _get_json(
        REGISTRATION_NUMBERS_PATH,
        params={"rmlId": echa_id, "pageIndex": 1, "pageSize": 100, "legislation": "REACH"},
        base_url=base_url,
        timeout=timeout,
    )
    return data.get("items", []) if isinstance(data, dict) else []


def fetch_dossier_html(asset_external_id, base_url=DEFAULT_ECHA_BASE, timeout=90):
    path = urllib.parse.urljoin(HTML_PAGES_PATH, f"{asset_external_id}/index.html")
    return _get_text(path, base_url=base_url, timeout=timeout)


def extract_dossier_use_candidates(html_text, dossier, compound, echa_id, base_url=DEFAULT_ECHA_BASE):
    nav_html = _sidebar_html(html_text)
    headers = []
    for match in re.finditer(
        r"<button\b(?=[^>]*class=\"[^\"]*das-nav-header)(?P<attrs>[^>]*)>(?P<label>.*?)</button>",
        nav_html,
        flags=re.I | re.S,
    ):
        label = _clean_html_text(match.group("label"))
        if label:
            headers.append((match.start(), label))

    candidates = []
    dossier_url = _dossier_url(dossier.get("asset_external_id"), base_url)
    context_text = " ".join(
        _clean_cell(value)
        for value in (
            dossier.get("dossier_subtype"),
            dossier.get("registration_role"),
        )
        if _clean_cell(value)
    )

    for match in re.finditer(
        r"<a\b(?=[^>]*class=\"[^\"]*das-leaf)(?P<attrs>[^>]*)>(?P<body>.*?)</a>",
        nav_html,
        flags=re.I | re.S,
    ):
        section = _header_before(headers, match.start())
        if not _is_use_section(section):
            continue

        body = match.group("body")
        title = _extract_leaf_title(body)
        if not title:
            continue
        raw_use = _strip_record_prefix(title)
        if not _is_meaningful_use(raw_use):
            continue

        attrs = match.group("attrs")
        href = _extract_attr(attrs, "href")
        record_url = urllib.parse.urljoin(dossier_url, href) if href else dossier_url
        phase = _phase_from_section(section)
        use_cn = classify_use_cn(raw_use, phase, context_text)
        candidates.append(
            {
                "use_cn": use_cn,
                "use_en": raw_use,
                "raw_use": raw_use,
                "echa_use_section": section,
                "use_phase": phase,
                "evidence_count": 1,
                "source_type": "reach_dossier_use",
                "source": "ECHA REACH dossier",
                "dossier_asset_id": dossier.get("asset_external_id", ""),
                "registration_number": dossier.get("registration_number", ""),
                "registration_status": dossier.get("registration_status", ""),
                "dossier_subtype": dossier.get("dossier_subtype", ""),
                "registration_role": dossier.get("registration_role", ""),
                "last_updated_date": dossier.get("last_updated_date", ""),
                "record_url": record_url,
                "dossier_url": dossier_url,
            }
        )

    return candidates


def classify_use_cn(*texts):
    combined = " ".join(_clean_cell(text) for text in texts if _clean_cell(text)).lower()
    for keywords, label in USE_TRANSLATION_RULES:
        if any(keyword in combined for keyword in keywords):
            return label
    return f"其他用途：{_clean_cell(texts[0])}" if texts and _clean_cell(texts[0]) else "未分类"


def rank_use_candidates(candidates, top_n=TOP_N_DEFAULT):
    grouped = {}
    for candidate in candidates:
        label = candidate.get("use_cn") or "未分类"
        key = _normalize_key(label)
        if not key:
            continue
        if key not in grouped:
            grouped[key] = {
                "use_cn": label,
                "evidence_count": 0,
                "raw_uses": set(),
                "sections": set(),
                "sources": set(),
            }
        grouped[key]["evidence_count"] += int(candidate.get("evidence_count") or 1)
        if candidate.get("raw_use"):
            grouped[key]["raw_uses"].add(str(candidate["raw_use"]))
        if candidate.get("echa_use_section"):
            grouped[key]["sections"].add(str(candidate["echa_use_section"]))
        if candidate.get("source"):
            grouped[key]["sources"].add(str(candidate["source"]))

    ranked = sorted(
        grouped.values(),
        key=lambda item: (item["evidence_count"], len(item["raw_uses"]), item["use_cn"]),
        reverse=True,
    )
    output = []
    for rank, item in enumerate(ranked[: int(top_n or TOP_N_DEFAULT)], start=1):
        output.append(
            {
                "rank": rank,
                "use_cn": item["use_cn"],
                "use_en": " | ".join(sorted(item["raw_uses"]))[:1000],
                "evidence_count": item["evidence_count"],
                "sources": "；".join(sorted(item["sources"])),
                "sections": "；".join(sorted(item["sections"])),
            }
        )
    return output


def build_result_workbook(input_df, summary_df=None, candidates_df=None, dossiers_df=None, errors_df=None):
    if summary_df is None:
        summary_df = pd.DataFrame(columns=_summary_columns(TOP_N_DEFAULT))
    if candidates_df is None:
        candidates_df = pd.DataFrame(columns=CANDIDATE_COLUMNS)
    if dossiers_df is None:
        dossiers_df = pd.DataFrame(columns=DOSSIER_COLUMNS)
    if errors_df is None:
        errors_df = pd.DataFrame(columns=WARNING_COLUMNS)

    mapping_df = pd.DataFrame(
        [{"英文关键词": " / ".join(keywords), "中文类别": label} for keywords, label in USE_TRANSLATION_RULES]
    )

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        normalize_input_columns(input_df)[REQUIRED_IDENTIFIER_COLUMNS].to_excel(
            writer, sheet_name="Input", index=False
        )
        summary_df.to_excel(writer, sheet_name="ECHA_Top5_Use_Summary", index=False)
        candidates_df.to_excel(writer, sheet_name="ECHA_All_Use_Candidates", index=False)
        dossiers_df.to_excel(writer, sheet_name="ECHA_Dossiers", index=False)
        errors_df.to_excel(writer, sheet_name="ECHA_Warnings", index=False)
        mapping_df.to_excel(writer, sheet_name="ECHA_CN_Mapping", index=False)
    buffer.seek(0)
    return buffer


def build_empty_summary_template(input_df, top_n=TOP_N_DEFAULT):
    clean_df = normalize_input_columns(input_df)
    return pd.DataFrame(
        [_summary_row(row, {"echa_id": pd.NA, "status": "待查询"}, [], top_n, "待查询") for _, row in clean_df.iterrows()]
    )


def _summary_row(row, resolution, ranked, top_n, status, dossier_count=0):
    output = {
        "compound": _display_compound(row),
        "cas": _clean_cell(row.get("cas")),
        "ec": _clean_cell(row.get("ec")),
        "smiles": _clean_cell(row.get("smiles")),
        "input_echa_id": _clean_cell(row.get("echa_id")),
        "matched_echa_id": resolution.get("echa_id", pd.NA),
        "matched_name": resolution.get("matched_name", pd.NA),
        "matched_cas": resolution.get("matched_cas", pd.NA),
        "matched_ec": resolution.get("matched_ec", pd.NA),
        "match_status": resolution.get("status", pd.NA),
        "query_status": status,
    }
    for idx in range(1, int(top_n or TOP_N_DEFAULT) + 1):
        output[f"用途{idx}"] = pd.NA
        output[f"用途{idx}_英文证据"] = pd.NA
        output[f"用途{idx}_证据数量"] = pd.NA
    for item in ranked[: int(top_n or TOP_N_DEFAULT)]:
        idx = item["rank"]
        output[f"用途{idx}"] = item["use_cn"]
        output[f"用途{idx}_英文证据"] = item["use_en"]
        output[f"用途{idx}_证据数量"] = item["evidence_count"]
    output["前五用途"] = "；".join(item["use_cn"] for item in ranked[: int(top_n or TOP_N_DEFAULT)])
    output["用途来源"] = "；".join(sorted({item["sources"] for item in ranked[: int(top_n or TOP_N_DEFAULT)] if item.get("sources")}))
    output["ECHA_dossier数量"] = dossier_count
    search_query = resolution.get("echa_id")
    if _is_missing(search_query) or not _clean_cell(search_query):
        search_query = _display_compound(row)
    output["ECHA搜索页面"] = _search_url(search_query)
    output["notes"] = resolution.get("message", "")
    return output


def _summary_columns(top_n):
    columns = SUMMARY_COLUMNS.copy()
    for idx in range(1, int(top_n or TOP_N_DEFAULT) + 1):
        columns.extend([f"用途{idx}", f"用途{idx}_英文证据", f"用途{idx}_证据数量"])
    columns.extend(["前五用途", "用途来源", "ECHA_dossier数量", "ECHA搜索页面", "notes"])
    return columns


def _resolution_from_substance(substance, status, message):
    return {
        "echa_id": _get_any(substance, ["rmlId"]),
        "matched_name": _get_any(substance, ["rmlName", "name"]),
        "matched_cas": _first_nonempty(_get_any(substance, ["rmlCas", "casNumber"])),
        "matched_ec": _first_nonempty(_get_any(substance, ["rmlEc", "ecNumber"])),
        "matched_smiles": _first_nonempty(_get_any(substance, ["rmlSmiles", "smiles"])),
        "status": status,
        "message": message,
    }


def _extract_substance_detail(data):
    if not isinstance(data, dict):
        return None
    if "substanceIndex" in data and isinstance(data["substanceIndex"], dict):
        return data["substanceIndex"]
    return data


def _choose_best_substance_match(candidates, term, term_type):
    if not candidates:
        return None
    term_norm = _normalize_key(term)

    def score(candidate):
        value = 0
        echa_id = _normalize_key(_get_any(candidate, ["rmlId"]))
        cas_values = [_normalize_key(v) for v in _as_list(_get_any(candidate, ["rmlCas", "casNumber"]))]
        ec_values = [_normalize_key(v) for v in _as_list(_get_any(candidate, ["rmlEc", "ecNumber"]))]
        name_values = [_normalize_key(v) for v in _as_list(_get_any(candidate, ["rmlName", "iupacName", "ecName"]))]
        smiles_values = [_normalize_key(v) for v in _as_list(_get_any(candidate, ["rmlSmiles", "smiles"]))]

        if term_type == "echa_id" and echa_id == term_norm:
            value += 150
        if term_type == "cas" and term_norm in cas_values:
            value += 120
        if term_type == "ec" and term_norm in ec_values:
            value += 120
        if term_type == "compound" and term_norm in name_values:
            value += 100
        if term_type == "smiles" and term_norm in smiles_values:
            value += 100
        if any(term_norm and term_norm in name for name in name_values):
            value += 20
        if _clean_cell(_get_any(candidate, ["rmlCas"])):
            value += 3
        if _clean_cell(_get_any(candidate, ["rmlEc"])):
            value += 3
        return value

    return sorted(candidates, key=score, reverse=True)[0]


def _flatten_dossier(item):
    info = item.get("reachDossierInfo") if isinstance(item, dict) else {}
    info = info if isinstance(info, dict) else {}
    return {
        "asset_external_id": _clean_cell(item.get("assetExternalId")),
        "root_key": _clean_cell(item.get("rootKey")),
        "rml_id": _clean_cell(item.get("rmlId")),
        "legislation": _clean_cell(item.get("legislation")),
        "registration_number": _clean_cell(item.get("registrationNumber")),
        "registration_status": _clean_cell(item.get("registrationStatus")),
        "registration_date": _clean_cell(item.get("registrationDate")),
        "last_updated_date": _clean_cell(item.get("lastUpdatedDate")),
        "registration_status_changed_date": _clean_cell(item.get("registrationStatusChangedDate")),
        "dossier_subtype": _clean_cell(info.get("dossierSubtype")),
        "registration_role": _clean_cell(info.get("registrationRole")),
        "details": _clean_cell(info.get("details")),
    }


def _dossier_sort_key(dossier):
    subtype = _clean_cell(dossier.get("dossier_subtype")).lower()
    role = _clean_cell(dossier.get("registration_role")).lower()
    status = _clean_cell(dossier.get("registration_status")).lower()
    date = _clean_cell(dossier.get("last_updated_date"))
    return (
        1 if status == "active" else 0,
        1 if "article 10" in subtype else 0,
        1 if "lead" in role else 0,
        date,
    )


def _dossier_row(row, echa_id, dossier, parsed_use_count, base_url):
    return {
        "compound": _display_compound(row),
        "echa_id": echa_id,
        "asset_external_id": dossier.get("asset_external_id", ""),
        "registration_number": dossier.get("registration_number", ""),
        "registration_status": dossier.get("registration_status", ""),
        "registration_date": dossier.get("registration_date", ""),
        "last_updated_date": dossier.get("last_updated_date", ""),
        "dossier_subtype": dossier.get("dossier_subtype", ""),
        "registration_role": dossier.get("registration_role", ""),
        "dossier_url": _dossier_url(dossier.get("asset_external_id"), base_url),
        "parsed_use_count": parsed_use_count,
    }


def _warning_row(row, echa_id, stage, message):
    return {
        "compound": _display_compound(row),
        "cas": _clean_cell(row.get("cas")),
        "ec": _clean_cell(row.get("ec")),
        "smiles": _clean_cell(row.get("smiles")),
        "echa_id": _clean_cell(echa_id),
        "stage": stage,
        "message": message,
    }


def _get_json(path, params=None, base_url=DEFAULT_ECHA_BASE, timeout=90):
    text = _get_text(path, params=params, base_url=base_url, timeout=timeout)
    return json.loads(text)


def _get_text(path, params=None, base_url=DEFAULT_ECHA_BASE, timeout=90):
    url = _build_url(path, params=params, base_url=base_url)
    headers = {
        "Accept": "application/json, text/html, */*",
        "User-Agent": "ToxApp ECHA use-query module",
    }
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连接失败: {exc.reason}") from exc
    return raw.decode("utf-8", errors="replace")


def _build_url(path, params=None, base_url=DEFAULT_ECHA_BASE):
    base = base_url if base_url.endswith("/") else base_url + "/"
    url = path if str(path).startswith(("http://", "https://")) else urllib.parse.urljoin(base, path)
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    return url


def _dossier_url(asset_external_id, base_url=DEFAULT_ECHA_BASE):
    if not asset_external_id:
        return ""
    return _build_url(urllib.parse.urljoin(HTML_PAGES_PATH, f"{asset_external_id}/index.html"), base_url=base_url)


def _search_url(query):
    return _build_url("", params={"searchText": _clean_cell(query)}, base_url=DEFAULT_ECHA_BASE)


def _header_before(headers, position):
    current = ""
    for start, label in headers:
        if start > position:
            break
        current = label
    return current


def _is_use_section(section):
    text = _clean_cell(section).lower()
    return bool(re.match(r"^\s*3\.5(?:\.\d+)?\b", text))


def _phase_from_section(section):
    text = re.sub(r"^\s*3\.5(?:\.\d+)?\s*", "", _clean_cell(section))
    return re.sub(r"\s+", " ", text).strip()


def _extract_leaf_title(body):
    span_titles = re.findall(r"<span\b[^>]*data-dastttxt=\"([^\"]+)\"", body, flags=re.I | re.S)
    candidates = [
        _clean_html_text(title)
        for title in span_titles
        if _clean_html_text(title).lower() not in {"flexible record", "endpoint summary"}
    ]
    if candidates:
        return max(candidates, key=len)
    text = _clean_html_text(body)
    return text


def _strip_record_prefix(title):
    return re.sub(r"^\s*\d+\s*\|\s*", "", _clean_cell(title)).strip()


def _is_meaningful_use(raw_use):
    text = _clean_cell(raw_use).lower()
    if not text:
        return False
    if text.startswith("no specified"):
        return False
    return text not in {"-", "no data", "not applicable", "not specified"}


def _sidebar_html(html_text):
    match = re.search(
        r"<aside\b[^>]*id=\"das-dossier-sidebar\"[^>]*>(?P<body>.*?)</aside>",
        html_text,
        flags=re.I | re.S,
    )
    return match.group("body") if match else html_text


def _extract_attr(attrs, name):
    match = re.search(rf"\b{name}\s*=\s*\"([^\"]*)\"", attrs, flags=re.I)
    return html.unescape(match.group(1)) if match else ""


def _clean_html_text(text):
    cleaned = re.sub(r"<[^>]+>", " ", str(text))
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _ensure_columns(df, columns):
    for col in columns:
        if col not in df.columns:
            df[col] = pd.NA
    return df[columns]


def _display_compound(row):
    for key in ("compound", "cas", "ec", "echa_id", "smiles"):
        value = _clean_cell(row.get(key))
        if value:
            return value
    return "未命名化合物"


def _get_any(record, names, default=pd.NA):
    if not isinstance(record, dict):
        return default
    key_map = {_normalize_key(key): value for key, value in record.items()}
    for name in names:
        value = key_map.get(_normalize_key(name), default)
        if value is not default and not _is_missing(value):
            return value
    return default


def _first_nonempty(value):
    for item in _as_list(value):
        text = _clean_cell(item)
        if text and text != "-":
            return text
    return ""


def _as_list(value):
    if _is_missing(value):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


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


def _normalize_key(value):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value).strip().lower())
