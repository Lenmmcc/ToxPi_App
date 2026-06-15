import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

import pandas as pd


REQUIRED_IDENTIFIER_COLUMNS = ["compound", "cas", "smiles", "dtxsid"]

DEFAULT_API_BASE = "https://api-ccte.epa.gov/"
DEFAULT_DASHBOARD_BASE = "https://comptox.epa.gov/dashboard/"

# This key is published in the CompTox Dashboard frontend bundle. Deployments can
# override it with COMPTOX_API_KEY if EPA changes access requirements.
DEFAULT_COMPTOX_API_KEY = os.environ.get(
    "COMPTOX_API_KEY", "546aa80b-f05c-4126-a303-18dd66eabb9d"
)

PRODUCT_USE_ENDPOINT = "ccdapp2/product-cat-puc/search/by-dtxsid"
PRODUCT_KEYWORD_ENDPOINT = "ccdapp2/product-cat-keyword/search/by-dtxsid"
FUNCTIONAL_USE_ENDPOINT = "ccdapp2/exposure-chemical-func-use/search/by-dtxsid"
CHEMICAL_SEARCH_ENDPOINT = "ccdapp1/search/chemical/equal-with-detail/"

TOP_N_DEFAULT = 5
DTXSID_RE = re.compile(r"\bDTXSID\d+\b", re.I)

USE_SOURCE_LABELS = {
    "product_category": "产品用途类别",
    "product_keyword": "产品用途关键词",
    "functional_use": "化学功能用途",
}

USE_TRANSLATION_RULES = [
    (("personal care", "cosmetic", "beauty", "skin care", "hair care", "toiletries"), "个人护理用品"),
    (("chemical intermediate", "intermediate", "intermediates"), "化学品中间体"),
    (("plasticizer", "phthalate"), "增塑剂"),
    (("uv absorber", "ultraviolet absorber", "sunscreen", "light stabilizer"), "紫外线吸收剂"),
    (("pesticide", "insecticide", "herbicide", "fungicide", "biocide"), "农药"),
    (("polycyclic aromatic", "polycyclic aromatic hydrocarbon", "pah"), "多环芳烃及其类似物"),
    (("pharmaceutical", "medicine", "drug", "therapeutic"), "医药用品"),
    (("fragrance", "flavor", "perfume", "scent"), "香精香料"),
    (("antioxidant",), "抗氧化剂"),
    (("hardener", "curing agent"), "固化剂"),
    (("processing aid",), "加工助剂"),
    (("additive",), "添加剂"),
    (("flame retardant", "fire retardant"), "阻燃剂"),
    (("solvent",), "溶剂"),
    (("surfactant", "detergent"), "表面活性剂"),
    (("lubricating", "lubricant"), "润滑剂"),
    (("adhesive", "sealant", "binder"), "胶黏剂"),
    (("dye", "pigment", "colorant"), "染料/颜料"),
    (("cleaning", "cleaner", "disinfectant"), "清洁用品"),
    (("industrial product",), "工业用品"),
    (("construction", "building material"), "建筑材料"),
    (("paint", "coating", "stain"), "涂料/油漆"),
    (("medical", "dental"), "医疗/牙科用品"),
    (("furniture", "furnishing"), "家具用品"),
    (("food", "beverage"), "食品相关"),
    (("home maintenance", "household"), "家庭维护用品"),
    (("auto", "automotive"), "汽车用品"),
    (("arts", "crafts", "office"), "文具/办公用品"),
    (("monomer", "polymer"), "聚合物相关原料"),
    (("catalyst",), "催化剂"),
    (("hydraulic fluid",), "液压流体"),
]

GENERIC_USE_EXACT = {
    "not yet categorized",
    "not categorized",
    "uncategorized",
    "unknown",
    "raw materials",
}

GENERIC_USE_PATTERNS = (
    "not yet categorized",
    "not categorized",
    "uncategorized",
    "no data",
    "not specified",
)


def make_template_file():
    template_df = pd.DataFrame(
        {
            "compound": ["Bisphenol A", "Benzophenone", "Diphenylamine"],
            "cas": ["80-05-7", "119-61-9", "122-39-4"],
            "smiles": [
                "CC(C)(c1ccc(O)cc1)c1ccc(O)cc1",
                "O=C(c1ccccc1)c1ccccc1",
                "c1ccc(Nc2ccccc2)cc1",
            ],
            "dtxsid": ["DTXSID7020182", "DTXSID0021961", "DTXSID4021975"],
        }
    )
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        template_df.to_excel(writer, sheet_name="CompTox_Input", index=False)
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
        elif key in {"smiles", "canonicalsmiles", "isomericsmiles", "结构式"}:
            rename_map[col] = "smiles"
        elif key in {"dtxsid", "dsstoxsubstanceid", "comptoxid", "comptoxid"}:
            rename_map[col] = "dtxsid"

    normalized = normalized.rename(columns=rename_map)
    for col in REQUIRED_IDENTIFIER_COLUMNS:
        if col not in normalized.columns:
            normalized[col] = pd.NA
    return normalized


def validate_input(df):
    available = [col for col in REQUIRED_IDENTIFIER_COLUMNS if col in df.columns]
    if not available:
        return False, "表格至少需要包含 compound、cas、smiles 或 dtxsid 中的一列。"

    usable_rows = df[REQUIRED_IDENTIFIER_COLUMNS].notna().any(axis=1).sum()
    if usable_rows == 0:
        return False, "没有可用于查询的化合物标识。"

    return True, f"输入数据检查通过，共 {usable_rows} 个可查询化合物。"


def run_comptox_use_batch(
    input_df,
    api_base=DEFAULT_API_BASE,
    api_key=None,
    timeout=45,
    delay_seconds=0.2,
    top_n=TOP_N_DEFAULT,
    dashboard_fallback=True,
    progress_callback=None,
):
    clean_df = normalize_input_columns(input_df)
    summary_rows = []
    candidate_rows = []
    error_rows = []
    total = len(clean_df)

    for pos, (_, row) in enumerate(clean_df.iterrows(), start=1):
        compound = _display_compound(row)
        try:
            resolution = resolve_dtxsid(
                row,
                api_base=api_base,
                api_key=api_key,
                timeout=timeout,
            )
            dtxsid = resolution.get("dtxsid")
            if _is_missing(dtxsid) or not _clean_cell(dtxsid):
                summary_rows.append(
                    _summary_row(row, resolution, [], top_n, "未解析到 DTXSID")
                )
                error_rows.append(
                    {
                        "compound": compound,
                        "cas": _clean_cell(row.get("cas")),
                        "smiles": _clean_cell(row.get("smiles")),
                        "dtxsid": _clean_cell(row.get("dtxsid")),
                        "stage": "identifier_resolution",
                        "message": resolution.get("message", "CompTox 未返回可用 DTXSID。"),
                    }
                )
            else:
                candidates, warnings = fetch_use_candidates(
                    dtxsid,
                    api_base=api_base,
                    api_key=api_key,
                    timeout=timeout,
                    dashboard_fallback=dashboard_fallback,
                )
                ranked = rank_use_candidates(candidates, top_n=top_n)
                status = "查询完成" if ranked else "未查到用途数据"
                summary_rows.append(_summary_row(row, resolution, ranked, top_n, status))

                for candidate in candidates:
                    candidate_row = {
                        "compound": compound,
                        "dtxsid": dtxsid,
                        **candidate,
                    }
                    candidate_rows.append(candidate_row)

                for warning in warnings:
                    error_rows.append(
                        {
                            "compound": compound,
                            "cas": _clean_cell(row.get("cas")),
                            "smiles": _clean_cell(row.get("smiles")),
                            "dtxsid": dtxsid,
                            "stage": warning.get("stage", "use_query"),
                            "message": warning.get("message", ""),
                        }
                    )
        except Exception as exc:
            summary_rows.append(
                _summary_row(row, {"dtxsid": pd.NA, "status": "失败"}, [], top_n, "查询失败")
            )
            error_rows.append(
                {
                    "compound": compound,
                    "cas": _clean_cell(row.get("cas")),
                    "smiles": _clean_cell(row.get("smiles")),
                    "dtxsid": _clean_cell(row.get("dtxsid")),
                    "stage": "unexpected_error",
                    "message": str(exc),
                }
            )

        if progress_callback:
            progress_callback(pos, total, compound)
        if delay_seconds and pos < total:
            time.sleep(delay_seconds)

    summary_df = pd.DataFrame(summary_rows)
    candidates_df = pd.DataFrame(candidate_rows)
    errors_df = pd.DataFrame(error_rows)
    return summary_df, candidates_df, errors_df


def resolve_dtxsid(row, api_base=DEFAULT_API_BASE, api_key=None, timeout=45):
    provided = _clean_cell(row.get("dtxsid"))
    if provided:
        match = DTXSID_RE.search(provided)
        if match:
            return {
                "dtxsid": match.group(0).upper(),
                "matched_name": _clean_cell(row.get("compound")),
                "matched_cas": _clean_cell(row.get("cas")),
                "status": "使用输入 DTXSID",
                "message": "",
            }

    for value in (row.get("compound"), row.get("cas"), row.get("smiles")):
        text = _clean_cell(value)
        if text:
            match = DTXSID_RE.search(text)
            if match:
                return {
                    "dtxsid": match.group(0).upper(),
                    "matched_name": _clean_cell(row.get("compound")),
                    "matched_cas": _clean_cell(row.get("cas")),
                    "status": "从输入文本识别 DTXSID",
                    "message": "",
                }

    search_terms = [
        ("cas", _clean_cell(row.get("cas"))),
        ("compound", _clean_cell(row.get("compound"))),
        ("smiles", _clean_cell(row.get("smiles"))),
    ]
    failures = []
    for term_type, term in search_terms:
        if not term:
            continue
        try:
            data = _api_get_json(
                CHEMICAL_SEARCH_ENDPOINT + urllib.parse.quote(term, safe=""),
                api_base=api_base,
                api_key=api_key,
                timeout=timeout,
            )
            candidates = _extract_chemical_candidates(data)
            chosen = _choose_best_identifier_match(candidates, term, term_type)
            if chosen:
                return {
                    "dtxsid": _get_any(chosen, ["dtxsid", "dsstoxSubstanceId"]),
                    "matched_name": _get_any(chosen, ["preferredName", "name", "label"]),
                    "matched_cas": _get_any(chosen, ["casrn", "cas", "casNumber"]),
                    "status": f"通过 {term_type} 匹配",
                    "message": "",
                }
        except Exception as exc:
            failures.append(f"{term_type}: {exc}")

        try:
            candidates = _dashboard_search_chemical_candidates(term, timeout=timeout)
            chosen = _choose_best_identifier_match(candidates, term, term_type)
            if chosen:
                return {
                    "dtxsid": _get_any(chosen, ["dtxsid", "dsstoxSubstanceId"]),
                    "matched_name": _get_any(chosen, ["preferredName", "name", "label"]),
                    "matched_cas": _get_any(chosen, ["casrn", "cas", "casNumber"]),
                    "status": f"通过 Dashboard {term_type} 匹配",
                    "message": "",
                }
        except Exception as exc:
            failures.append(f"dashboard {term_type}: {exc}")

    message = "；".join(failures) if failures else "没有可用查询词。"
    return {
        "dtxsid": pd.NA,
        "matched_name": pd.NA,
        "matched_cas": pd.NA,
        "status": "未解析",
        "message": message,
    }


def fetch_use_candidates(
    dtxsid,
    api_base=DEFAULT_API_BASE,
    api_key=None,
    timeout=45,
    dashboard_fallback=True,
):
    candidates = []
    warnings = []

    api_calls = [
        (
            "product_category",
            PRODUCT_USE_ENDPOINT,
            _extract_product_category_candidates,
        ),
        (
            "product_keyword",
            PRODUCT_KEYWORD_ENDPOINT,
            _extract_product_keyword_candidates,
        ),
        (
            "functional_use",
            FUNCTIONAL_USE_ENDPOINT,
            _extract_functional_use_candidates,
        ),
    ]

    for source_type, endpoint, extractor in api_calls:
        try:
            data = _api_get_json(
                endpoint,
                params={"id": dtxsid},
                api_base=api_base,
                api_key=api_key,
                timeout=timeout,
            )
            candidates.extend(extractor(data, source=f"api:{source_type}"))
        except Exception as exc:
            warnings.append(
                {
                    "stage": f"api:{source_type}",
                    "message": str(exc),
                }
            )

    if dashboard_fallback:
        if not any(item["source_type"] == "product_category" for item in candidates):
            try:
                html = _dashboard_get_html(
                    f"chemical/product-use-categories/{dtxsid}",
                    timeout=timeout,
                )
                candidates.extend(_extract_dashboard_product_categories(html))
            except Exception as exc:
                warnings.append(
                    {
                        "stage": "dashboard:product_category",
                        "message": str(exc),
                    }
                )

        if not any(item["source_type"] == "functional_use" for item in candidates):
            try:
                html = _dashboard_get_html(
                    f"chemical/chemical-functional-use/{dtxsid}",
                    timeout=timeout,
                )
                candidates.extend(_extract_dashboard_functional_uses(html))
            except Exception as exc:
                warnings.append(
                    {
                        "stage": "dashboard:functional_use",
                        "message": str(exc),
                    }
                )

    return candidates, warnings


def rank_use_candidates(candidates, top_n=TOP_N_DEFAULT):
    grouped = {}
    for candidate in candidates:
        label_cn = candidate.get("use_cn") or "其他用途"
        raw_label = candidate.get("raw_use") or candidate.get("use_en") or label_cn
        display_label = _display_use_label(label_cn, raw_label)
        key = label_cn if label_cn != "其他用途" else _normalize_key(raw_label)
        if not key:
            continue

        evidence = _to_number(candidate.get("evidence_count"))
        if pd.isna(evidence) or evidence <= 0:
            evidence = 1

        specificity = candidate.get("specificity")
        if pd.isna(specificity):
            specificity = 0

        source_priority = {
            "product_category": 3,
            "functional_use": 2,
            "product_keyword": 1,
        }.get(candidate.get("source_type"), 0)

        if key not in grouped:
            grouped[key] = {
                "use_cn": display_label,
                "use_en": raw_label,
                "evidence_count": 0.0,
                "max_single_evidence": 0.0,
                "specificity": 0,
                "source_priority": 0,
                "sources": set(),
                "details": set(),
            }

        group = grouped[key]
        group["evidence_count"] += float(evidence)
        group["max_single_evidence"] = max(group["max_single_evidence"], float(evidence))
        group["specificity"] = max(group["specificity"], int(specificity or 0))
        group["source_priority"] = max(group["source_priority"], source_priority)
        group["sources"].add(USE_SOURCE_LABELS.get(candidate.get("source_type"), candidate.get("source_type", "")))
        if raw_label:
            group["details"].add(str(raw_label))

    ranked = sorted(
        grouped.values(),
        key=lambda item: (
            item["evidence_count"],
            item["max_single_evidence"],
            item["source_priority"],
            item["specificity"],
            item["use_cn"],
        ),
        reverse=True,
    )

    output = []
    for rank, item in enumerate(ranked[:top_n], start=1):
        evidence = item["evidence_count"]
        output.append(
            {
                "rank": rank,
                "use_cn": item["use_cn"],
                "use_en": " | ".join(sorted(item["details"]))[:1000],
                "evidence_count": int(evidence) if evidence.is_integer() else evidence,
                "sources": "；".join(sorted(source for source in item["sources"] if source)),
            }
        )
    return output


def build_result_workbook(input_df, summary_df=None, candidates_df=None, errors_df=None):
    if summary_df is None:
        summary_df = pd.DataFrame()
    if candidates_df is None:
        candidates_df = pd.DataFrame()
    if errors_df is None:
        errors_df = pd.DataFrame()

    mapping_df = pd.DataFrame(
        [
            {"英文关键词": " / ".join(keywords), "中文类别": label}
            for keywords, label in USE_TRANSLATION_RULES
        ]
    )

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        normalize_input_columns(input_df)[REQUIRED_IDENTIFIER_COLUMNS].to_excel(
            writer, sheet_name="Input", index=False
        )
        summary_df.to_excel(writer, sheet_name="Top5_Use_Summary", index=False)
        candidates_df.to_excel(writer, sheet_name="All_Use_Candidates", index=False)
        errors_df.to_excel(writer, sheet_name="Warnings", index=False)
        mapping_df.to_excel(writer, sheet_name="CN_Mapping", index=False)
    buffer.seek(0)
    return buffer


def build_empty_summary_template(input_df, top_n=TOP_N_DEFAULT):
    clean_df = normalize_input_columns(input_df)
    return pd.DataFrame(
        [_summary_row(row, {"dtxsid": pd.NA, "status": "待查询"}, [], top_n, "待查询") for _, row in clean_df.iterrows()]
    )


def _summary_row(row, resolution, ranked, top_n, status):
    output = {
        "compound": _display_compound(row),
        "cas": _clean_cell(row.get("cas")),
        "smiles": _clean_cell(row.get("smiles")),
        "input_dtxsid": _clean_cell(row.get("dtxsid")),
        "matched_dtxsid": resolution.get("dtxsid", pd.NA),
        "matched_name": resolution.get("matched_name", pd.NA),
        "matched_cas": resolution.get("matched_cas", pd.NA),
        "match_status": resolution.get("status", pd.NA),
        "query_status": status,
    }

    for idx in range(1, top_n + 1):
        output[f"用途{idx}"] = pd.NA
        output[f"用途{idx}_英文证据"] = pd.NA
        output[f"用途{idx}_证据数量"] = pd.NA

    for item in ranked[:top_n]:
        idx = item["rank"]
        output[f"用途{idx}"] = item["use_cn"]
        output[f"用途{idx}_英文证据"] = item["use_en"]
        output[f"用途{idx}_证据数量"] = item["evidence_count"]

    output["前五用途"] = "；".join(item["use_cn"] for item in ranked[:top_n])
    output["用途来源"] = "；".join(sorted({item["sources"] for item in ranked[:top_n] if item.get("sources")}))
    dtxsid = resolution.get("dtxsid")
    if isinstance(dtxsid, str) and DTXSID_RE.search(dtxsid):
        output["CompTox产品用途页面"] = urllib.parse.urljoin(
            DEFAULT_DASHBOARD_BASE, f"chemical/product-use-categories/{dtxsid}"
        )
        output["CompTox功能用途页面"] = urllib.parse.urljoin(
            DEFAULT_DASHBOARD_BASE, f"chemical/chemical-functional-use/{dtxsid}"
        )
    else:
        output["CompTox产品用途页面"] = pd.NA
        output["CompTox功能用途页面"] = pd.NA
    output["notes"] = resolution.get("message", "")
    return output


def _extract_product_category_candidates(data, source):
    records = _find_dicts(
        data,
        lambda item: any(
            _get_any(item, names) is not pd.NA
            for names in (
                ["displayPuc", "display_puc", "pucName"],
                ["generalCategory"],
                ["productFamily"],
            )
        ),
    )
    candidates = []
    for record in records:
        label = _get_any(record, ["displayPuc", "display_puc", "pucName", "puc", "name"])
        general = _get_any(record, ["generalCategory", "general_category"])
        family = _get_any(record, ["productFamily", "product_family"])
        product_type = _get_any(record, ["productType", "product_type"])
        if pd.isna(label):
            label = _join_nonempty([general, family, product_type], ":")
        if pd.isna(label) or not str(label).strip():
            continue
        if _is_generic_use(label):
            continue

        evidence = _to_number(_get_any(record, ["productCount", "product_count", "count"]))
        description = _get_any(record, ["pucDescription", "description"])
        text_parts = [label, general, family, product_type]
        candidates.append(
            _candidate(
                source_type="product_category",
                source=source,
                raw_use=label,
                general_category=general,
                product_family=family,
                product_type=product_type,
                reported_use=pd.NA,
                harmonized_use=pd.NA,
                evidence_count=evidence,
                description=description,
                use_cn=classify_use_cn(*text_parts),
                specificity=_specificity(label),
            )
        )
    return candidates


def _extract_product_keyword_candidates(data, source):
    candidates = []
    for value in _collect_keyword_values(data):
        label = _clean_cell(value)
        if not label:
            continue
        if _is_generic_use(label):
            continue
        candidates.append(
            _candidate(
                source_type="product_keyword",
                source=source,
                raw_use=label,
                general_category=pd.NA,
                product_family=pd.NA,
                product_type=pd.NA,
                reported_use=label,
                harmonized_use=pd.NA,
                evidence_count=1,
                description=pd.NA,
                use_cn=classify_use_cn(label),
                specificity=_specificity(label),
            )
        )
    return candidates


def _extract_functional_use_candidates(data, source):
    records = _find_dicts(
        data,
        lambda item: any(
            _get_any(item, names) is not pd.NA
            for names in (
                ["harmonizedFunctionalUse", "harmonized_functional_use"],
                ["reportedFunctionalUse", "reported_functional_use"],
            )
        ),
    )
    groups = {}
    for record in records:
        harmonized = _get_any(record, ["harmonizedFunctionalUse", "harmonized_functional_use"])
        reported = _get_any(record, ["reportedFunctionalUse", "reported_functional_use"])
        label = harmonized if not pd.isna(harmonized) and str(harmonized).strip() else reported
        label = _clean_cell(label)
        if not label:
            continue
        if _is_generic_use(label):
            continue
        key = _normalize_key(label)
        if key not in groups:
            groups[key] = {
                "label": label,
                "harmonized": harmonized,
                "reported_values": set(),
                "count": 0,
            }
        groups[key]["count"] += 1
        reported_text = _clean_cell(reported)
        if reported_text:
            groups[key]["reported_values"].add(reported_text)

    candidates = []
    for group in groups.values():
        label = group["label"]
        reported_joined = " | ".join(sorted(group["reported_values"])) if group["reported_values"] else pd.NA
        candidates.append(
            _candidate(
                source_type="functional_use",
                source=source,
                raw_use=label,
                general_category=pd.NA,
                product_family=pd.NA,
                product_type=pd.NA,
                reported_use=reported_joined,
                harmonized_use=group["harmonized"],
                evidence_count=group["count"],
                description=pd.NA,
                use_cn=classify_use_cn(label, reported_joined, group["harmonized"]),
                specificity=_specificity(label),
            )
        )
    return candidates


def _extract_dashboard_product_categories(html):
    records = _extract_nuxt_array_records(html, "pucData")
    return _extract_product_category_candidates(records, source="dashboard:product_category")


def _extract_dashboard_functional_uses(html):
    records = _extract_nuxt_array_records(html, "reportedFunctionalUse")
    if not records:
        records = _extract_nuxt_array_records(html, "FunctionalUse")
    return _extract_functional_use_candidates(records, source="dashboard:functional_use")


def _candidate(
    source_type,
    source,
    raw_use,
    general_category,
    product_family,
    product_type,
    reported_use,
    harmonized_use,
    evidence_count,
    description,
    use_cn,
    specificity,
):
    return {
        "source_type": source_type,
        "source": source,
        "raw_use": _clean_cell(raw_use),
        "use_cn": use_cn,
        "general_category": _clean_cell(general_category),
        "product_family": _clean_cell(product_family),
        "product_type": _clean_cell(product_type),
        "reported_use": _clean_cell(reported_use),
        "harmonized_use": _clean_cell(harmonized_use),
        "evidence_count": evidence_count,
        "description": _clean_cell(description),
        "specificity": specificity,
    }


def classify_use_cn(*texts):
    combined = " ".join(_clean_cell(text) for text in texts if _clean_cell(text)).lower()
    for keywords, label in USE_TRANSLATION_RULES:
        if any(keyword in combined for keyword in keywords):
            return label
    cleaned = combined.strip()
    if cleaned:
        return "其他用途"
    return "未分类"


def _display_use_label(label_cn, raw_label):
    if label_cn == "其他用途":
        raw = _clean_cell(raw_label)
        return f"其他用途：{raw}" if raw else label_cn
    return label_cn


def _is_generic_use(label):
    text = _clean_cell(label).lower()
    if not text:
        return True
    normalized = re.sub(r"\s+", " ", text).strip()
    if normalized in GENERIC_USE_EXACT:
        return True
    return any(pattern in normalized for pattern in GENERIC_USE_PATTERNS)


def _api_get_json(path, params=None, api_base=DEFAULT_API_BASE, api_key=None, timeout=45):
    base = api_base if api_base.endswith("/") else api_base + "/"
    url = urllib.parse.urljoin(base, path)
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    key = DEFAULT_COMPTOX_API_KEY if api_key is None else api_key.strip()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "ChemPriority CompTox use-query module",
    }
    if key:
        headers["x-api-key"] = key

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连接失败: {exc.reason}") from exc

    text = raw.decode("utf-8", errors="replace")
    return json.loads(text)


def _dashboard_get_html(path, timeout=45):
    url = urllib.parse.urljoin(DEFAULT_DASHBOARD_BASE, path)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": "ChemPriority CompTox dashboard fallback",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连接失败: {exc.reason}") from exc
    return raw.decode("utf-8", errors="replace")


def _extract_chemical_candidates(data):
    return _find_dicts(data, lambda item: _get_any(item, ["dtxsid", "dsstoxSubstanceId"]) is not pd.NA)


def _dashboard_search_chemical_candidates(term, timeout=45):
    query = urllib.parse.urlencode({"input_type": "equalsDetails", "inputs": term})
    html = _dashboard_get_html(f"search-results?{query}", timeout=timeout)
    body, variables = _extract_nuxt_body_and_variables(html)
    if not body:
        return []

    candidates = []
    seen = set()
    for match in re.finditer(r"\bdtxsid\s*:", body):
        start = body.rfind("{", 0, match.start())
        end = _find_matching(body, start, "{", "}") if start != -1 else -1
        if start == -1 or end == -1:
            continue
        record = _parse_js_object(body[start : end + 1], variables)
        dtxsid = _get_any(record, ["dtxsid"])
        if _is_missing(dtxsid):
            continue
        dtxsid_key = str(dtxsid).upper()
        if dtxsid_key in seen:
            continue
        seen.add(dtxsid_key)
        candidates.append(record)
    return candidates


def _choose_best_identifier_match(candidates, term, term_type):
    if not candidates:
        return None
    term_norm = _normalize_key(term)

    def score(candidate):
        candidate_dtxsid = _normalize_key(_get_any(candidate, ["dtxsid", "dsstoxSubstanceId"]))
        candidate_cas = _normalize_key(_get_any(candidate, ["casrn", "cas", "casNumber"]))
        candidate_name = _normalize_key(_get_any(candidate, ["preferredName", "name", "label"]))
        candidate_smiles = _normalize_key(_get_any(candidate, ["smiles"]))
        value = 0
        if term_type == "cas" and candidate_cas == term_norm:
            value += 100
        if term_type == "compound" and candidate_name == term_norm:
            value += 100
        if term_type == "smiles" and candidate_smiles == term_norm:
            value += 100
        if candidate_dtxsid == term_norm:
            value += 100
        source_count = _to_number(_get_any(candidate, ["sources", "sourceCount", "cpdat"]))
        if not _is_missing(source_count):
            value += int(source_count)
        qc = _to_number(_get_any(candidate, ["qc", "quality"]))
        if not _is_missing(qc):
            value += max(0, 5 - int(qc))
        return value

    return sorted(candidates, key=score, reverse=True)[0]


def _find_dicts(data, predicate):
    found = []
    stack = [data]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if predicate(current):
                found.append(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _collect_keyword_values(data):
    values = []
    stack = [data]
    keyword_keys = {
        "keyword",
        "keywords",
        "keywordsearch",
        "searchterm",
        "productkeyword",
        "displaypuc",
    }
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if _normalize_key(key) in keyword_keys:
                    if isinstance(value, list):
                        values.extend(value)
                    else:
                        values.append(value)
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, str):
            values.append(current)
    return values


def _extract_nuxt_array_records(html, key):
    body, variables = _extract_nuxt_body_and_variables(html)
    if not body:
        return []
    array_text = _extract_js_array_after_key(body, key)
    if not array_text:
        return []
    records = []
    for item in _split_top_level(array_text):
        item = item.strip()
        if item.startswith("{") and item.endswith("}"):
            records.append(_parse_js_object(item, variables))
    return records


def _extract_nuxt_body_and_variables(html):
    marker = "window.__NUXT__="
    start = html.find(marker)
    if start == -1:
        return "", {}

    script_end = html.find("</script>", start)
    source = html[start + len(marker) : script_end if script_end != -1 else len(html)]
    function_start = source.find("(function(")
    if function_start == -1:
        return source, {}

    params_start = function_start + len("(function(")
    params_end = source.find("){", params_start)
    if params_end == -1:
        return source, {}

    params = [item.strip() for item in source[params_start:params_end].split(",") if item.strip()]
    body_start = source.find("{", params_end)
    body_end = _find_matching(source, body_start, "{", "}")
    if body_start == -1 or body_end == -1:
        return source, {}

    args_start = source.find("(", body_end)
    args_end = _find_matching(source, args_start, "(", ")") if args_start != -1 else -1
    variables = {}
    if args_start != -1 and args_end != -1:
        args = _split_top_level(source[args_start + 1 : args_end])
        for name, token in zip(params, args):
            variables[name] = _parse_js_value(token, {})

    return source[body_start + 1 : body_end], variables


def _extract_js_array_after_key(text, key):
    pattern = f"{key}:"
    start = text.find(pattern)
    if start == -1:
        return ""
    bracket = text.find("[", start + len(pattern))
    if bracket == -1:
        return ""
    end = _find_matching(text, bracket, "[", "]")
    if end == -1:
        return ""
    return text[bracket + 1 : end]


def _parse_js_object(text, variables):
    inner = text.strip()[1:-1]
    output = {}
    for pair in _split_top_level(inner):
        colon = _find_top_level_colon(pair)
        if colon == -1:
            continue
        key = pair[:colon].strip().strip("\"'")
        value = _parse_js_value(pair[colon + 1 :].strip(), variables)
        output[key] = value
    return output


def _parse_js_value(token, variables):
    token = token.strip()
    if not token:
        return pd.NA
    if token in variables:
        return variables[token]
    if token in {"null", "undefined", "void 0", "NaN"}:
        return pd.NA
    if token == "true":
        return True
    if token == "false":
        return False
    if token.startswith('"') and token.endswith('"'):
        try:
            return json.loads(token)
        except json.JSONDecodeError:
            return token[1:-1]
    if token.startswith("'") and token.endswith("'"):
        return token[1:-1]
    if re.fullmatch(r"[-+]?\d+", token):
        return int(token)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+)(?:[Ee][-+]?\d+)?", token):
        return float(token)
    return token


def _split_top_level(text):
    parts = []
    start = 0
    depth = 0
    quote = None
    escape = False
    for idx, char in enumerate(text):
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:idx])
            start = idx + 1
    tail = text[start:]
    if tail.strip():
        parts.append(tail)
    return parts


def _find_matching(text, start, opener, closer):
    if start < 0 or start >= len(text) or text[start] != opener:
        return -1
    depth = 0
    quote = None
    escape = False
    for idx in range(start, len(text)):
        char = text[idx]
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return idx
    return -1


def _find_top_level_colon(text):
    depth = 0
    quote = None
    escape = False
    for idx, char in enumerate(text):
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == ":" and depth == 0:
            return idx
    return -1


def _get_any(record, names, default=pd.NA):
    if not isinstance(record, dict):
        return default
    key_map = {_normalize_key(key): value for key, value in record.items()}
    for name in names:
        value = key_map.get(_normalize_key(name), default)
        if value is not default and not _is_missing(value):
            return value
    return default


def _clean_cell(value):
    if _is_missing(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "<na>"} else text


def _display_compound(row):
    for key in ("compound", "cas", "dtxsid", "smiles"):
        value = _clean_cell(row.get(key))
        if value:
            return value
    return "未命名化合物"


def _to_number(value):
    if _is_missing(value):
        return pd.NA
    if isinstance(value, (int, float)):
        return value
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", str(value))
    return float(match.group(0)) if match else pd.NA


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


def _specificity(label):
    text = _clean_cell(label)
    if not text:
        return 0
    return min(text.count(":") + text.count(">") + 1, 5)


def _join_nonempty(values, separator):
    cleaned = [_clean_cell(value) for value in values if _clean_cell(value)]
    return separator.join(cleaned) if cleaned else pd.NA
